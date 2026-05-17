"""Whisper文字起こし: macOS → whisper.cpp (CoreML), その他 → faster-whisper (CPU int8)"""

import json
import os
import platform
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SYSTEM = platform.system()

_WHISPER_CPP = Path(os.environ.get("WHISPER_CPP_DIR", "/Users/yoshihide/my-projects/whisper.cpp"))
_WHISPER_CLI = _WHISPER_CPP / "build/bin/whisper-cli"
_WHISPER_MODELS = _WHISPER_CPP / "models"


def _model_path(model_size: str) -> Path:
    return _WHISPER_MODELS / f"ggml-{model_size}.bin"


def _write_wav(path: str, audio: np.ndarray, sample_rate: int = 16000) -> None:
    audio_int16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())


@dataclass
class TranscriptSegment:
    text: str
    start: float   # 録音開始からの絶対秒数
    end: float
    segment_id: int


class Transcriber:
    def __init__(
        self,
        model_size: str = "large-v3-turbo",
        language: str = "ja",
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 8,
    ):
        self._language = language
        self._prev_end: float = 0.0

        if SYSTEM == "Darwin":
            self._backend = "whisper_cpp"
            self._model_file = _model_path(model_size)
            if not self._model_file.exists():
                raise FileNotFoundError(f"モデルファイルが見つかりません: {self._model_file}")
            if not _WHISPER_CLI.exists():
                raise FileNotFoundError(f"whisper-cli が見つかりません: {_WHISPER_CLI}")
            # ウォームアップ
            self._run_whisper_cpp(np.zeros(16000, dtype=np.float32))
        else:
            self._backend = "faster-whisper"
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                model_size,
                device=device,
                compute_type=compute_type,
                cpu_threads=cpu_threads,
                num_workers=1,
            )

    def _run_whisper_cpp(self, audio_data: np.ndarray) -> list[dict]:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_f:
            wav_path = wav_f.name
        with tempfile.NamedTemporaryFile(delete=False) as out_f:
            out_path = out_f.name

        try:
            _write_wav(wav_path, audio_data)
            subprocess.run(
                [
                    str(_WHISPER_CLI),
                    "-m", str(self._model_file),
                    "-f", wav_path,
                    "-l", self._language,
                    "--output-json",
                    "-of", out_path,
                ],
                check=True,
                capture_output=True,
            )
            json_path = out_path + ".json"
            with open(json_path) as f:
                return json.load(f).get("transcription", [])
        finally:
            for p in [wav_path, out_path, out_path + ".json"]:
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

    def transcribe(
        self,
        audio_data: np.ndarray,
        time_offset: float = 0.0,
    ) -> list[TranscriptSegment]:
        if self._backend == "whisper_cpp":
            return self._transcribe_whisper_cpp(audio_data, time_offset)
        return self._transcribe_faster(audio_data, time_offset)

    def _transcribe_whisper_cpp(
        self, audio_data: np.ndarray, time_offset: float
    ) -> list[TranscriptSegment]:
        segments = self._run_whisper_cpp(audio_data)
        results: list[TranscriptSegment] = []
        for seg in segments:
            abs_start = time_offset + seg["offsets"]["from"] / 1000.0
            abs_end = time_offset + seg["offsets"]["to"] / 1000.0
            if abs_end <= self._prev_end:
                continue
            text = seg["text"].strip()
            if not text:
                continue
            results.append(TranscriptSegment(text=text, start=abs_start, end=abs_end, segment_id=0))
            self._prev_end = abs_end
        return results

    def _transcribe_faster(
        self, audio_data: np.ndarray, time_offset: float
    ) -> list[TranscriptSegment]:
        segments, _ = self._model.transcribe(
            audio_data,
            language=self._language,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        results: list[TranscriptSegment] = []
        for seg in segments:
            abs_start = time_offset + seg.start
            abs_end = time_offset + seg.end
            if abs_end <= self._prev_end:
                continue
            text = seg.text.strip()
            if not text:
                continue
            results.append(TranscriptSegment(text=text, start=abs_start, end=abs_end, segment_id=0))
            self._prev_end = abs_end
        return results
