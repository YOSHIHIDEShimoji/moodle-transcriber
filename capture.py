"""音声キャプチャ: macOS (BlackHole) / Linux (PulseAudio) / Windows (WASAPI Loopback)"""

import platform
import queue
import threading
from dataclasses import dataclass
from typing import Iterator

import numpy as np
from scipy.signal import resample_poly

WHISPER_SAMPLE_RATE = 16000
DTYPE = "float32"
SYSTEM = platform.system()


@dataclass
class AudioSegment:
    data: np.ndarray   # float32, 16kHz, モノラル
    start_time: float  # 録音開始からの秒数
    segment_id: int


# ─── sounddevice ベース (macOS / Linux) ──────────────────────────────────────

class AudioCapture:
    def __init__(
        self,
        device_index: int,
        segment_duration: float = 30.0,
        overlap_duration: float = 5.0,
    ):
        import sounddevice as sd
        info = sd.query_devices(device_index)
        self._device_index = device_index
        self._segment_duration = segment_duration
        self._overlap_duration = overlap_duration
        self._native_rate = int(info["default_samplerate"])
        self._channels = min(2, int(info["max_input_channels"]))
        self._segment_samples = int(self._native_rate * segment_duration)
        self._overlap_samples = int(self._native_rate * overlap_duration)
        self._buffer: list[np.ndarray] = []
        self._buffer_size = 0
        self._queue: queue.Queue[AudioSegment | None] = queue.Queue()
        self._lock = threading.Lock()
        self._segment_id = 0
        self._samples_captured = 0
        self._stream = None

    def start(self) -> None:
        import sounddevice as sd
        self._stream = sd.InputStream(
            samplerate=self._native_rate,
            channels=self._channels,
            dtype=DTYPE,
            device=self._device_index,
            blocksize=4096,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
        self._flush_remaining()
        self._queue.put(None)

    def segments(self) -> Iterator[AudioSegment]:
        while True:
            seg = self._queue.get()
            if seg is None:
                return
            yield seg

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            print(f"[capture] {status}")
        mono = np.mean(indata, axis=1) if indata.ndim > 1 else indata[:, 0]
        with self._lock:
            self._buffer.append(mono.copy())
            self._buffer_size += len(mono)
            self._samples_captured += len(mono)
            if self._buffer_size >= self._segment_samples:
                combined = np.concatenate(self._buffer)
                seg_data = combined[: self._segment_samples]
                overlap = combined[self._segment_samples - self._overlap_samples :]
                start_time = (self._samples_captured - self._buffer_size) / self._native_rate
                self._queue.put(AudioSegment(
                    data=_resample(seg_data, self._native_rate),
                    start_time=start_time,
                    segment_id=self._segment_id,
                ))
                self._segment_id += 1
                self._buffer = [overlap]
                self._buffer_size = len(overlap)

    def _flush_remaining(self) -> None:
        with self._lock:
            if self._buffer_size < self._native_rate:
                return
            combined = np.concatenate(self._buffer)
            start_time = (self._samples_captured - self._buffer_size) / self._native_rate
            self._queue.put(AudioSegment(
                data=_resample(combined, self._native_rate),
                start_time=start_time,
                segment_id=self._segment_id,
            ))


# ─── Windows WASAPI Loopback (pyaudiowpatch) ─────────────────────────────────

class WindowsAudioCapture:
    def __init__(self, segment_duration: float = 30.0, overlap_duration: float = 5.0):
        try:
            import pyaudiowpatch as pyaudio
            self._pyaudio = pyaudio
        except ImportError:
            raise ImportError(
                "Windows では pip install pyaudiowpatch が必要です"
            )
        self._segment_duration = segment_duration
        self._overlap_duration = overlap_duration
        self._queue: queue.Queue[AudioSegment | None] = queue.Queue()
        self._pa = None
        self._stream = None
        self._segment_id = 0
        self._buffer = np.array([], dtype=np.float32)
        self._lock = threading.Lock()
        self._native_rate = 44100
        self._channels = 2

    def _find_loopback(self) -> int:
        pyaudio = self._pyaudio
        self._pa = pyaudio.PyAudio()
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        # デフォルト出力に対応するループバックを優先
        for i in range(self._pa.get_device_count()):
            d = self._pa.get_device_info_by_index(i)
            if d.get("isLoopbackDevice") and default_out["name"] in d["name"]:
                self._native_rate = int(d["defaultSampleRate"])
                self._channels = d["maxInputChannels"]
                return i
        # フォールバック: 任意のループバックデバイス
        for i in range(self._pa.get_device_count()):
            d = self._pa.get_device_info_by_index(i)
            if d.get("isLoopbackDevice"):
                self._native_rate = int(d["defaultSampleRate"])
                self._channels = d["maxInputChannels"]
                return i
        raise RuntimeError("WASAPI loopbackデバイスが見つかりません")

    def start(self) -> None:
        pyaudio = self._pyaudio
        device_idx = self._find_loopback()
        chunk = int(self._native_rate * 0.1)

        def callback(in_data, frame_count, time_info, status):
            pcm = np.frombuffer(in_data, dtype=np.float32)
            if self._channels > 1:
                pcm = pcm.reshape(-1, self._channels).mean(axis=1)
            with self._lock:
                self._buffer = np.concatenate([self._buffer, pcm])
                seg_samples = int(self._native_rate * self._segment_duration)
                ovl_samples = int(self._native_rate * self._overlap_duration)
                if len(self._buffer) >= seg_samples:
                    seg_data = self._buffer[:seg_samples]
                    self._queue.put(AudioSegment(
                        data=_resample(seg_data, self._native_rate),
                        start_time=float(self._segment_id) * (self._segment_duration - self._overlap_duration),
                        segment_id=self._segment_id,
                    ))
                    self._segment_id += 1
                    self._buffer = self._buffer[seg_samples - ovl_samples:]
            return (None, pyaudio.paContinue)

        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=self._channels,
            rate=self._native_rate,
            input=True,
            input_device_index=device_idx,
            frames_per_buffer=chunk,
            stream_callback=callback,
        )

    def stop(self) -> None:
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
        if self._pa:
            self._pa.terminate()
        with self._lock:
            if len(self._buffer) >= self._native_rate:
                self._queue.put(AudioSegment(
                    data=_resample(self._buffer, self._native_rate),
                    start_time=float(self._segment_id) * (self._segment_duration - self._overlap_duration),
                    segment_id=self._segment_id,
                ))
        self._queue.put(None)

    def segments(self) -> Iterator[AudioSegment]:
        while True:
            seg = self._queue.get()
            if seg is None:
                return
            yield seg


# ─── ユーティリティ ───────────────────────────────────────────────────────────

def _resample(data: np.ndarray, native_rate: int) -> np.ndarray:
    if native_rate == WHISPER_SAMPLE_RATE:
        return data
    from math import gcd
    g = gcd(WHISPER_SAMPLE_RATE, native_rate)
    return resample_poly(data, WHISPER_SAMPLE_RATE // g, native_rate // g).astype(np.float32)


def create_capture(
    device_index: int | None = None,
    segment_duration: float = 30.0,
    overlap_duration: float = 5.0,
) -> "AudioCapture | WindowsAudioCapture":
    """プラットフォームに応じたキャプチャインスタンスを生成"""
    if SYSTEM == "Windows":
        return WindowsAudioCapture(segment_duration, overlap_duration)
    if device_index is None:
        device_index = find_loopback_device()
    return AudioCapture(device_index, segment_duration, overlap_duration)


def find_loopback_device() -> int:
    if SYSTEM == "Darwin":
        return _find_blackhole()
    elif SYSTEM == "Linux":
        return _find_pulse_monitor()
    else:
        raise RuntimeError("Windows では WindowsAudioCapture を使用してください")


def _find_blackhole() -> int:
    import sounddevice as sd
    candidates = [
        (i, d) for i, d in enumerate(sd.query_devices())
        if "blackhole" in d["name"].lower() and d["max_input_channels"] >= 1
    ]
    if not candidates:
        raise RuntimeError(
            "BlackHole デバイスが見つかりません。\n"
            "brew install --cask blackhole-2ch を実行してください。"
        )
    return max(candidates, key=lambda x: x[1]["max_input_channels"])[0]


def _find_pulse_monitor() -> int:
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and "monitor" in d["name"].lower():
            return i
    raise RuntimeError(
        "PulseAudio モニターソースが見つかりません。\n"
        "pactl list short sources でモニターソースを確認してください。\n"
        "作成例: pactl load-module module-null-sink sink_name=moodle"
    )


def list_all_devices() -> None:
    if SYSTEM == "Windows":
        try:
            import pyaudiowpatch as pyaudio
            pa = pyaudio.PyAudio()
            print("利用可能な入力デバイス (Windows WASAPI):")
            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                if d["maxInputChannels"] > 0:
                    lb = " ← Loopback" if d.get("isLoopbackDevice") else ""
                    print(f"  [{i:2d}] {d['name']}  ({d['maxInputChannels']}ch){lb}")
            pa.terminate()
        except ImportError:
            print("pip install pyaudiowpatch が必要です")
        return

    import sounddevice as sd
    print("利用可能な入力デバイス:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            if "blackhole" in d["name"].lower():
                tag = " ← BlackHole (macOS)"
            elif "monitor" in d["name"].lower():
                tag = " ← Monitor (Linux)"
            else:
                tag = ""
            print(f"  [{i:2d}] {d['name']}  ({d['max_input_channels']}ch, {int(d['default_samplerate'])}Hz){tag}")
