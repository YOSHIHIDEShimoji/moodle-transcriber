"""Whisper文字起こし: macOS → mlx-whisper (Metal), その他 → faster-whisper (CPU int8)"""

import platform
from dataclasses import dataclass

import numpy as np

SYSTEM = platform.system()


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
            self._backend = "mlx"
            self._model_repo = f"mlx-community/whisper-{model_size}"
            import mlx_whisper as _mlx
            self._mlx = _mlx
            # ウォームアップ（初回モデルロードを起動時に済ませる）
            _mlx.transcribe(
                np.zeros(16000, dtype=np.float32),
                path_or_hf_repo=self._model_repo,
                language=self._language,
                verbose=None,
            )
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

    def transcribe(
        self,
        audio_data: np.ndarray,
        time_offset: float = 0.0,
    ) -> list[TranscriptSegment]:
        if self._backend == "mlx":
            return self._transcribe_mlx(audio_data, time_offset)
        return self._transcribe_faster(audio_data, time_offset)

    def _transcribe_mlx(
        self, audio_data: np.ndarray, time_offset: float
    ) -> list[TranscriptSegment]:
        result = self._mlx.transcribe(
            audio_data,
            path_or_hf_repo=self._model_repo,
            language=self._language,
            verbose=None,
            temperature=0.0,
            no_speech_threshold=0.6,
            condition_on_previous_text=False,
        )
        results: list[TranscriptSegment] = []
        for seg in result.get("segments", []):
            abs_start = time_offset + seg["start"]
            abs_end = time_offset + seg["end"]
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
