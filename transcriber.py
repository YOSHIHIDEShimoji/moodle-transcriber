"""Whisper文字起こし: macOS → whisper.cpp (Metal/CoreML), その他 → faster-whisper (CPU int8)

macOS では whisper-server を常駐させ、セグメントごとの
モデル再ロード（whisper-cli 方式の欠点）を避ける。
サーバーが起動できない場合は whisper-cli にフォールバックする。
"""

import atexit
import difflib
import io
import json
import logging
import os
import platform
import re
import socket
import subprocess
import tempfile
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_logger = logging.getLogger("moodle-transcriber")

SYSTEM = platform.system()

_WHISPER_CPP = Path(os.environ.get("WHISPER_CPP_DIR", "/Users/yoshihide/my-projects/whisper.cpp"))
_WHISPER_CLI = _WHISPER_CPP / "build/bin/whisper-cli"
_WHISPER_SERVER = _WHISPER_CPP / "build/bin/whisper-server"
_WHISPER_MODELS = _WHISPER_CPP / "models"

# 無音ゲート: このRMS未満のセグメントは文字起こしせずスキップする。
# 無音を Whisper に渡すと「ご視聴ありがとうございました」等の幻覚を生成するため。
# （BlackHole 経由の無音はほぼ完全なゼロ信号。通常の講義音声の RMS は 1e-2 前後）
SILENCE_RMS_THRESHOLD = 5e-4


def audio_rms(audio: np.ndarray) -> float:
    """音声セグメントの RMS を返す。"""
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))


def is_silent(audio: np.ndarray) -> bool:
    """セグメントが無音（幻覚を生むだけの入力）かどうか。"""
    return audio_rms(audio) < SILENCE_RMS_THRESHOLD


def _model_path(model_size: str) -> Path:
    return _WHISPER_MODELS / f"ggml-{model_size}.bin"


def _wav_bytes(audio: np.ndarray, sample_rate: int = 16000) -> bytes:
    audio_int16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())
    return buf.getvalue()


def _write_wav(path: str, audio: np.ndarray, sample_rate: int = 16000) -> None:
    Path(path).write_bytes(_wav_bytes(audio, sample_rate))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# 境界重複判定用のテキスト正規化（空白・句読点を除去して比較する）
_NORM_RE = re.compile(r"[\s、。,.．，!?！？]+")


def _norm_text(text: str) -> str:
    return _NORM_RE.sub("", text)


# 境界セグメントの重複判定。完全一致・包含に加え、語尾ゆれ
# （「終わりました」/「終わります」、「園芸学部」/「演技学部」）を
# 類似度で吸収する。短い文は誤除去しやすいので 8 文字以上に限る。
BOUNDARY_SIMILARITY = 0.75


def _similar(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) < 8 or len(b) < 8:
        return False
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= BOUNDARY_SIMILARITY


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
        # オーバーラップ境界の重複除去用: 直近に出力したテキスト（正規化済み）
        self._recent_texts: deque[str] = deque(maxlen=8)
        self._server_proc: subprocess.Popen | None = None
        self._server_url: str | None = None

        if SYSTEM == "Darwin":
            self._model_file = _model_path(model_size)
            if not self._model_file.exists():
                raise FileNotFoundError(
                    f"モデルファイルが見つかりません: {self._model_file}\n"
                    f"（whisper.cpp の場所は環境変数 WHISPER_CPP_DIR で指定できます）"
                )
            if self._start_server(cpu_threads):
                self._backend = "whisper_cpp_server"
            elif _WHISPER_CLI.exists():
                self._backend = "whisper_cpp_cli"
                # ウォームアップ（CoreML コンパイルキャッシュを温める）
                self._run_whisper_cli(np.zeros(16000, dtype=np.float32))
            else:
                raise FileNotFoundError(
                    f"whisper-server / whisper-cli が見つかりません: {_WHISPER_CPP}/build/bin\n"
                    f"（whisper.cpp の場所は環境変数 WHISPER_CPP_DIR で指定できます）"
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

    # ─── ライフサイクル ──────────────────────────────────────────────────────

    @property
    def backend_desc(self) -> str:
        """出力ファイルヘッダ用のバックエンド表記。"""
        return {
            "whisper_cpp_server": "whisper.cpp server (Metal/CoreML)",
            "whisper_cpp_cli": "whisper.cpp cli (Metal/CoreML)",
            "faster-whisper": "faster-whisper (CPU int8)",
        }.get(self._backend, self._backend)

    def reset(self) -> None:
        """URL（動画）の切り替わり時に呼ぶ。

        _prev_end は「録音開始からの絶対秒数」に対する重複除去カーソルなので、
        リセットしないと 2 本目以降の動画の冒頭（前の動画の長さぶん）が
        すべて重複と誤判定されて捨てられる。
        """
        self._prev_end = 0.0
        self._recent_texts.clear()

    def close(self) -> None:
        """常駐 whisper-server を終了する。"""
        proc = self._server_proc
        if proc is None:
            return
        self._server_proc = None
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ─── whisper.cpp server ─────────────────────────────────────────────────

    def _start_server(self, cpu_threads: int) -> bool:
        if not _WHISPER_SERVER.exists():
            return False
        port = _free_port()
        try:
            self._server_proc = subprocess.Popen(
                [
                    str(_WHISPER_SERVER),
                    "-m", str(self._model_file),
                    "-l", self._language,
                    "-t", str(cpu_threads),
                    "--host", "127.0.0.1",
                    "--port", str(port),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            _logger.warning("whisper-server 起動失敗: %s", e)
            return False

        self._server_url = f"http://127.0.0.1:{port}/inference"
        atexit.register(self.close)

        # モデルロード完了（=ポートが開く）を待つ
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self._server_proc.poll() is not None:
                _logger.warning("whisper-server が起動直後に終了しました (rc=%s)",
                                self._server_proc.returncode)
                self._server_proc = None
                return False
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    _logger.info("whisper-server ready on port %d", port)
                    return True
            except OSError:
                time.sleep(0.5)
        _logger.warning("whisper-server の起動がタイムアウトしました")
        self.close()
        return False

    def _run_whisper_server(self, audio_data: np.ndarray) -> list[dict]:
        """常駐サーバーに投げて [{text, start, end}] を返す。"""
        import requests
        resp = requests.post(
            self._server_url,
            files={"file": ("segment.wav", _wav_bytes(audio_data), "audio/wav")},
            data={"response_format": "verbose_json", "temperature": "0.0"},
            timeout=300,
        )
        resp.raise_for_status()
        segments = resp.json().get("segments", [])
        return [
            {"text": s.get("text", ""), "start": float(s["start"]), "end": float(s["end"])}
            for s in segments
        ]

    # ─── whisper.cpp cli（フォールバック） ──────────────────────────────────

    def _run_whisper_cli(self, audio_data: np.ndarray) -> list[dict]:
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
                raw = json.load(f).get("transcription", [])
            return [
                {
                    "text": seg["text"],
                    "start": seg["offsets"]["from"] / 1000.0,
                    "end": seg["offsets"]["to"] / 1000.0,
                }
                for seg in raw
            ]
        finally:
            for p in [wav_path, out_path, out_path + ".json"]:
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

    # ─── 共通 ────────────────────────────────────────────────────────────────

    def transcribe(
        self,
        audio_data: np.ndarray,
        time_offset: float = 0.0,
    ) -> list[TranscriptSegment]:
        # 無音ゲート: 幻覚（「ご視聴ありがとうございました」等）を防ぐ
        if is_silent(audio_data):
            return []

        if self._backend == "whisper_cpp_server":
            try:
                raw = self._run_whisper_server(audio_data)
            except Exception as e:
                _logger.warning("whisper-server 呼び出し失敗、cli にフォールバック: %s", e)
                if _WHISPER_CLI.exists():
                    self.close()
                    self._backend = "whisper_cpp_cli"
                    raw = self._run_whisper_cli(audio_data)
                else:
                    raise
        elif self._backend == "whisper_cpp_cli":
            raw = self._run_whisper_cli(audio_data)
        else:
            raw = self._run_faster(audio_data)

        return self._dedup(raw, time_offset)

    def _run_faster(self, audio_data: np.ndarray) -> list[dict]:
        segments, _ = self._model.transcribe(
            audio_data,
            language=self._language,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        return [{"text": s.text, "start": s.start, "end": s.end} for s in segments]

    def _dedup(self, raw: list[dict], time_offset: float) -> list[TranscriptSegment]:
        """時間 + テキストの二段構えでオーバーラップ重複を除去する。

        1) 時間: すでに出力済みの時刻 (_prev_end) 以前で終わるセグメントは捨てる
        2) テキスト: _prev_end をまたぐ境界セグメントは、直近に出力した
           テキストと（正規化して）一致・包含関係にあれば捨てる。
           講師が後で同じフレーズを繰り返すケースは境界をまたがないため誤除去しない。
        """
        results: list[TranscriptSegment] = []
        for seg in raw:
            abs_start = time_offset + seg["start"]
            abs_end = time_offset + seg["end"]
            if abs_end <= self._prev_end:
                continue
            text = seg["text"].strip()
            if not text:
                continue
            # 境界をまたぐ（= 出力済み区間に食い込んで始まる）セグメントのみテキスト照合。
            # 連続セグメント（abs_start == prev_end）に発火しないよう負のマージンを取る
            if abs_start < self._prev_end - 0.3:
                norm = _norm_text(text)
                if norm and any(_similar(norm, r) for r in self._recent_texts):
                    self._prev_end = max(self._prev_end, abs_end)
                    continue
            results.append(TranscriptSegment(text=text, start=abs_start, end=abs_end, segment_id=0))
            self._prev_end = abs_end
            norm = _norm_text(text)
            if norm:
                self._recent_texts.append(norm)
        return results
