"""出力フォーマット管理: TXT / SRT / VTT"""

from datetime import datetime
from enum import Enum
from pathlib import Path

from transcriber import TranscriptSegment


class OutputFormat(Enum):
    TXT = "txt"
    SRT = "srt"
    VTT = "vtt"


class OutputWriter:
    def __init__(
        self,
        output_path: Path,
        fmt: OutputFormat,
        model_size: str,
        language: str,
        title: str | None = None,
        page_url: str | None = None,
    ):
        self._path = output_path
        self._fmt = fmt
        self._srt_index = 1
        self._file = output_path.open("w", encoding="utf-8")

        if fmt == OutputFormat.TXT:
            if title:
                self._file.write(f"# {title}\n")
            self._file.write(f"# Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            if page_url:
                self._file.write(f"# URL: {page_url}\n")
            self._file.write(f"# Model: {model_size} (int8) | Language: {language}\n")
            self._file.write(f"# {'─' * 50}\n\n")
        elif fmt == OutputFormat.VTT:
            self._file.write("WEBVTT\n\n")

        self._file.flush()

    def append(self, seg: TranscriptSegment) -> None:
        if self._fmt == OutputFormat.TXT:
            ts = _fmt_ts(seg.start)
            self._file.write(f"[{ts}] {seg.text}\n")

        elif self._fmt == OutputFormat.SRT:
            self._file.write(f"{self._srt_index}\n")
            self._file.write(f"{_fmt_srt(seg.start)} --> {_fmt_srt(seg.end)}\n")
            self._file.write(f"{seg.text}\n\n")
            self._srt_index += 1

        elif self._fmt == OutputFormat.VTT:
            self._file.write(f"{_fmt_vtt(seg.start)} --> {_fmt_vtt(seg.end)}\n")
            self._file.write(f"{seg.text}\n\n")

        self._file.flush()

    def finalize(self) -> None:
        self._file.close()


def _fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_srt(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_vtt(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
