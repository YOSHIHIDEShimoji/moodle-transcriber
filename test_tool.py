"""自動テストスクリプト: import / フォーマット / デバイス / AppleScript / Whisper"""

import shutil
import subprocess
import sys

import numpy as np

PASS = "✓"
FAIL = "✗"
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, msg: str = "") -> None:
    mark = PASS if ok else FAIL
    print(f"{mark} {name}" + (f": {msg}" if msg else ""))
    results.append((name, ok))


# ─── 1. imports ───────────────────────────────────────────────────────────────

def test_imports() -> None:
    print("\n[1] モジュールインポート")
    for mod in ["capture", "transcriber", "output", "platform_utils"]:
        try:
            __import__(mod)
            check(f"import {mod}", True)
        except Exception as e:
            check(f"import {mod}", False, str(e))


# ─── 2. output.py フォーマット関数 ────────────────────────────────────────────

def test_output_formatting() -> None:
    print("\n[2] 出力フォーマット関数")
    from output import _fmt_ts, _fmt_srt, _fmt_vtt

    cases = [
        (_fmt_ts,  0.0,    "00:00:00"),
        (_fmt_ts,  65.0,   "00:01:05"),
        (_fmt_ts,  3661.0, "01:01:01"),
        (_fmt_srt, 1.5,    "00:00:01,500"),
        (_fmt_vtt, 1.5,    "00:00:01.500"),
    ]
    for fn, val, expected in cases:
        got = fn(val)
        check(f"{fn.__name__}({val})", got == expected, f"got={got!r} expected={expected!r}")


# ─── 3. デバイス・ツール確認 ─────────────────────────────────────────────────

def test_devices_and_tools() -> None:
    print("\n[3] デバイス・ツール")

    # BlackHole
    import sounddevice as sd
    devices = sd.query_devices()
    bh = [d for d in devices if "blackhole" in d["name"].lower() and d["max_input_channels"] > 0]
    check("BlackHole 2ch デバイス", bool(bh), bh[0]["name"] if bh else "未検出 → brew install --cask blackhole-2ch")

    # SwitchAudioSource
    found = bool(shutil.which("SwitchAudioSource"))
    if found:
        cur = subprocess.run(["SwitchAudioSource", "-c"], capture_output=True, text=True).stdout.strip()
        check("SwitchAudioSource", True, f"現在の出力: {cur}")
    else:
        check("SwitchAudioSource", False, "未インストール → brew install switchaudio-osx")


# ─── 4. AppleScript / Chrome 連携 ────────────────────────────────────────────

def test_applescript() -> None:
    import platform
    print("\n[4] AppleScript / Chrome")

    if platform.system() != "Darwin":
        check("AppleScript (macOS専用)", True, "スキップ (非macOS)")
        return

    # Chrome が起動しているか
    r = subprocess.run(
        ["osascript", "-e", "return application \"Google Chrome\" is running"],
        capture_output=True, text=True, timeout=5,
    )
    chrome_running = r.stdout.strip() == "true"
    check("Google Chrome が起動中", chrome_running, "" if chrome_running else "Chromeを起動してから再テスト")

    if chrome_running:
        # タイトル取得
        r2 = subprocess.run(
            ["osascript", "-e", "tell application \"Google Chrome\" to return title of active tab of front window"],
            capture_output=True, text=True, timeout=5,
        )
        ok = r2.returncode == 0
        msg = r2.stdout.strip()[:60] if ok else r2.stderr.strip()
        check("Chrome タイトル取得 (AppleScript権限)", ok, msg)

        # JS 注入テスト
        r3 = subprocess.run(
            ["osascript", "-e",
             'tell application "Google Chrome" to execute front window\'s active tab javascript "document.visibilityState"'],
            capture_output=True, text=True, timeout=5,
        )
        ok3 = r3.returncode == 0 and "visible" in r3.stdout
        check("JS injection (visibilityState)", ok3,
              r3.stdout.strip() if ok3 else r3.stderr.strip())


# ─── 5. Whisper tiny モデル + 合成音声 ───────────────────────────────────────

def test_whisper() -> None:
    print("\n[5] Whisper (tiny モデル + 合成音声)")
    print("    ※ 初回のみモデルDL (~40MB)")

    try:
        from transcriber import Transcriber
        t = Transcriber(model_size="tiny", language="ja", cpu_threads=4)
        check("Whisper tiny モデルロード", True)
    except Exception as e:
        check("Whisper tiny モデルロード", False, str(e))
        return

    # 3秒の 440Hz 正弦波 (16kHz)
    sr = 16000
    arr = (0.3 * np.sin(2 * np.pi * 440 * np.linspace(0, 3, sr * 3))).astype(np.float32)
    try:
        segs = t.transcribe(arr, time_offset=0.0)
        check("Transcriber.transcribe() 実行", True, f"{len(segs)} segments (正弦波なので0でもOK)")
    except Exception as e:
        check("Transcriber.transcribe() 実行", False, str(e))


# ─── 6. AudioCapture セグメント分割ロジック ──────────────────────────────────

def test_capture_segmentation() -> None:
    print("\n[6] AudioCapture セグメント分割ロジック (sounddeviceなし)")

    from unittest.mock import MagicMock, patch

    mock_info = {"default_samplerate": 16000.0, "max_input_channels": 2}
    mock_sd = MagicMock()
    mock_sd.query_devices.return_value = mock_info

    with patch.dict(sys.modules, {"sounddevice": mock_sd}):
        import importlib, capture as cap_mod
        importlib.reload(cap_mod)
        from capture import AudioCapture, AudioSegment

    cap = AudioCapture.__new__(AudioCapture)
    cap._device_index = 0
    cap._segment_duration = 2.0
    cap._overlap_duration = 0.5
    cap._native_rate = 16000
    cap._channels = 1
    cap._segment_samples = 32000
    cap._overlap_samples = 8000
    cap._buffer = []
    cap._buffer_size = 0
    cap._segment_id = 0
    cap._samples_captured = 0

    import queue, threading
    cap._queue = queue.Queue()
    cap._lock = threading.Lock()
    cap._stream = None

    # 2秒分 (32000 samples) のデータを投入してセグメントが生成されるか確認
    chunk = np.zeros((16000, 1), dtype=np.float32)  # 1秒チャンク
    cap._callback(chunk, 16000, None, None)
    cap._callback(chunk, 16000, None, None)

    seg = cap._queue.get_nowait()
    check("AudioCapture: 2秒→セグメント生成", isinstance(seg, AudioSegment))
    check("AudioCapture: セグメントは16kHz", len(seg.data) == 32000, f"samples={len(seg.data)}")
    check("AudioCapture: オーバーラップ保持", cap._buffer_size == 8000, f"buffer_size={cap._buffer_size}")


# ─── 7. _url_to_pattern / _collect_urls / _unique_path ───────────────────────

def test_multi_url_utils() -> None:
    print("\n[7] 複数URL処理ユーティリティ (_url_to_pattern / _collect_urls / _unique_path)")
    import argparse, os, tempfile
    from pathlib import Path
    from main import _url_to_pattern, _collect_urls, _unique_path

    # _url_to_pattern: id= クエリパラメータを抽出
    cases = [
        ("https://moodle.example.com/mod/scorm/player.php?id=123", "id=123"),
        ("https://moodle.example.com/mod/scorm/player.php?id=456&cm=789", "id=456"),
        ("https://moodle.example.com/course/view.php", "moodle.example.com/course/view.php"),
    ]
    for url, expected in cases:
        got = _url_to_pattern(url)
        check(f"_url_to_pattern id={expected}", got == expected, f"got={got!r}")

    # _collect_urls: --urls
    args = argparse.Namespace(urls=["https://a.com", "https://b.com"], url_file=None)
    check("_collect_urls --urls", _collect_urls(args) == ["https://a.com", "https://b.com"])

    # _collect_urls: --url-file（空行はスキップ）
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("https://c.com\nhttps://d.com\n  \n")
        fname = f.name
    try:
        args2 = argparse.Namespace(urls=None, url_file=fname)
        got2 = _collect_urls(args2)
        check("_collect_urls --url-file", got2 == ["https://c.com", "https://d.com"], f"got={got2}")
    finally:
        os.unlink(fname)

    # _collect_urls: 両方指定で結合
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("https://e.com\n")
        fname2 = f.name
    try:
        args3 = argparse.Namespace(urls=["https://a.com"], url_file=fname2)
        got3 = _collect_urls(args3)
        check("_collect_urls 両方指定で結合", got3 == ["https://a.com", "https://e.com"], f"got={got3}")
    finally:
        os.unlink(fname2)

    # _unique_path: 重複なし
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir) / "lecture.txt"
        check("_unique_path: 重複なし", _unique_path(p) == p)

        p.touch()
        p2 = _unique_path(p)
        check("_unique_path: 1件重複 → _2", p2.name == "lecture_2.txt", f"got={p2.name}")

        p2.touch()
        p3 = _unique_path(p)
        check("_unique_path: 2件重複 → _3", p3.name == "lecture_3.txt", f"got={p3.name}")


# ─── 8. WindowKeepAlive save_interval ────────────────────────────────────────

def test_window_keep_alive_save_interval() -> None:
    print("\n[8] WindowKeepAlive save_interval")
    from platform_utils import WindowKeepAlive

    ka = WindowKeepAlive("chrome", interval=20.0, url_pattern="example.com", save_interval=60.0)
    check("save_interval 設定値", ka._save_interval == 60.0)
    check("_last_save 初期値 0.0", ka._last_save == 0.0)

    ka2 = WindowKeepAlive("chrome", save_interval=0.0)
    check("save_interval=0 で無効", ka2._save_interval == 0.0)

    # デフォルト値
    ka3 = WindowKeepAlive("chrome")
    check("save_interval デフォルト 60.0", ka3._save_interval == 60.0)


# ─── 9. argparse: --timestamps / --urls / --url-file / --save-interval ───────

def test_new_args() -> None:
    print("\n[9] 新規引数 (--timestamps / --urls / --url-file / --save-interval)")
    from main import build_parser

    p = build_parser()

    # --timestamps: デフォルト False
    args = p.parse_args([])
    check("--timestamps デフォルト False", args.timestamps is False)
    args_ts = p.parse_args(["--timestamps"])
    check("--timestamps 指定で True", args_ts.timestamps is True)

    # --save-interval: デフォルト 60.0
    check("--save-interval デフォルト 60.0", args.save_interval == 60.0)
    args_si = p.parse_args(["--save-interval", "30"])
    check("--save-interval=30 の解析", args_si.save_interval == 30.0)

    # --urls
    args_urls = p.parse_args(["--urls", "https://a.com", "https://b.com"])
    check("--urls 複数値", args_urls.urls == ["https://a.com", "https://b.com"])

    # --url-file
    args_uf = p.parse_args(["--url-file", "urls.txt"])
    check("--url-file 解析", args_uf.url_file == "urls.txt")


# ─── main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("moodle-transcriber 自動テスト")
    print("=" * 55)

    test_imports()
    test_output_formatting()
    test_devices_and_tools()
    test_applescript()
    test_whisper()
    test_capture_segmentation()
    test_multi_url_utils()
    test_window_keep_alive_save_interval()
    test_new_args()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'=' * 55}")
    print(f"結果: {passed}/{total} passed")
    if passed < total:
        print("失敗した項目:")
        for name, ok in results:
            if not ok:
                print(f"  {FAIL} {name}")
    print("=" * 55)

    sys.exit(0 if passed == total else 1)
