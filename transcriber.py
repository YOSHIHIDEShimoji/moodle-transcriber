"""Whisper文字起こし: faster-whisper + CTranslate2 int8量子化"""

from dataclasses import dataclass

import numpy as np
from faster_whisper import WhisperModel


@dataclass
class TranscriptSegment:
    text: str
    start: float   # 録音開始からの絶対秒数
    end: float
    segment_id: int


class Transcriber:
    def __init__(
        self,
        model_size: str = "large-v3",
        language: str = "ja",
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 8,
    ):
        self._language = language
        self._model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
            num_workers=1,
        )
        self._prev_end: float = 0.0

    def transcribe(
        self,
        audio_data: np.ndarray,
        time_offset: float = 0.0,
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

            results.append(
                TranscriptSegment(
                    text=text,
                    start=abs_start,
                    end=abs_end,
                    segment_id=0,
                )
            )
            self._prev_end = abs_end

        return results
