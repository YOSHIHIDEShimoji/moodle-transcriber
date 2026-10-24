"""ページ内キャプチャの受け口: base64 PCM（int16 ステレオ 16kHz）→ 30 秒セグメント。

BlackHole 版 `capture.AudioCapture` と同じインターフェース（start / stop / segments）を持ち、
main.py の文字起こしループから見て差し替え可能。入力はすでに 16kHz なのでリサンプル不要。
ステレオは `capture.downmix_to_mono` で位相を見てモノラル化する（逆相ステレオ対策を共有）。
"""

from __future__ import annotations

import base64
import queue
import threading

import numpy as np

from capture import AudioSegment, downmix_to_mono

SAMPLE_RATE = 16000


class PageAudioCapture:
    def __init__(self, segment_duration: float = 30.0, overlap_duration: float = 5.0):
        self._segment_samples = int(SAMPLE_RATE * segment_duration)
        self._overlap_samples = int(SAMPLE_RATE * overlap_duration)
        self._buffer: list[np.ndarray] = []
        self._buffer_size = 0
        self._samples_total = 0
        self._segment_id = 0
        self._queue: queue.Queue[AudioSegment | None] = queue.Queue()
        self._lock = threading.Lock()
        self._running = False
        self._stopped = False
        self._ended = False
        self.chunks_received = 0

    # ── 入力 ─────────────────────────────────────────────────────────────
    def feed_b64(self, b64: str) -> None:
        """ブラウザ binding から呼ばれる。int16 LRLR... を float32 モノラルにして蓄積する。"""
        if not self._running:
            return
        raw = np.frombuffer(base64.b64decode(b64), dtype=np.int16)
        if raw.size == 0:
            return
        if raw.size % 2:
            raw = raw[:-1]
        stereo = raw.reshape(-1, 2).astype(np.float32) / 32767.0
        self.feed_pcm(stereo)

    def feed_pcm(self, pcm: np.ndarray) -> None:
        """(frames, 2) または (frames,) の float32 を受け取る。テストからも直接呼ぶ。"""
        if not self._running:
            return
        mono = downmix_to_mono(pcm).astype(np.float32, copy=False)
        with self._lock:
            self.chunks_received += 1
            self._buffer.append(mono)
            self._buffer_size += len(mono)
            self._samples_total += len(mono)
            while self._buffer_size >= self._segment_samples:
                combined = np.concatenate(self._buffer)
                seg = combined[: self._segment_samples]
                start_time = (self._samples_total - self._buffer_size) / SAMPLE_RATE
                self._queue.put(AudioSegment(data=seg.copy(), start_time=start_time, segment_id=self._segment_id))
                self._segment_id += 1
                rest = combined[self._segment_samples - self._overlap_samples:]
                self._buffer = [rest]
                self._buffer_size = len(rest)

    # ── ライフサイクル ───────────────────────────────────────────────────
    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        """冪等。残りバッファ（1 秒以上）を最終セグメントとして流し、終端 None を入れる。"""
        if self._stopped:
            return
        self._stopped = True
        self._running = False
        with self._lock:
            if self._buffer_size >= SAMPLE_RATE:
                combined = np.concatenate(self._buffer)
                # 直前セグメントとのオーバーラップ分は既出なので除く
                fresh = combined[self._overlap_samples:] if self._segment_id > 0 else combined
                if len(fresh) >= SAMPLE_RATE:
                    start_time = (self._samples_total - len(fresh)) / SAMPLE_RATE
                    self._queue.put(AudioSegment(data=fresh.copy(), start_time=start_time, segment_id=self._segment_id))
                    self._segment_id += 1
            self._buffer = []
            self._buffer_size = 0
        self._queue.put(None)

    def segments(self):
        """セグメントを順に yield する。stop() 後に終端で抜ける。"""
        while True:
            seg = self._queue.get()
            if seg is None:
                return
            yield seg

    def next_segment(self, timeout: float | None = None) -> AudioSegment | None:
        """次のセグメントを返す。timeout 秒待っても無ければ None（終端でも None）。

        ページが止まって音声チャンクが来なくなっても、呼び出し側が終了判定を
        進められるようにタイムアウト付きで待つ。終端（stop 後）は None を返し続ける。
        """
        if self._ended:
            return None
        try:
            seg = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if seg is None:
            self._ended = True
            return None
        return seg

    @property
    def seconds_received(self) -> float:
        return self._samples_total / SAMPLE_RATE
