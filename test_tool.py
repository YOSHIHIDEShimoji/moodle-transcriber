"""自動テストスクリプト: import / フォーマット / デバイス / AppleScript / Whisper"""

import shutil
from pathlib import Path
import subprocess
import sys
import time

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
    import os, platform
    print("\n[4] AppleScript / Chrome（blackhole エンジン用・本人の Chrome を読む）")

    if platform.system() != "Darwin":
        check("AppleScript (macOS専用)", True, "スキップ (非macOS)")
        return
    # 既定の page エンジンは本人の Chrome を使わない。この検査は本人の Chrome に
    # JS を流すので、従来方式を検証したいときだけ MT_BLACKHOLE_CHECKS=1 で有効にする。
    if os.environ.get("MT_BLACKHOLE_CHECKS") != "1":
        check("AppleScript / Chrome 検査", True, "スキップ (MT_BLACKHOLE_CHECKS=1 で有効・本人の Chrome を触らない)")
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

    # _url_to_pattern: id= / a= クエリパラメータを抽出
    # （可能なら player.php?a=... のようにパス付きで返し部分一致誤爆を防ぐ）
    cases = [
        ("https://moodle.example.com/mod/scorm/player.php?id=123", "id=123"),
        ("https://moodle.example.com/mod/scorm/player.php?id=456&cm=789", "id=456"),
        ("https://moodle.example.com/mod/scorm/view.php?id=123", "view.php?id=123"),
        # SCORM player.php?a= (リダイレクト後の実URL)
        ("https://moodle.gs.chiba-u.jp/moodle/mod/scorm/player.php?a=176267&currentorg&scoid=352590",
         "player.php?a=176267"),
        ("https://moodle.example.com/course/view.php", "moodle.example.com/course/view.php"),
    ]
    for url, expected in cases:
        got = _url_to_pattern(url)
        check(f"_url_to_pattern({expected})", got == expected, f"got={got!r}")

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

def test_progress_bar() -> None:
    print("\n[8] 進捗バー (_fmt_progress / get_video_time import)")
    from main import _fmt_progress

    # 50% / 30分00秒経過 / 60分00秒
    s = _fmt_progress(50, 1800.0, 3600.0)
    check("_fmt_progress 50%: バー半分", "██████████░░░░░░░░░░" in s, repr(s))
    check("_fmt_progress 50%: 00:30:00", "00:30:00" in s, repr(s))
    check("_fmt_progress 50%: 01:00:00", "01:00:00" in s, repr(s))

    # 0%
    s0 = _fmt_progress(0, 0.0, 3620.0)
    check("_fmt_progress 0%: バー空", "░░░░░░░░░░░░░░░░░░░░" in s0, repr(s0))

    # 100%
    s100 = _fmt_progress(100, 3620.0, 3620.0)
    check("_fmt_progress 100%: バー満", "████████████████████" in s100, repr(s100))

    # get_video_time: 非macOS または Chrome 未起動では (-1, -1)
    from platform_utils import get_video_time
    import platform
    if platform.system() != "Darwin":
        cur, total = get_video_time("example.com")
        check("get_video_time: 非macOS → (-1,-1)", (cur, total) == (-1.0, -1.0))
    else:
        check("get_video_time import (macOS)", True)

    # #11: _print_progress が TTY なら \r で同行更新、非TTYなら改行付き
    import io
    from unittest.mock import patch
    from main import _print_progress

    # 非TTY: 改行付き出力
    fake_stdout = io.StringIO()
    fake_stdout.isatty = lambda: False
    with patch("main.sys.stdout", fake_stdout):
        _print_progress(50, 1800.0, 3600.0)
    out = fake_stdout.getvalue()
    check("_print_progress 非TTY: 改行で終わる", out.endswith("\n"), repr(out[-30:]))
    check("_print_progress 非TTY: \\r を含まない", "\r" not in out, repr(out))

    # TTY: \r + 行クリア、改行なし
    fake_stdout2 = io.StringIO()
    fake_stdout2.isatty = lambda: True
    with patch("main.sys.stdout", fake_stdout2):
        _print_progress(50, 1800.0, 3600.0)
    out2 = fake_stdout2.getvalue()
    check("_print_progress TTY: \\r で始まる", out2.startswith("\r"), repr(out2[:10]))
    check("_print_progress TTY: \\033[K で行クリア", "\033[K" in out2, repr(out2[:10]))
    check("_print_progress TTY: 改行で終わらない", not out2.endswith("\n"), repr(out2[-20:]))


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


# ─── 10. video.ended=True で capture.stop() が1回のみ呼ばれてループ終了 ──────

def test_pct100_auto_stop() -> None:
    print("\n[10] pct >= 100: capture.stop() 1回のみ・ループ正常終了")
    import argparse, threading
    from unittest.mock import MagicMock, patch
    from main import _process_one_url

    stop_call_count = [0]

    def fake_stop():
        stop_call_count[0] += 1

    mock_capture = MagicMock()
    mock_capture.stop.side_effect = fake_stop

    mock_seg1 = MagicMock()
    mock_seg1.segment_id = 1
    mock_seg1.data = np.zeros(16000, dtype=np.float32)
    mock_seg1.start_time = 0.0
    mock_seg2 = MagicMock()
    mock_seg2.segment_id = 2
    mock_seg2.data = np.zeros(16000, dtype=np.float32)
    mock_seg2.start_time = 30.0
    mock_capture.segments.return_value = iter([mock_seg1, mock_seg2])

    mock_transcriber = MagicMock()
    mock_transcriber.transcribe.return_value = []

    args = argparse.Namespace(
        keep_active="chrome", verbose=False, timestamps=False,
        format="txt", output=None, model="tiny", language="ja",
        segment_duration=30.0, overlap=5.0,
        keep_interval=20.0, save_interval=60.0,
    )
    stop_event = threading.Event()
    current: dict = {"keep_alive": None, "capture": None}

    ended_seq = iter([False, True])

    with patch("main.navigate_to_url"), \
         patch("main.time.sleep"), \
         patch("main.get_active_tab_id", return_value=42), \
         patch("main.wait_for_page_ready", return_value="https://example.com/mod/scorm/player.php?id=42"), \
         patch("main.get_moodle_page_info", return_value=("テスト講義", "https://example.com")), \
         patch("main.create_capture", return_value=mock_capture), \
         patch("main.WindowKeepAlive", return_value=MagicMock()), \
         patch("main.trigger_video_play"), \
         patch("main.get_viewing_percentage", return_value=100), \
         patch("main.get_video_ended", side_effect=lambda *a, **kw: next(ended_seq, True)), \
         patch("main.get_video_time", return_value=(1800.0, 3600.0)), \
         patch("main.click_save_button"), \
         patch("main.click_exit_activity_button"), \
         patch("main._prompt_rename", side_effect=lambda p: p), \
         patch("main.OutputWriter"):
        _process_one_url(
            "https://example.com/mod/scorm/player.php?id=42",
            0, 2, mock_transcriber, args,
            0, "BlackHole 2ch", current, stop_event,
        )

    # ended 経路 + finally の二重呼び出しは実クラス側の冪等化で吸収する
    # （test_capture_stop_idempotent で検証）。ここでは呼ばれたことだけ確認。
    check("ended=True: capture.stop() が呼ばれた", stop_call_count[0] >= 1,
          f"実際の呼び出し回数: {stop_call_count[0]}")
    check("ended=True: stop_event は未設定（正常終了）", not stop_event.is_set())
    check("ended=True: transcriber.reset() が呼ばれた", mock_transcriber.reset.called)


# ─── 11. シングルURL _unique_path 適用確認 ────────────────────────────────────

def test_single_url_unique_path() -> None:
    print("\n[11] シングルURL: 同名ファイルに _2, _3 連番が付く")
    import argparse, threading, tempfile, os
    from unittest.mock import MagicMock, patch
    from pathlib import Path

    captured_paths: list[Path] = []

    def fake_output_writer(path, *args, **kwargs):
        captured_paths.append(path)
        m = MagicMock()
        m.__enter__ = lambda s: s
        m.__exit__ = MagicMock(return_value=False)
        return m

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir) / "20260101"
        base.mkdir()
        existing = base / "テスト講義.txt"
        existing.touch()

        args = argparse.Namespace(
            keep_active="chrome", verbose=False, timestamps=False,
            format="txt", output=None, model="tiny", language="ja",
            segment_duration=30.0, overlap=5.0,
            keep_interval=20.0, save_interval=60.0,
            moodle_url="example.com",
        )

        mock_capture = MagicMock()
        mock_capture.segments.return_value = iter([])
        mock_transcriber = MagicMock()
        mock_transcriber.transcribe.return_value = []

        with patch("main.navigate_to_url"), \
             patch("main.time.sleep"), \
             patch("main.get_active_tab_id", return_value=1), \
             patch("main.wait_for_page_ready", return_value="https://example.com/mod/scorm/player.php?id=1"), \
             patch("main.get_moodle_page_info", return_value=("テスト講義", "https://example.com")), \
             patch("main.create_capture", return_value=mock_capture), \
             patch("main.WindowKeepAlive", return_value=MagicMock()), \
             patch("main.trigger_video_play"), \
             patch("main.get_viewing_percentage", return_value=-1), \
             patch("main.OutputWriter", side_effect=fake_output_writer), \
             patch("main.Path") as mock_path_cls:

            from main import _process_one_url
            import threading
            stop_event = threading.Event()
            current: dict = {"keep_alive": None, "capture": None}

            # out/YYYYMMDD ディレクトリを tmpdir に向ける
            real_path = Path
            def patched_path(*args2, **kwargs2):
                p = real_path(*args2, **kwargs2)
                if str(p).startswith("out/"):
                    p = real_path(tmpdir) / real_path(*args2[1:] if len(args2) > 1 else args2)
                return p
            mock_path_cls.side_effect = patched_path

    # _unique_path の単体動作確認（シングルURL相当）
    from main import _unique_path
    with tempfile.TemporaryDirectory() as tmpdir2:
        p = Path(tmpdir2) / "テスト講義.txt"
        p.touch()
        p2 = _unique_path(p)
        check("_unique_path: 1件重複 → _2", p2.name == "テスト講義_2.txt", f"got={p2.name}")
        p2.touch()
        p3 = _unique_path(p)
        check("_unique_path: 2件重複 → _3", p3.name == "テスト講義_3.txt", f"got={p3.name}")
        p_new = Path(tmpdir2) / "新規講義.txt"
        check("_unique_path: 重複なし → そのまま", _unique_path(p_new) == p_new)


# ─── 12. _run_batch: 全URL順次処理 + URL間5秒待機 ───────────────────────────

def test_run_batch_all_urls() -> None:
    print("\n[12] _run_batch: 全URL順次処理 + URL間5秒待機")
    import argparse
    from unittest.mock import MagicMock, patch
    from main import _run_batch

    processed: list[tuple[str, int, int]] = []  # (url, idx, total)

    def fake_process(url, idx, total, transcriber, args, device_index, device_name, current, stop_event):
        processed.append((url, idx, total))

    urls = [
        "https://moodle.example.com/mod/scorm/player.php?id=101",
        "https://moodle.example.com/mod/scorm/player.php?id=102",
        "https://moodle.example.com/mod/scorm/player.php?id=103",
    ]
    args = argparse.Namespace(
        model="tiny", language="ja", cpu_threads=8,
        keep_active="chrome", verbose=False, timestamps=False,
        format="txt", output=None,
        segment_duration=30.0, overlap=5.0,
        keep_interval=20.0, save_interval=60.0,
        restore_to=None,
    )
    sleep_calls: list[float] = []

    with patch("transcriber.Transcriber", return_value=MagicMock()), \
         patch("main._process_one_url", side_effect=fake_process), \
         patch("main.time.sleep", side_effect=lambda s: sleep_calls.append(s)), \
         patch("main.restore_audio_output"), \
         patch("subprocess.run"):
        _run_batch(urls, args, 0, "BlackHole 2ch", False)

    check("3件全URL処理", [u for u, _, _ in processed] == urls,
          f"got={[u for u, _, _ in processed]}")
    check("idx が 0,1,2 の順", [i for _, i, _ in processed] == [0, 1, 2])
    check("total=3 で統一", all(t == 3 for _, _, t in processed))
    check("URL間に5秒待機が2回（最後はなし）", sleep_calls.count(5) == 2,
          f"5秒sleep回数: {sleep_calls.count(5)}")


# ─── 13. _run_batch: stop_event で途中停止 ───────────────────────────────────

def test_run_batch_stop_event() -> None:
    print("\n[13] _run_batch: Ctrl-C で1件目以降をスキップ")
    import argparse
    from unittest.mock import MagicMock, patch
    from main import _run_batch

    processed_urls: list[str] = []

    def fake_process(url, idx, total, transcriber, args, device_index, device_name, current, stop_event):
        processed_urls.append(url)
        stop_event.set()  # 1件目処理後に Ctrl-C をシミュレート

    urls = [
        "https://moodle.example.com/mod/scorm/player.php?id=101",
        "https://moodle.example.com/mod/scorm/player.php?id=102",
        "https://moodle.example.com/mod/scorm/player.php?id=103",
    ]
    args = argparse.Namespace(
        model="tiny", language="ja", cpu_threads=8,
        keep_active="chrome", verbose=False, timestamps=False,
        format="txt", output=None,
        segment_duration=30.0, overlap=5.0,
        keep_interval=20.0, save_interval=60.0,
        restore_to=None,
    )

    with patch("transcriber.Transcriber", return_value=MagicMock()), \
         patch("main._process_one_url", side_effect=fake_process), \
         patch("main.time.sleep"), \
         patch("main.restore_audio_output"), \
         patch("subprocess.run"):
        _run_batch(urls, args, 0, "BlackHole 2ch", False)

    check("Ctrl-C 後は1件のみ処理", len(processed_urls) == 1,
          f"処理件数: {len(processed_urls)}")
    check("処理されたのは1件目のみ", processed_urls == [urls[0]],
          f"got={processed_urls}")


# ─── 14. _process_one_url: navigate + save/exit ボタン呼び出し確認 ─────────

def test_process_one_url_navigate_and_buttons() -> None:
    print("\n[14] _process_one_url: navigate_to_url + click_save + click_exit が呼ばれる")
    import argparse, threading
    from unittest.mock import MagicMock, patch
    from main import _process_one_url

    target_url = "https://moodle.example.com/mod/scorm/player.php?id=99"

    mock_capture = MagicMock()
    mock_seg = MagicMock()
    mock_seg.segment_id = 1
    mock_seg.data = np.zeros(16000, dtype=np.float32)
    mock_seg.start_time = 0.0
    mock_capture.segments.return_value = iter([mock_seg])

    mock_transcriber = MagicMock()
    mock_transcriber.transcribe.return_value = []

    args = argparse.Namespace(
        keep_active="chrome", verbose=False, timestamps=False,
        format="txt", output=None, model="tiny", language="ja",
        segment_duration=30.0, overlap=5.0,
        keep_interval=20.0, save_interval=60.0,
    )
    stop_event = threading.Event()
    current: dict = {"keep_alive": None, "capture": None}

    navigate_calls: list[str] = []
    save_calls: list = []
    exit_calls: list = []

    with patch("main.navigate_to_url", side_effect=lambda url, *a, **kw: navigate_calls.append(url)), \
         patch("main.time.sleep"), \
         patch("main.get_active_tab_id", return_value=99), \
         patch("main.wait_for_page_ready", return_value=target_url), \
         patch("main.get_moodle_page_info", return_value=("テスト講義99", target_url)), \
         patch("main.create_capture", return_value=mock_capture), \
         patch("main.WindowKeepAlive", return_value=MagicMock()), \
         patch("main.trigger_video_play"), \
         patch("main.get_viewing_percentage", return_value=100), \
         patch("main.get_video_ended", return_value=True), \
         patch("main.get_video_time", return_value=(3600.0, 3600.0)), \
         patch("main.click_save_button", side_effect=lambda *a, **kw: save_calls.append(1)), \
         patch("main.click_exit_activity_button", side_effect=lambda *a, **kw: exit_calls.append(1)), \
         patch("main._prompt_rename", side_effect=lambda p: p), \
         patch("main.OutputWriter"):
        _process_one_url(
            target_url, 0, 2, mock_transcriber, args,
            0, "BlackHole 2ch", current, stop_event,
        )

    check("navigate_to_url が対象URLで呼ばれた", target_url in navigate_calls,
          f"navigate calls: {navigate_calls}")
    check("click_save_button が1回呼ばれた", len(save_calls) == 1,
          f"呼び出し回数: {len(save_calls)}")
    check("click_exit_activity_button が1回呼ばれた", len(exit_calls) == 1,
          f"呼び出し回数: {len(exit_calls)}")
    check("current が後片付けされた", current["keep_alive"] is None and current["capture"] is None)


def test_keep_active_only_mode() -> None:
    """#19: --no-transcribe モードで Transcriber/OutputWriter/Capture が使われないこと。"""
    print("\n[16] #19 --no-transcribe モード（keep-active専用）")
    import argparse, threading
    from unittest.mock import MagicMock, patch
    from main import _keep_active_one_url, _run_keep_active_batch, build_parser

    parser = build_parser()
    ns = parser.parse_args(["--no-transcribe", "--moodle-url", "https://m.example.com/x?id=1"])
    check("--no-transcribe フラグ存在", ns.no_transcribe is True)

    args = argparse.Namespace(
        keep_active="chrome",
        keep_interval=20.0, save_interval=60.0,
        segment_duration=30.0,
    )
    stop_event = threading.Event()
    current: dict = {"keep_alive": None}

    target_url = "https://moodle.example.com/mod/scorm/player.php?id=42"
    save_calls: list = []
    exit_calls: list = []

    with patch("main.navigate_to_url"), \
         patch("main.time.sleep"), \
         patch("main.get_active_tab_id", return_value=42), \
         patch("main.wait_for_page_ready", return_value=target_url), \
         patch("main.get_moodle_page_info", return_value=("講義42", target_url)), \
         patch("main.WindowKeepAlive", return_value=MagicMock()), \
         patch("main.trigger_video_play"), \
         patch("main.get_viewing_percentage", return_value=100), \
         patch("main.get_video_ended", return_value=True), \
         patch("main.get_video_time", return_value=(3600.0, 3600.0)), \
         patch("main.click_save_button", side_effect=lambda *a, **kw: save_calls.append(1)), \
         patch("main.click_exit_activity_button", side_effect=lambda *a, **kw: exit_calls.append(1)), \
         patch.object(stop_event, "wait", return_value=False):
        _keep_active_one_url(target_url, 0, 1, args, stop_event, current)

    check("ended検知で click_save_button が呼ばれた", len(save_calls) == 1,
          f"calls={len(save_calls)}")
    check("ended検知で click_exit_activity_button が呼ばれた", len(exit_calls) == 1,
          f"calls={len(exit_calls)}")
    check("keep_alive が後片付けされた", current["keep_alive"] is None)

    urls = ["https://m.example.com/x?id=1", "https://m.example.com/x?id=2"]
    args2 = argparse.Namespace(
        keep_active="chrome",
        keep_interval=20.0, save_interval=60.0,
        segment_duration=30.0,
    )
    processed: list[str] = []
    with patch("main._keep_active_one_url", side_effect=lambda u, i, t, a, s, c: processed.append(u)), \
         patch("main.time.sleep"):
        rc = _run_keep_active_batch(urls, args2)
    check("_run_keep_active_batch: 全URL処理", processed == urls, f"processed={processed}")
    check("_run_keep_active_batch: rc=0", rc == 0)


def test_no_transcribe_requires_url() -> None:
    """#19: --no-transcribe で URL 未指定だとエラー終了する。"""
    print("\n[17] #19 --no-transcribe で URL 必須")
    import argparse, io
    from unittest.mock import patch
    from main import run

    args = argparse.Namespace(
        no_transcribe=True,
        moodle_url=None, urls=None, url_file=None,
        keep_active=None,
        list_devices=False, reset_audio=False, restore_to=None,
        no_auto_routing=True,
    )
    with patch("sys.stderr", new_callable=io.StringIO):
        rc = run(args)
    check("--no-transcribe + URL未指定 → rc=1", rc == 1)


def test_main_hooks() -> None:
    """#4: main.py の _progress_hook / _rename_hook で CLI 既定動作を差し替えられる。"""
    print("\n[18] #4 main.py の hook 機構")
    from pathlib import Path as _Path
    import main as _m

    # progress hook: hook がセットされていればそちらに委譲
    captured: list[tuple[int, float, float]] = []
    _m._progress_hook = lambda p, c, t: captured.append((p, c, t))
    try:
        _m._print_progress(42, 1234.5, 3600.0)
    finally:
        _m._progress_hook = None
    check("_progress_hook 経由で受信", captured == [(42, 1234.5, 3600.0)],
          f"got={captured}")

    # rename hook: hook がセットされていれば input をスキップ
    rename_calls: list[_Path] = []

    def fake_rename(p: _Path) -> _Path:
        rename_calls.append(p)
        return p.parent / "renamed.txt"

    _m._rename_hook = fake_rename
    try:
        result = _m._prompt_rename(_Path("out/20260504/orig.txt"))
    finally:
        _m._rename_hook = None
    check("_rename_hook が呼ばれた", len(rename_calls) == 1)
    check("_rename_hook の戻り値で置換", result.name == "renamed.txt", f"got={result}")


def test_capture_stop_idempotent() -> None:
    """capture.stop() の冪等性: ended経路 + SIGINT の二重呼び出しでも
    末尾セグメントが重複しない / None センチネルが1つだけ入る。"""
    print("\n[19] AudioCapture.stop() 冪等性")
    import queue, threading
    from capture import AudioCapture

    cap = AudioCapture.__new__(AudioCapture)
    cap._native_rate = 16000
    cap._buffer = [np.zeros(20000, dtype=np.float32)]  # 1秒以上 → flush対象
    cap._buffer_size = 20000
    cap._samples_captured = 20000
    cap._segment_id = 0
    cap._queue = queue.Queue()
    cap._lock = threading.Lock()
    cap._stream = None
    cap._stopped = False

    cap.stop()
    cap.stop()  # 二重呼び出し

    items = []
    while not cap._queue.empty():
        items.append(cap._queue.get_nowait())
    segs = [i for i in items if i is not None]
    nones = [i for i in items if i is None]
    check("二重 stop でも末尾セグメントは1個", len(segs) == 1, f"segs={len(segs)}")
    check("None センチネルは1個", len(nones) == 1, f"nones={len(nones)}")
    check("flush 後バッファはクリア", cap._buffer_size == 0)


def test_transcriber_dedup_and_reset() -> None:
    """_prev_end の URL 間持ち越しバグ（バッチ2本目の冒頭欠落）の回帰テスト。"""
    print("\n[20] Transcriber の重複除去 / reset()")
    from collections import deque
    from transcriber import Transcriber

    t = Transcriber.__new__(Transcriber)
    t._prev_end = 0.0
    t._recent_texts = deque(maxlen=8)

    # 1本目: 0〜1800秒まで出力した状態を作る
    out1 = t._dedup([{"text": "一本目の最後の文です", "start": 1770.0, "end": 1800.0}], 0.0)
    check("1本目のセグメントは出力される", len(out1) == 1)
    check("_prev_end が進む", t._prev_end == 1800.0)

    # reset() しないと 2 本目冒頭 (abs 0〜30秒) は捨てられる（バグの再現）
    dropped = t._dedup([{"text": "二本目の冒頭です", "start": 0.0, "end": 30.0}], 0.0)
    check("reset なしだと冒頭が捨てられる（旧バグの再現）", len(dropped) == 0)

    # reset() すれば出力される
    t.reset()
    out2 = t._dedup([{"text": "二本目の冒頭です", "start": 0.0, "end": 30.0}], 0.0)
    check("reset 後は冒頭が出力される", len(out2) == 1, f"got={len(out2)}")

    # オーバーラップ境界の重複テキスト除去:
    # 30秒セグメント + 5秒オーバーラップで、次セグメントが同じ文を再転写するケース
    t.reset()
    t._dedup([{"text": "医師は他者を侵害する仕事です", "start": 25.0, "end": 30.0}], 0.0)
    dup = t._dedup([{"text": "医師は他者を侵害する仕事です。", "start": 0.0, "end": 5.5}], 25.0)
    check("境界の重複テキストは除去される", len(dup) == 0, f"got={[s.text for s in dup]}")

    # 連続セグメント（境界をまたがない）の同一文は誤除去しない
    t.reset()
    t._dedup([{"text": "大事なことなので繰り返します", "start": 0.0, "end": 5.0}], 0.0)
    rep = t._dedup([{"text": "大事なことなので繰り返します", "start": 0.0, "end": 5.0}], 5.0)
    check("境界をまたがない繰り返し文は保持", len(rep) == 1, f"got={len(rep)}")


def test_playback_monitor_fallbacks() -> None:
    """PlaybackMonitor: ended が取れなくても止まれるフォールバックの検証。"""
    print("\n[21] PlaybackMonitor フォールバック終了判定")
    from unittest.mock import patch
    from main import PlaybackMonitor

    # 1) video.ended=True で即終了
    with patch("main.get_viewing_percentage", return_value=50), \
         patch("main.get_video_ended", return_value=True), \
         patch("main.get_video_time", return_value=(900.0, 1800.0)), \
         patch("main._print_progress"):
        m = PlaybackMonitor(1, "chrome")
        stop, reason = m.poll(silent=False)
    check("ended=True → 即終了", stop and "ended" in reason, f"({stop}, {reason})")

    # 2) 全シグナル不能 (ended=None, pct=-1, cur=-1) でも無音継続で終了
    with patch("main.get_viewing_percentage", return_value=-1), \
         patch("main.get_video_ended", return_value=None), \
         patch("main.get_video_time", return_value=(-1.0, -1.0)), \
         patch("main._print_progress"):
        m = PlaybackMonitor(1, "chrome")
        m.poll(silent=False)  # 音あり
        results = [m.poll(silent=True) for _ in range(PlaybackMonitor.SILENCE_AFTER_AUDIO)]
    check("シグナル不能でも無音継続で終了", results[-1][0],
          f"last={results[-1]}")
    check("無音判定より前は継続", not results[0][0])

    # 3) currentTime が総尺付近で停滞 → 終了（ended が取れない SCORM 対策）
    with patch("main.get_viewing_percentage", return_value=-1), \
         patch("main.get_video_ended", return_value=None), \
         patch("main.get_video_time", return_value=(1799.5, 1800.0)), \
         patch("main._print_progress"):
        m = PlaybackMonitor(1, "chrome")
        r = [m.poll(silent=False) for _ in range(PlaybackMonitor.NEAR_END_POLLS)]
    check("総尺付近で停滞 → 終了", r[-1][0], f"last={r[-1]}")

    # 4) currentTime 停滞（途中で止まったまま）→ 終了
    with patch("main.get_viewing_percentage", return_value=-1), \
         patch("main.get_video_ended", return_value=None), \
         patch("main.get_video_time", return_value=(600.0, 1800.0)), \
         patch("main._print_progress"):
        m = PlaybackMonitor(1, "chrome")
        r = [m.poll(silent=False) for _ in range(PlaybackMonitor.STALL_POLLS + 1)]
    check("currentTime 停滞 → 終了", r[-1][0], f"last={r[-1]}")

    # 5) 正常再生中（cur が進む）は止まらない
    times = iter([(float(i * 30), 1800.0) for i in range(1, 11)])
    with patch("main.get_viewing_percentage", return_value=-1), \
         patch("main.get_video_ended", return_value=False), \
         patch("main.get_video_time", side_effect=lambda *a, **kw: next(times)), \
         patch("main._print_progress"):
        m = PlaybackMonitor(1, "chrome")
        r = [m.poll(silent=False, elapsed=i * 30.0) for i in range(1, 11)]
    check("正常再生中は止まらない", not any(s for s, _ in r), f"stops={[x for x in r if x[0]]}")

    # 6) 一度も音が出ない → リトライ後に諦めて終了
    retriggers: list = []
    with patch("main.get_viewing_percentage", return_value=-1), \
         patch("main.get_video_ended", return_value=None), \
         patch("main.get_video_time", return_value=(-1.0, -1.0)), \
         patch("main._print_progress"):
        m = PlaybackMonitor(1, "chrome", retrigger=lambda: retriggers.append(1))
        r = [m.poll(silent=True) for _ in range(PlaybackMonitor.SILENCE_NO_AUDIO)]
    check("無音時に再生リトライが1回打たれる", len(retriggers) == 1, f"retriggers={len(retriggers)}")
    check("音が出ないまま → 諦めて終了", r[-1][0], f"last={r[-1]}")


def test_keep_alive_ended_guard() -> None:
    """WindowKeepAlive: 終了検知後は注入しない（再ループ防止）。"""
    print("\n[22] WindowKeepAlive の終了ガード / target 指定")
    from unittest.mock import patch
    from platform_utils import WindowKeepAlive

    # target (タブid) / url_pattern(旧引数) の両方を受け付ける
    ka = WindowKeepAlive("chrome", target=123)
    check("target=タブid を受け付ける", ka._target == 123)
    ka2 = WindowKeepAlive("chrome", url_pattern="example.com")
    check("url_pattern (旧引数) 互換", ka2._target == "example.com")

    # get_video_ended が True を返したら以後の注入をスキップ
    injected: list = []
    with patch("platform_utils.get_video_ended", return_value=True), \
         patch("platform_utils._osascript", side_effect=lambda *a, **kw: injected.append(1)):
        ka3 = WindowKeepAlive("chrome", target=1)
        ka3._tick_macos()
        ka3._tick_macos()
    check("ended=True 後は JS 注入しない", len(injected) == 0, f"injected={len(injected)}")
    check("_ended フラグが立つ", ka3._ended)

    # mark_ended() でも注入停止
    with patch("platform_utils.get_video_ended", return_value=False), \
         patch("platform_utils._osascript", side_effect=lambda *a, **kw: injected.append(1)):
        ka4 = WindowKeepAlive("chrome", target=1)
        ka4.mark_ended()
        ka4._tick_macos()
    check("mark_ended() 後は注入しない", len(injected) == 0, f"injected={len(injected)}")

    # stop() は join する（スレッド未起動でも安全）
    ka5 = WindowKeepAlive("chrome", target=1)
    ka5.stop()
    check("stop() はスレッド未起動でも安全", True)


def test_video_ended_tristate() -> None:
    """get_video_ended: true/false/unknown の三値を返す。"""
    print("\n[23] get_video_ended 三値化")
    from unittest.mock import patch
    from platform_utils import get_video_ended

    with patch("platform_utils._run_js_in_tab", return_value="true"):
        check("'true' → True", get_video_ended(1) is True)
    with patch("platform_utils._run_js_in_tab", return_value="false"):
        check("'false' → False", get_video_ended(1) is False)
    with patch("platform_utils._run_js_in_tab", return_value="unknown"):
        check("'unknown' → None", get_video_ended(1) is None)
    with patch("platform_utils._run_js_in_tab", return_value=""):
        check("JS失敗 ('') → None", get_video_ended(1) is None)


def test_play_btn_pos_js_iframe_priority() -> None:
    """#13: _GET_PLAY_BTN_POS_JS が iframe 中心を最優先で返すこと（autoplay policy対策）。

    Node.js が利用可能な環境では実際に JS を評価して座標を検証する。
    """
    print("\n[15] #13 _GET_PLAY_BTN_POS_JS の iframe 中心優先")
    from platform_utils import _GET_PLAY_BTN_POS_JS

    # 必須: AppleScript 内に "..." で埋め込むためダブルクォート禁止
    check("_GET_PLAY_BTN_POS_JS にダブルクォート無し",
          '"' not in _GET_PLAY_BTN_POS_JS)

    # iframe 中心を最優先する分岐があること
    check("iframe を querySelectorAll で取得",
          "querySelectorAll('iframe')" in _GET_PLAY_BTN_POS_JS)
    check("最大面積の iframe を選ぶ",
          "bestArea" in _GET_PLAY_BTN_POS_JS)

    if shutil.which("node"):
        # 同オリジン iframe 1つあるページをモック
        node_script = f"""
            global.window = {{outerHeight:900,innerHeight:800,innerWidth:1280,screenX:0,screenY:0}};
            global.document = {{
                querySelectorAll: () => [{{
                    getBoundingClientRect: () => ({{left:200,top:150,width:1000,height:600}})
                }}],
                querySelector: () => null
            }};
            global.Math = Math;
            console.log(new Function('return ' + {_GET_PLAY_BTN_POS_JS!r})());
        """
        result = subprocess.run(
            ["node", "-e", node_script],
            capture_output=True, text=True, timeout=5,
        )
        out = result.stdout.strip()
        # 期待座標: x=screenX(0)+200+500=700, y=screenY(0)+cH(100)+150+300=550
        check("Node評価: iframe中心 → 700,550", out == "700,550", f"got={out!r}")


# ─── main ────────────────────────────────────────────────────────────────────


def test_tab_condition_string_compare() -> None:
    """#16: タブ id 照合は必ず文字列比較にする（docs/fix-plan-tab-id.md）。

    Chrome の `id of tab` は text で返るため、`id of t is 2144314444` の
    整数リテラル比較は一度も一致しない。既存テストは osascript をモックするので
    この障害を検出できなかった。ここでは生成される AppleScript の文面そのものと、
    （macOS なら）osacompile による構文検証を行う。Chrome は一切操作しない。
    """
    print("\n[25] _tab_condition の文字列比較 / タブ未発見の可視化")
    import platform
    import tempfile
    from pathlib import Path
    from unittest.mock import MagicMock, patch
    import platform_utils
    from platform_utils import _tab_condition, _run_js_in_tab

    big = 2144314444  # 実ログに出た Chrome のタブ id（2^29 超）
    line = _tab_condition(big)
    check("int: (id of t as string) で比較する", "(id of t as string)" in line, line.strip())
    check("int: id は引用符付き文字列リテラル", f'is "{big}"' in line, line.strip())
    check("int: 整数リテラル比較に戻っていない", f"is {big} then" not in line)
    sline = _tab_condition("player.php?a=123")
    check("str: URL 部分一致は従来どおり", sline.strip() == 'if URL of t contains "player.php?a=123" then', sline.strip())

    # _run_js_in_tab が生成するスクリプト全体を捕まえる（osascript は実行しない）
    captured: list[str] = []

    def fake_osascript(script: str, timeout: float = 10.0):
        captured.append(script)
        r = MagicMock(); r.returncode = 0; r.stdout = "hello\n"
        return r

    platform_utils._no_tab_warned.clear()
    with patch("platform_utils._osascript", side_effect=fake_osascript):
        out = _run_js_in_tab("document.title", big)
    check("通常時は stdout をそのまま返す", out == "hello", repr(out))
    script = captured[0]
    check("スクリプト末尾に __NO_TAB__ 番兵がある", 'return "__NO_TAB__"' in script)
    check("番兵は repeat の外（最後の end repeat より後）",
          'return "__NO_TAB__"' in script and "end repeat" in script
          and script.index('return "__NO_TAB__"') > script.rindex("end repeat"))

    if platform.system() == "Darwin" and shutil.which("osacompile"):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "s.applescript"
            src.write_text(script)
            r = subprocess.run(["osacompile", "-o", str(Path(d) / "s.scpt"), str(src)],
                               capture_output=True, text=True, timeout=30)
            check("生成 AppleScript が osacompile を通る（実行はしない）",
                  r.returncode == 0, r.stderr.strip()[:120])
    else:
        check("osacompile 構文検証", True, "スキップ (非macOS)")

    # タブ未発見: 番兵を受けたら "" を返し、target ごとに 1 回だけ警告する
    warnings: list = []

    def fake_no_tab(script: str, timeout: float = 10.0):
        r = MagicMock(); r.returncode = 0; r.stdout = "__NO_TAB__\n"
        return r

    platform_utils._no_tab_warned.clear()
    with patch("platform_utils._osascript", side_effect=fake_no_tab), \
         patch.object(platform_utils._logger, "warning", side_effect=lambda *a, **kw: warnings.append(a)):
        o1 = _run_js_in_tab("1", big)
        o2 = _run_js_in_tab("1", big)
        o3 = _run_js_in_tab("1", big + 1)
    check("タブ未発見時は '' を返す", o1 == "" and o2 == "" and o3 == "")
    check("警告は target ごとに 1 回だけ", len(warnings) == 2, f"warnings={len(warnings)}")
    check("警告文に target が含まれる", any(str(big) in str(w) for w in warnings))
    platform_utils._no_tab_warned.clear()


def test_trigger_video_play_verifies_playback() -> None:
    """#17: trigger_video_play は「クリック API が成功した」ではなく
    「video.paused が false になった」を実測して True を返す。"""
    print("\n[26] trigger_video_play の再生実測")
    from unittest.mock import patch, MagicMock
    import platform_utils
    from platform_utils import trigger_video_play

    if platform_utils.SYSTEM != "Darwin":
        check("trigger_video_play (macOS専用)", True, "スキップ (非macOS)")
        return

    def mk_run_js(before_play, after_play="playing"):
        """_VIDEO_PLAYING_JS は play() が呼ばれる前は before_play、後は after_play を返す。"""
        calls = {"play": 0, "state": 0}
        def _run(js, target, browser="chrome"):
            if js is platform_utils._GET_PLAY_BTN_POS_JS:
                return "942,744"
            if js is platform_utils._PLAY_VIDEO_JS:
                calls["play"] += 1
                return "playing"
            if js is platform_utils._VIDEO_PLAYING_JS:
                calls["state"] += 1
                return after_play if calls["play"] else before_play
            return ""
        return _run, calls

    ok_osa = MagicMock(); ok_osa.returncode = 0; ok_osa.stdout = "Google Chrome\n"
    # 1 poll = 0.5 秒進む疑似時計（_wait_playing の窓を確実に使い切る）
    import itertools
    fake_clock = lambda: patch("platform_utils.time.monotonic", side_effect=itertools.count(0.0, 0.5))

    # 1) クリック後に playing → True、play() は呼ばれない
    clicks: list = []
    run_js, calls = mk_run_js("playing")
    with patch("platform_utils._run_js_in_tab", side_effect=run_js), \
         patch("platform_utils._cg_click", side_effect=lambda x, y: clicks.append((x, y)) or True), \
         patch("platform_utils._osascript", return_value=ok_osa), \
         patch("platform_utils.time.sleep"), fake_clock():
        r = trigger_video_play(1)
    check("クリックで再生開始 → True", r is True)
    check("クリック座標が JS の返した位置", clicks == [(942, 744)], f"clicks={clicks}")
    check("クリックで始まれば play() は呼ばれない", calls["play"] == 0, f"play={calls['play']}")

    # 2) クリックでは始まらず play() で始まる → True（Step2 を実際に通す）
    run_js, calls = mk_run_js("paused", "playing")
    with patch("platform_utils._run_js_in_tab", side_effect=run_js), \
         patch("platform_utils._cg_click", return_value=True), \
         patch("platform_utils._osascript", return_value=ok_osa), \
         patch("platform_utils.time.sleep"), fake_clock():
        r = trigger_video_play(1)
    check("play() で再生開始 → True", r is True)
    check("play() が 1 回呼ばれた", calls["play"] == 1, f"play={calls['play']}")

    # 3) クリックも play() も効かない（autoplay policy 拒否）→ False
    run_js, calls = mk_run_js("paused", "paused")
    with patch("platform_utils._run_js_in_tab", side_effect=run_js), \
         patch("platform_utils._cg_click", return_value=True), \
         patch("platform_utils._osascript", return_value=ok_osa), \
         patch("platform_utils.time.sleep"), fake_clock():
        r = trigger_video_play(1)
    check("再生が始まらなければ False（旧実装は True を返していた）", r is False)
    check("False でも play() は試みている", calls["play"] == 1, f"play={calls['play']}")
    check("False は有限回のポーリングで確定（無限ループではない）", calls["state"] <= 12, f"state_polls={calls['state']}")

    # 4) CG クリック失敗時は System Events にフォールバックする
    osa_calls: list = []
    def rec_osa(script, timeout=10.0):
        osa_calls.append(script); return ok_osa
    run_js, _ = mk_run_js("playing")
    with patch("platform_utils._run_js_in_tab", side_effect=run_js), \
         patch("platform_utils._cg_click", return_value=False), \
         patch("platform_utils._osascript", side_effect=rec_osa), \
         patch("platform_utils.time.sleep"), fake_clock():
        r = trigger_video_play(1)
    check("CG 失敗時は System Events click にフォールバック",
          any("click at {942, 744}" in c for c in osa_calls))

    # 4b) System Events フォールバックも失敗 → False / 座標が取れない → False
    bad_osa = MagicMock(); bad_osa.returncode = 1; bad_osa.stdout = ""
    def osa_click_fails(script, timeout=10.0):
        return bad_osa if "click at" in script else ok_osa
    run_js, _ = mk_run_js("playing")
    with patch("platform_utils._run_js_in_tab", side_effect=run_js), \
         patch("platform_utils._cg_click", return_value=False), \
         patch("platform_utils._osascript", side_effect=osa_click_fails), \
         patch("platform_utils.time.sleep"), fake_clock():
        r = trigger_video_play(1)
    check("フォールバックも失敗 → False", r is False)
    with patch("platform_utils._run_js_in_tab", return_value=""), \
         patch("platform_utils._cg_click", return_value=True):
        check("座標が取れない → False", trigger_video_play(1) is False)

    # 5) _cg_click 本体: subprocess.run の呼び方と戻り値の契約
    from platform_utils import _cg_click
    runs: list = []
    def fake_run(argv, capture_output=True, text=True, timeout=10):
        runs.append(argv)
        m = MagicMock(); m.returncode = 0; m.stdout = "ok\n"; m.stderr = ""
        return m
    with patch("platform_utils.subprocess.run", side_effect=fake_run):
        ok = _cg_click(942, 744)
    check("_cg_click: rc=0 & 'ok' → True", ok is True)
    argv = runs[0]
    check("_cg_click: osascript -l JavaScript -e <jxa> で呼ぶ",
          argv[:4] == ["osascript", "-l", "JavaScript", "-e"] and len(argv) == 5, f"argv={argv[:4]}")
    check("_cg_click: 座標が JXA に埋め込まれる", "CGPointMake(942, 744)" in argv[-1])
    check("_cg_click: CGEventPost を使う", "CGEventPost" in argv[-1])

    def run_rc1(argv, **kw):
        m = MagicMock(); m.returncode = 1; m.stdout = ""; m.stderr = "err"; return m
    with patch("platform_utils.subprocess.run", side_effect=run_rc1):
        check("_cg_click: rc≠0 → False", _cg_click(1, 2) is False)
    def run_no_ok(argv, **kw):
        m = MagicMock(); m.returncode = 0; m.stdout = ""; m.stderr = ""; return m
    with patch("platform_utils.subprocess.run", side_effect=run_no_ok):
        check("_cg_click: stdout に ok 無し → False", _cg_click(1, 2) is False)
    import subprocess as _sp
    with patch("platform_utils.subprocess.run", side_effect=_sp.TimeoutExpired("osascript", 10)):
        check("_cg_click: TimeoutExpired → False", _cg_click(1, 2) is False)
    with patch("platform_utils.subprocess.run", side_effect=OSError("boom")):
        check("_cg_click: OSError → False", _cg_click(1, 2) is False)

    # 6) JXA の構文を osacompile で検証（コンパイルのみ・イベントは投稿しない）
    if shutil.which("osacompile"):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "c.js"
            src.write_text(platform_utils._CG_CLICK_JXA % (1, 2))
            r = subprocess.run(["osacompile", "-l", "JavaScript", "-o", str(Path(d) / "c.scpt"), str(src)],
                               capture_output=True, text=True, timeout=30)
            check("JXA が osacompile -l JavaScript を通る", r.returncode == 0, r.stderr.strip()[:120])
    else:
        check("JXA 構文検証", True, "スキップ (osacompile なし)")


def test_playback_monitor_not_started() -> None:
    """#18: currentTime が 0 のまま → 「停滞終了」ではなく再試行→中止。played=False。"""
    print("\n[27] PlaybackMonitor 再生未開始の扱い")
    from unittest.mock import patch
    from main import PlaybackMonitor

    retriggers: list = []
    with patch("main.get_viewing_percentage", return_value=0), \
         patch("main.get_video_ended", return_value=False), \
         patch("main.get_video_time", return_value=(0.0, 262.0)), \
         patch("main._print_progress"):
        m = PlaybackMonitor(1, "chrome", retrigger=lambda: retriggers.append(1))
        r = [m.poll(silent=False, elapsed=i * 30.0) for i in range(1, PlaybackMonitor.NOT_STARTED_GIVEUP + 1)]
    early = r[:PlaybackMonitor.STALL_POLLS + 1]
    check("cur=0 のままでは STALL_POLLS で『停滞終了』しない", not any(s for s, _ in early),
          f"stops={[x for x in early if x[0]]}")
    check("未開始なら再生リトライが打たれる", len(retriggers) == PlaybackMonitor.NOT_STARTED_MAX_RETRY,
          f"retriggers={len(retriggers)}")
    check("未開始のまま → 中止で終了", r[-1][0] and "中止" in r[-1][1], f"last={r[-1]}")
    check("played=False のまま", m.played is False)

    # 再生が始まれば played=True になり、以後は通常の停滞判定に戻る
    times = iter([(0.0, 262.0), (0.0, 262.0), (30.0, 262.0), (60.0, 262.0), (60.0, 262.0),
                  (60.0, 262.0), (60.0, 262.0), (60.0, 262.0), (60.0, 262.0)])
    with patch("main.get_viewing_percentage", return_value=0), \
         patch("main.get_video_ended", return_value=False), \
         patch("main.get_video_time", side_effect=lambda *a, **kw: next(times)), \
         patch("main._print_progress"):
        m2 = PlaybackMonitor(1, "chrome")
        r2 = [m2.poll(silent=False, elapsed=i * 30.0) for i in range(9)]
    check("再生開始後は played=True", m2.played is True)
    check("再生開始後の停滞は従来どおり終了", r2[-1][0] and "停滞" in r2[-1][1], f"last={r2[-1]}")

    # 境界値: cur=0.9 は未開始、cur=1.0 で開始済み
    for cur, expect in ((0.9, False), (1.0, True)):
        with patch("main.get_viewing_percentage", return_value=0), \
             patch("main.get_video_ended", return_value=False), \
             patch("main.get_video_time", return_value=(cur, 262.0)), \
             patch("main._print_progress"):
            mb = PlaybackMonitor(1, "chrome"); mb.poll(silent=False)
        check(f"cur={cur} → played={expect}", mb.played is expect)

    # ended=True なら cur が取れなくても played=True（保存/離脱を押す）
    with patch("main.get_viewing_percentage", return_value=-1), \
         patch("main.get_video_ended", return_value=True), \
         patch("main.get_video_time", return_value=(-1.0, -1.0)), \
         patch("main._print_progress"):
        me = PlaybackMonitor(1, "chrome"); stop, reason = me.poll(silent=False)
    check("ended=True & cur 不明 → 終了かつ played=True", stop and me.played is True)

    # 既視聴 (pct=100) で再生が始まらない → 4 分待たず PCT100_POLLS で終了、played=True
    with patch("main.get_viewing_percentage", return_value=100), \
         patch("main.get_video_ended", return_value=False), \
         patch("main.get_video_time", return_value=(0.0, 262.0)), \
         patch("main._print_progress"):
        mp = PlaybackMonitor(1, "chrome")
        rp = [mp.poll(silent=False) for _ in range(PlaybackMonitor.PCT100_POLLS)]
    check("pct=100 & 未開始 → 視聴状況 100% で終了", rp[-1][0] and "100%" in rp[-1][1], f"last={rp[-1]}")
    check("pct=100 終了は played=True（保存/離脱を押す）", mp.played is True)


def test_downmix_antiphase() -> None:
    """#19: 逆相ステレオ (L ≈ -R) を平均で打ち消さない。"""
    print("\n[28] 逆相ステレオのダウンミックス")
    from capture import downmix_to_mono
    sr = 16000
    t = np.arange(sr) / sr
    sig = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    rms = lambda a: float(np.sqrt(np.mean(np.square(a))))

    inphase = np.stack([sig, sig], axis=1)
    m = downmix_to_mono(inphase)
    check("同相: 平均と一致", np.allclose(m, sig, atol=1e-6))

    anti = np.stack([sig, -sig], axis=1)
    m2 = downmix_to_mono(anti)
    check("逆相: 打ち消されない（RMS が片側と同等）", abs(rms(m2) - rms(sig)) < 0.01, f"rms={rms(m2):.4f}")
    check("旧実装 (mean) なら無音になっていた", rms(anti.mean(axis=1)) < 1e-6)

    mono_in = sig.reshape(-1, 1)
    check("1ch 入力はそのまま", np.allclose(downmix_to_mono(mono_in), sig))
    check("1次元入力はそのまま", downmix_to_mono(sig) is sig)

    # AudioCapture._callback 経由でも逆相が保持される
    from unittest.mock import MagicMock, patch
    mock_sd = MagicMock()
    mock_sd.query_devices.return_value = {"default_samplerate": 16000.0, "max_input_channels": 2}
    with patch.dict(sys.modules, {"sounddevice": mock_sd}):
        import importlib, capture as cap
        importlib.reload(cap)
        c = cap.AudioCapture(device_index=0, segment_duration=1.0, overlap_duration=0.0)
        c._callback(np.stack([sig, -sig], axis=1), len(sig), None, None)
        seg = c._queue.get(timeout=1)
    check("_callback 経由でも逆相セグメントの RMS が保たれる", rms(seg.data) > 0.15, f"rms={rms(seg.data):.4f}")

    # 閾値 2.0 を両側から固定する
    voice = sig
    amb = (0.35 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
    # (i) 中央ボイス + 逆相アンビエンス: dot<0 だが side/mid≈1.17 → mid（ボイス保持）
    part = np.stack([voice + amb, voice - amb], axis=1)
    check("部分逆相 (side/mid≈1.2) は mid を選びボイスを保持", np.allclose(downmix_to_mono(part), voice, atol=1e-5))
    # (ii) 実測相当 (R ≈ -0.74 L, side/mid≈6.7) → side
    real = np.stack([voice, -0.74 * voice], axis=1)
    check("実測相当の逆相 (side/mid≈6.7) は side を選ぶ",
          np.allclose(downmix_to_mono(real), (voice - (-0.74 * voice)) * 0.5, atol=1e-5))
    # (iii) 境界: k=0.32 → side/mid≈1.94 → mid / k=0.34 → ≈2.03 → side
    lo = np.stack([voice, -0.32 * voice], axis=1)
    hi = np.stack([voice, -0.34 * voice], axis=1)
    check("境界直下 (≈1.94) は mid", np.allclose(downmix_to_mono(lo), (voice - 0.32 * voice) * 0.5, atol=1e-5))
    check("境界直上 (≈2.03) は side", np.allclose(downmix_to_mono(hi), (voice + 0.34 * voice) * 0.5, atol=1e-5))
    # (iv) 無相関 L/R → mid
    g = np.random.default_rng(0)
    xl = (g.standard_normal(sr) * 0.1).astype(np.float32); xr = (g.standard_normal(sr) * 0.1).astype(np.float32)
    check("無相関 L/R は mid", np.allclose(downmix_to_mono(np.stack([xl, xr], axis=1)), (xl + xr) * 0.5, atol=1e-6))
    # (v) 3ch 以上は全 ch 平均
    four = np.stack([voice, voice, np.zeros_like(voice), np.zeros_like(voice)], axis=1)
    check("4ch 入力は全 ch 平均", np.allclose(downmix_to_mono(four), voice * 0.5, atol=1e-6))


def test_boundary_dedup_fuzzy() -> None:
    """#20: 境界をまたぐ語尾ゆれ（終わりました/終わります）を重複とみなす。"""
    print("\n[29] 境界重複のあいまい一致")
    from transcriber import _similar, _norm_text, Transcriber
    a = _norm_text("以上ガイダンスの説明は終わりました")
    b = _norm_text("以上ガイダンスの説明は終わります")
    check("語尾ゆれは類似", _similar(a, b))
    check("短い文は類似扱いしない", not _similar("はい", "はい。ええ"))
    check("別内容は類似しない", not _similar(_norm_text("授業の概要について説明します"),
                                            _norm_text("評価の方法は授業への参加状況")))

    from collections import deque
    tr = Transcriber.__new__(Transcriber)
    tr._prev_end = 0.0
    tr._recent_texts = deque(maxlen=8)
    out1 = tr._dedup([{"text": "以上ガイダンスの説明は終わりました", "start": 20.0, "end": 24.0}], 180.0)
    # 次セグメント（オフセット 200, オーバーラップ 5 秒）の境界に語尾ゆれで再出現
    out2 = tr._dedup([{"text": "以上ガイダンスの説明は終わります", "start": 0.5, "end": 3.5},
                      {"text": "皆さんはこれから学習を進めてください", "start": 4.0, "end": 8.0}], 200.0)
    check("1本目は出力", len(out1) == 1)
    check("境界の語尾ゆれ重複は除去、続きは出力", [o.text for o in out2] == ["皆さんはこれから学習を進めてください"],
          f"{[o.text for o in out2]}")

    import difflib
    ratio = lambda x, y: difflib.SequenceMatcher(None, _norm_text(x), _norm_text(y)).ratio()
    base = "以上ガイダンスの説明は終わりました"
    below = "以上ガイダンスの説明は終了です次へ"            # ratio ≈ 0.71 (< 0.75)
    above = "ガイダンスの説明は終わりました次は評価です"     # ratio ≈ 0.79 (> 0.75)
    check("閾値直下の対は類似しない", ratio(base, below) < 0.75 and not _similar(_norm_text(base), _norm_text(below)),
          f"ratio={ratio(base, below):.3f}")
    check("閾値直上の対は類似", ratio(base, above) > 0.75 and _similar(_norm_text(base), _norm_text(above)),
          f"ratio={ratio(base, above):.3f}")

    # 末尾 + 新規内容（ratio 0.5）は境界でも保持される
    tr2 = Transcriber.__new__(Transcriber); tr2._prev_end = 0.0; tr2._recent_texts = deque(maxlen=8)
    tr2._dedup([{"text": base, "start": 20.0, "end": 24.0}], 180.0)
    kept = tr2._dedup([{"text": "説明は終わりました次は評価についてです", "start": 0.5, "end": 6.0}], 200.0)
    check("境界で ratio≈0.5 の『末尾+新規内容』は保持", [o.text for o in kept] == ["説明は終わりました次は評価についてです"])

    # 一文字違いの別文 (ratio 0.8) でも境界をまたがなければ保持
    tr3 = Transcriber.__new__(Transcriber); tr3._prev_end = 0.0; tr3._recent_texts = deque(maxlen=8)
    tr3._dedup([{"text": "ここで出席を取ります", "start": 0.0, "end": 3.0}], 0.0)
    kept3 = tr3._dedup([{"text": "ここで休憩を取ります", "start": 0.0, "end": 3.0}], 10.0)
    check("非境界の類似文は保持（連続セグメント）", [o.text for o in kept3] == ["ここで休憩を取ります"])

    # 最小長 8: 7 文字は包含でも非類似、8 文字で包含は類似
    check("7 文字の包含は非類似", not _similar("評価の方法は授", "評価の方法は授業への"))
    check("8 文字の包含は類似", _similar("評価の方法は授業", "評価の方法は授業への"))


def test_playback_monitor_rewind_after_end() -> None:
    """#21: 末尾付近まで再生後に currentTime が 0 へ戻ったら即終了。"""
    print("\n[30] 末尾到達後の巻き戻し検出")
    from unittest.mock import patch
    from main import PlaybackMonitor
    times = iter([(30.0, 262.0), (120.0, 262.0), (258.0, 262.0), (0.0, 262.0)])
    with patch("main.get_viewing_percentage", return_value=50), \
         patch("main.get_video_ended", return_value=False), \
         patch("main.get_video_time", side_effect=lambda *a, **kw: next(times)), \
         patch("main._print_progress"):
        m = PlaybackMonitor(1, "chrome")
        r = [m.poll(silent=False, elapsed=i * 30.0) for i in range(4)]
    check("巻き戻し直後に終了", r[-1][0] and "巻き戻し" in r[-1][1], f"last={r[-1]}")
    check("それ以前は継続", not any(s for s, _ in r[:3]))

    # 途中（末尾未到達）で 0 に戻った場合は巻き戻し終了にしない
    times2 = iter([(30.0, 262.0), (60.0, 262.0), (0.0, 262.0)])
    with patch("main.get_viewing_percentage", return_value=20), \
         patch("main.get_video_ended", return_value=False), \
         patch("main.get_video_time", side_effect=lambda *a, **kw: next(times2)), \
         patch("main._print_progress"):
        m2 = PlaybackMonitor(1, "chrome")
        r2 = [m2.poll(silent=False, elapsed=i * 30.0) for i in range(3)]
    check("末尾未到達の 0 戻りでは終了しない", not r2[-1][0], f"last={r2[-1]}")


def test_callers_skip_save_exit_when_not_played() -> None:
    """#22: 未再生で中止したとき、呼び出し側 3 経路が保存/離脱を押さない（対照: 再生済みは押す）。"""
    print("\n[31] 未再生時は保存/離脱クリックをスキップ（_process_one_url / _keep_active_one_url / run）")
    import argparse, threading, tempfile
    from unittest.mock import MagicMock, patch
    from main import _process_one_url, _keep_active_one_url, PlaybackMonitor, build_parser

    url = "https://example.com/mod/scorm/player.php?id=42"

    def run_process(video_time, ended, pct):
        n_seg = PlaybackMonitor.NOT_STARTED_GIVEUP + 2
        segs = []
        for i in range(n_seg):
            m = MagicMock(); m.segment_id = i; m.data = np.zeros(16000, dtype=np.float32); m.start_time = 30.0 * i
            segs.append(m)
        cap = MagicMock(); cap.segments.return_value = iter(segs)
        ka = MagicMock()
        tr = MagicMock(); tr.transcribe.return_value = []
        args = argparse.Namespace(keep_active="chrome", verbose=False, timestamps=False, format="txt",
                                  output=None, model="tiny", language="ja", segment_duration=30.0,
                                  overlap=5.0, keep_interval=20.0, save_interval=60.0)
        saves: list = []; exits: list = []; triggers: list = []
        with tempfile.TemporaryDirectory() as d, \
             patch("main.navigate_to_url"), patch("main.time.sleep"), \
             patch("main.get_active_tab_id", return_value=42), \
             patch("main.wait_for_page_ready", return_value=url), \
             patch("main.get_moodle_page_info", return_value=("講義", url)), \
             patch("main.create_capture", return_value=cap), \
             patch("main.WindowKeepAlive", return_value=ka), \
             patch("main._trigger_with_retry", side_effect=lambda *a, **kw: triggers.append(1)), \
             patch("main.get_viewing_percentage", return_value=pct), \
             patch("main.get_video_ended", return_value=ended), \
             patch("main.get_video_time", return_value=video_time), \
             patch("main.click_save_button", side_effect=lambda *a, **kw: saves.append(1)), \
             patch("main.click_exit_activity_button", side_effect=lambda *a, **kw: exits.append(1)), \
             patch("main._prompt_rename", side_effect=lambda p: p), \
             patch("main.OutputWriter"), patch("main._print_progress"):
            args.output = d
            entry = _process_one_url(url, 0, 1, tr, args, 0, "BlackHole 2ch", {"keep_alive": None, "capture": None},
                                     threading.Event())
        return saves, exits, triggers, ka, cap, entry

    saves, exits, triggers, ka, cap, entry = run_process((0.0, 262.0), False, 0)
    check("_process_one_url 未再生: entry.qa.status == failed（monitor.played が QA に渡る）",
          entry["qa"]["status"] == "failed" and entry["played"] is False, f"qa={entry['qa']['status']}")
    check("_process_one_url 未再生: 保存クリック 0 回", len(saves) == 0, f"saves={len(saves)}")
    check("_process_one_url 未再生: 離脱クリック 0 回", len(exits) == 0, f"exits={len(exits)}")
    check("_process_one_url 未再生: 再生リトライが打たれた（初回 + 再試行）",
          len(triggers) == 1 + PlaybackMonitor.NOT_STARTED_MAX_RETRY, f"triggers={len(triggers)}")
    check("_process_one_url 未再生: keep_alive.mark_ended/stop は呼ばれる", ka.mark_ended.called and ka.stop.called)
    check("_process_one_url 未再生: capture.stop は呼ばれる", cap.stop.called)

    saves, exits, _, _, _, entry_ok = run_process((3600.0, 3600.0), True, 100)
    check("_process_one_url 再生済み(対照): 保存 1 回・離脱 1 回", len(saves) == 1 and len(exits) == 1,
          f"saves={len(saves)} exits={len(exits)}")

    # ── _keep_active_one_url ──
    def run_keep(video_time, ended, pct):
        args = argparse.Namespace(keep_active="chrome", keep_interval=20.0, save_interval=60.0, segment_duration=30.0)
        ev = threading.Event(); saves: list = []; exits: list = []; ka = MagicMock()
        waits = {"n": 0}
        def bounded_wait(timeout=None):
            waits["n"] += 1
            return waits["n"] > PlaybackMonitor.NOT_STARTED_GIVEUP + 3  # 止まらない回帰はハングでなく脱出
        with patch("main.navigate_to_url"), patch("main.time.sleep"), \
             patch("main.get_active_tab_id", return_value=42), \
             patch("main.wait_for_page_ready", return_value=url), \
             patch("main.get_moodle_page_info", return_value=("講義", url)), \
             patch("main.WindowKeepAlive", return_value=ka), \
             patch("main._trigger_with_retry"), \
             patch("main.get_viewing_percentage", return_value=pct), \
             patch("main.get_video_ended", return_value=ended), \
             patch("main.get_video_time", return_value=video_time), \
             patch("main.click_save_button", side_effect=lambda *a, **kw: saves.append(1)), \
             patch("main.click_exit_activity_button", side_effect=lambda *a, **kw: exits.append(1)), \
             patch("main._print_progress"), \
             patch.object(ev, "wait", side_effect=bounded_wait):
            _keep_active_one_url(url, 0, 1, args, ev, {"keep_alive": None})
        return saves, exits, ka

    saves, exits, ka = run_keep((0.0, 262.0), False, 0)
    check("_keep_active_one_url 未再生: 保存/離脱 0 回", len(saves) == 0 and len(exits) == 0,
          f"saves={len(saves)} exits={len(exits)}")
    check("_keep_active_one_url 未再生: 停止分岐に到達（mark_ended）", ka.mark_ended.called)
    check("_keep_active_one_url 未再生: keep_alive.stop は呼ばれる", ka.stop.called)
    saves, exits, _ = run_keep((3600.0, 3600.0), True, 100)
    check("_keep_active_one_url 再生済み(対照): 保存/離脱 1 回ずつ", len(saves) == 1 and len(exits) == 1)

    # ── run() シングルモード（モニタループを通す） ──
    def run_single(video_time, ended, pct):
        from main import run
        n_seg = PlaybackMonitor.NOT_STARTED_GIVEUP + 2
        segs = []
        for i in range(n_seg):
            m = MagicMock(); m.segment_id = i; m.data = np.zeros(16000, dtype=np.float32); m.start_time = 30.0 * i
            segs.append(m)
        cap = MagicMock(); cap.segments.return_value = iter(segs)
        saves: list = []; exits: list = []; ka = MagicMock()
        sd_mod = MagicMock(); sd_mod.query_devices.return_value = {"name": "BlackHole 2ch"}
        from contextlib import ExitStack
        patches = [
            lambda: patch.dict(sys.modules, {"sounddevice": sd_mod}),
            lambda: patch("main.warn_other_audio_apps", return_value=[]),
            lambda: patch("main.find_loopback_device", return_value=0),
            lambda: patch("main.get_tab_id_by_pattern", return_value=42),
            lambda: patch("main.get_tab_url", return_value=url),
            lambda: patch("main.get_moodle_page_info", return_value=("講義", url)),
            lambda: patch("main.create_capture", return_value=cap),
            lambda: patch("main.WindowKeepAlive", return_value=ka),
            lambda: patch("main._trigger_with_retry"),
            lambda: patch("main.get_viewing_percentage", return_value=pct),
            lambda: patch("main.get_video_ended", return_value=ended),
            lambda: patch("main.get_video_time", return_value=video_time),
            lambda: patch("main.click_save_button", side_effect=lambda *a, **kw: saves.append(1)),
            lambda: patch("main.click_exit_activity_button", side_effect=lambda *a, **kw: exits.append(1)),
            lambda: patch("main._prompt_rename", side_effect=lambda p: p),
            lambda: patch("main.OutputWriter"), lambda: patch("main._print_progress"),
            lambda: patch("main.time.sleep"), lambda: patch("main.signal.signal"),
            lambda: patch("transcriber.Transcriber", return_value=MagicMock(transcribe=MagicMock(return_value=[]))),
        ]
        with tempfile.TemporaryDirectory() as d, ExitStack() as stack:
            args = build_parser().parse_args(["--engine", "blackhole", "--moodle-url", "example.com",
                                              "--no-auto-routing", "--output", d, "--model", "tiny"])
            for mk in patches:
                stack.enter_context(mk())
            rc = run(args)
        return rc, saves, exits, ka

    rc, saves, exits, ka = run_single((0.0, 262.0), False, 0)
    check("run() 未再生: rc=0 で終了", rc == 0, f"rc={rc}")
    check("run() 未再生: 保存/離脱 0 回", len(saves) == 0 and len(exits) == 0, f"saves={len(saves)} exits={len(exits)}")
    check("run() 未再生: 停止分岐に到達（keep_alive.mark_ended）", ka.mark_ended.called)
    rc, saves, exits, _ = run_single((3600.0, 3600.0), True, 100)
    check("run() 再生済み(対照): 保存/離脱 1 回ずつ", len(saves) == 1 and len(exits) == 1,
          f"saves={len(saves)} exits={len(exits)}")


def test_qa_metrics() -> None:
    """#23: 自己診断（qa_metrics）の判定。既往の 3 障害が failed/warn として検出されること。"""
    print("\n[32] qa_metrics 自己診断")
    from qa import qa_metrics, MIN_CHARS_PER_MIN

    good = [f"これは{i}番目の講義内容の説明です。詳しく話します。" for i in range(60)]
    q = qa_metrics(good, 262.0, played=True, stop_reason="video.ended")
    check("正常: status=ok", q["status"] == "ok", f"{q['status']} {q['warnings']}")
    check("正常: 行数・字/分が入る", q["lines"] == 60 and q["chars_per_min"] > MIN_CHARS_PER_MIN)

    q = qa_metrics(["ご視聴ありがとうございました"], 262.0, played=True, stop_reason="再生停止（currentTime 停滞）")
    check("幻覚 1 行のみ → failed（逆相ステレオ事故の形）", q["status"] == "failed", f"{q['warnings']}")

    q = qa_metrics([], 262.0, played=False, stop_reason="再生を開始できないため中止")
    check("未再生・空 → failed（クリック事故の形）", q["status"] == "failed")
    check("未再生の警告文がある", any("再生されなかった" in w for w in q["warnings"]))

    q = qa_metrics(["短い"] * 3, 1800.0, played=True)
    check("30 分で数行 → warn（少なすぎる）", q["status"] == "warn" and any("少なすぎる" in w for w in q["warnings"]))

    rep = ["同じ文が連発しています。幻覚かもしれません。"] * 20 + good[:10]
    q = qa_metrics(rep, 262.0, played=True)
    check("同一文の連発 → warn", q["status"] == "warn" and any("連発" in w for w in q["warnings"]), f"{q['warnings']}")

    q = qa_metrics(good, 262.0, played=True, stop_reason="音声が検出できないため中止")
    check("内容は十分でも中止理由なら warn", q["status"] == "warn")

    q = qa_metrics(good, 0.0, played=True)
    check("総尺不明でも落ちない（字/分 0・ok）", q["status"] == "ok" and q["chars_per_min"] == 0)


def test_run_batch_summary() -> None:
    """#24: _run_batch が run-summary.json を書き、failed があれば rc=2、全 URL 分のエントリを持つ。"""
    print("\n[33] _run_batch の run-summary.json と終了コード")
    import argparse, json, tempfile
    from unittest.mock import MagicMock, patch
    from pathlib import Path
    from main import _run_batch

    urls = ["https://m.example.com/x?a=1", "https://m.example.com/x?a=2", "https://m.example.com/x?a=3"]

    def make_runner(outdir, statuses):
        def _one(url, i, total, tr, args, di, dn, current, stop_event):
            st = statuses[i]
            return {"index": i, "url": url, "page_url": url, "title": f"講義{i}",
                    "output": str(Path(outdir) / f"講義{i}.txt"), "stop_reason": "video.ended",
                    "played": st != "failed", "elapsed_seconds": 1.0,
                    "qa": {"status": st, "lines": 10, "chars": 300, "video_seconds": 60.0,
                           "chars_per_min": 300.0, "repeated_ratio": 0.0, "hallucination_ratio": 0.0,
                           "warnings": [] if st == "ok" else ["x"]}}
        return _one

    with tempfile.TemporaryDirectory() as d:
        args = argparse.Namespace(model="tiny", language="ja", cpu_threads=4, restore_to=None, json_summary=None, output=d)
        with patch("main._process_one_url", side_effect=make_runner(d, ["ok", "ok", "ok"])), \
             patch("main.time.sleep"), patch("main.signal.signal"), \
             patch("transcriber.Transcriber", return_value=MagicMock()), \
             patch("subprocess.run"):
            rc = _run_batch(urls, args, 0, "BlackHole", False)
        summary = Path(d) / "run-summary.json"
        check("全 ok → rc=0", rc == 0, f"rc={rc}")
        check("run-summary.json が出力ディレクトリに書かれる", summary.exists())
        doc = json.loads(summary.read_text())
        check("エントリ数 = URL 数（取りこぼしなし）", doc["total"] == 3 and [e["url"] for e in doc["entries"]] == urls)
        check("finished=true", doc["finished"] is True)

    with tempfile.TemporaryDirectory() as d:
        args = argparse.Namespace(model="tiny", language="ja", cpu_threads=4, restore_to=None,
                                  json_summary=str(Path(d) / "custom.json"))
        with patch("main._process_one_url", side_effect=make_runner(d, ["ok", "failed", "warn"])), \
             patch("main.time.sleep"), patch("main.signal.signal"), \
             patch("transcriber.Transcriber", return_value=MagicMock()):
            rc = _run_batch(urls, args, 0, "BlackHole", False)
        doc = json.loads((Path(d) / "custom.json").read_text())
        check("failed があれば rc=2", rc == 2, f"rc={rc}")
        check("--json-summary の指定先に書かれる・集計が合う", doc["ok"] == 1 and doc["warn"] == 1 and doc["failed"] == 1)

    # URL の途中で中断（stop_event）→ 残りは未処理として rc=2・finished=false
    with tempfile.TemporaryDirectory() as d:
        args = argparse.Namespace(model="tiny", language="ja", cpu_threads=4, restore_to=None, json_summary=None, output=d)
        runner = make_runner(d, ["ok", "ok", "ok"])
        def interrupting(url, i, total, tr, args_, di, dn, current, stop_event):
            e = runner(url, i, total, tr, args_, di, dn, current, stop_event)
            if i == 0:
                stop_event.set()
            return e
        with patch("main._process_one_url", side_effect=interrupting), \
             patch("main.time.sleep"), patch("main.signal.signal"), \
             patch("transcriber.Transcriber", return_value=MagicMock()):
            rc = _run_batch(urls, args, 0, "BlackHole", False)
        doc = json.loads((Path(d) / "run-summary.json").read_text())
        check("中断時: 処理済み 1 件だけ記録・finished=false・rc=2",
              doc["total"] == 1 and doc["finished"] is False and rc == 2, f"total={doc['total']} rc={rc}")


def test_process_one_url_returns_entry() -> None:
    """#25: _process_one_url が QA 付きのエントリを返し、本文の行数を数えている。"""
    print("\n[34] _process_one_url の戻り値（run-summary エントリ）")
    import argparse, threading, tempfile
    from unittest.mock import MagicMock, patch
    from main import _process_one_url
    from transcriber import TranscriptSegment

    url = "https://example.com/mod/scorm/player.php?id=42"
    seg = MagicMock(); seg.segment_id = 0; seg.data = np.ones(16000, dtype=np.float32) * 0.1; seg.start_time = 0.0
    cap = MagicMock(); cap.segments.return_value = iter([seg])
    tr = MagicMock()
    tr.transcribe.return_value = [TranscriptSegment(text=f"本文{i}です。", start=i, end=i + 1, segment_id=0) for i in range(5)]
    args = argparse.Namespace(keep_active="chrome", verbose=False, timestamps=False, format="txt",
                              output=None, model="tiny", language="ja", segment_duration=30.0,
                              overlap=5.0, keep_interval=20.0, save_interval=60.0)
    with tempfile.TemporaryDirectory() as d, \
         patch("main.navigate_to_url"), patch("main.time.sleep"), \
         patch("main.get_active_tab_id", return_value=42), \
         patch("main.wait_for_page_ready", return_value=url), \
         patch("main.get_moodle_page_info", return_value=("講義X", url)), \
         patch("main.create_capture", return_value=cap), \
         patch("main.WindowKeepAlive", return_value=MagicMock()), \
         patch("main._trigger_with_retry"), \
         patch("main.get_viewing_percentage", return_value=100), \
         patch("main.get_video_ended", return_value=True), \
         patch("main.get_video_time", return_value=(60.0, 60.0)), \
         patch("main.click_save_button"), patch("main.click_exit_activity_button"), \
         patch("main._prompt_rename", side_effect=lambda p: p), \
         patch("main.OutputWriter"), patch("main._print_progress"):
        args.output = d
        entry = _process_one_url(url, 0, 1, tr, args, 0, "BlackHole 2ch",
                                 {"keep_alive": None, "capture": None}, threading.Event())
    check("エントリに url/title/output/qa がある", all(k in entry for k in ("url", "title", "output", "qa", "played")))
    check("本文 5 行を数えている", entry["qa"]["lines"] == 5, f"lines={entry['qa']['lines']}")
    check("stop_reason=video.ended・played=True", entry["stop_reason"] == "video.ended" and entry["played"] is True)
    check("総尺 60 秒が QA の分母に入る", entry["qa"]["video_seconds"] == 60.0)


def test_qa_boundaries() -> None:
    """#26: qa_metrics の閾値を両側から固定し、failed が格下げされないことを拘束する。"""
    print("\n[35] qa_metrics 境界値 / failed 不格下げ")
    from qa import qa_metrics

    def distinct(n, length=40):
        return [("講義内容の説明文" + f"{i:04d}" + "あ" * 200)[:length] for i in range(n)]

    # failed は warn 条件で格下げされない
    q = qa_metrics(["はい", "えーと", "そうですね"], 262.0, played=False, stop_reason="再生を開始できないため中止")
    check("未再生 + 数行 → failed（warn に格下げしない）", q["status"] == "failed", f"{q['status']} {q['warnings']}")
    q = qa_metrics(distinct(40), 1800.0, played=False)
    check("未再生 + 十分な本文 → failed", q["status"] == "failed")
    q = qa_metrics([], 262.0, played=True)
    check("再生済み + 空 → failed", q["status"] == "failed")
    check("未再生の警告文", "再生されなかった" in " ".join(qa_metrics(["はい"], 262.0, False)["warnings"]))

    # ブラインド（総尺不明・未再生）だが本文あり → warn
    q = qa_metrics(distinct(40), -1.0, played=False)
    check("ブラインド + 本文あり → warn（ブラインドモード）", q["status"] == "warn" and any("ブラインド" in w for w in q["warnings"]),
          f"{q['status']} {q['warnings']}")

    # cpm 境界 (262 秒 = 4.367 分): 170 字 → 38.9 → warn / 180 字 → 41.2 → ok
    check("cpm 38.9 → warn", qa_metrics(["あ" * 170], 262.0, True)["status"] == "warn")
    check("cpm 41.2 → ok", qa_metrics(["あ" * 180], 262.0, True)["status"] == "ok")

    # repeated 境界: 100 行中 重複行 30 (0.30) → ok / 32 (0.32) → warn
    good = distinct(100)
    q30 = qa_metrics(good[:71] + [good[0]] * 29, 262.0, True)
    q32 = qa_metrics(good[:69] + [good[0]] * 31, 262.0, True)
    check("repeated 0.30 → ok", q30["repeated_ratio"] == 0.3 and q30["status"] == "ok", f"{q30['repeated_ratio']} {q30['status']}")
    check("repeated 0.32 → warn", q32["repeated_ratio"] == 0.32 and q32["status"] == "warn", f"{q32['repeated_ratio']} {q32['status']}")

    # hallucination 境界: 0.49 → 非 failed / 0.51 → failed
    q49 = qa_metrics(["ご視聴ありがとうございました"] * 49 + good[:51], 262.0, True)
    q51 = qa_metrics(["ご視聴ありがとうございました"] * 51 + good[:49], 262.0, True)
    check("hallucination 0.49 → failed ではない", q49["hallucination_ratio"] == 0.49 and q49["status"] != "failed")
    check("hallucination 0.51 → failed", q51["hallucination_ratio"] == 0.51 and q51["status"] == "failed")

    # 定型句を数える最大長 30/31
    s30 = "ご視聴ありがとうございました" + "あ" * 16
    s31 = s30 + "あ"
    check("30 字の定型句行は幻覚として数える", qa_metrics([s30], 262.0, True)["hallucination_ratio"] == 1.0)
    check("31 字は数えない", qa_metrics([s31], 262.0, True)["hallucination_ratio"] == 0.0)
    check("「最後までご視聴いただきありがとうございました」1 行 → failed",
          qa_metrics(["最後までご視聴いただきありがとうございました"], 262.0, True)["status"] == "failed")

    # 正規化: 句読点違いは同一文
    q = qa_metrics(["同じ文章です。", "同じ文章です", "同じ文章です、"], 262.0, True)
    check("句読点違いを重複と数える（_norm）", q["repeated_ratio"] == 1.0, f"{q['repeated_ratio']}")

    # 異常終了理由（PlaybackMonitor の実文字列）
    for reason in ("録音時間が総尺を大幅超過（watchdog）", "再生を開始できないため中止", "音声が検出できないため中止"):
        q = qa_metrics(distinct(60), 262.0, True, stop_reason=reason)
        check(f"良好本文でも理由『{reason}』→ warn", q["status"] == "warn" and any("異常終了" in w for w in q["warnings"]))
    q = qa_metrics(distinct(60), 262.0, True, stop_reason="video.ended")
    check("video.ended は ok", q["status"] == "ok")


def test_run_batch_interrupt_and_exception() -> None:
    """#27: 中断（interrupted）と例外経路で run-summary.json / rc が正しい。"""
    print("\n[36] _run_batch 中断・例外・warn のみ")
    import argparse, json, tempfile
    from unittest.mock import MagicMock, patch
    from pathlib import Path
    from main import _run_batch

    urls = ["https://m.example.com/x?a=1", "https://m.example.com/x?a=2", "https://m.example.com/x?a=3"]

    def entry(i, url, outdir, status):
        return {"index": i, "url": url, "page_url": url, "title": f"講義{i}",
                "output": str(Path(outdir) / f"講義{i}.txt"), "stop_reason": "video.ended",
                "played": status in ("ok", "warn"), "elapsed_seconds": 1.0,
                "qa": {"status": status, "lines": 10, "chars": 300, "video_seconds": 60.0,
                       "chars_per_min": 300.0, "repeated_ratio": 0.0, "hallucination_ratio": 0.0,
                       "warnings": [] if status == "ok" else ["x"]}}

    base_args = dict(model="tiny", language="ja", cpu_threads=4, restore_to=None, json_summary=None, output=None)

    # 3 本目で Ctrl+C → status interrupted → rc=2・failed=1・finished=false
    with tempfile.TemporaryDirectory() as d:
        def runner(url, i, total, tr, args, di, dn, current, stop_event):
            if i == 2:
                stop_event.set()
                return entry(i, url, d, "interrupted")
            return entry(i, url, d, "ok")
        with patch("main._process_one_url", side_effect=runner), patch("main.time.sleep"), \
             patch("main.signal.signal"), patch("transcriber.Transcriber", return_value=MagicMock()), \
             patch("subprocess.run"):
            rc = _run_batch(urls, argparse.Namespace(**{**base_args, "output": d}), 0, "BlackHole", False)
        doc = json.loads((Path(d) / "run-summary.json").read_text())
        check("interrupted → rc=2", rc == 2, f"rc={rc}")
        check("interrupted は failed に集計", doc["failed"] == 1 and doc["ok"] == 2, f"{doc['ok']}/{doc['failed']}")
        check("interrupted → finished=false", doc["finished"] is False)

    # warn だけなら rc=0
    with tempfile.TemporaryDirectory() as d:
        statuses = ["ok", "warn", "ok"]
        with patch("main._process_one_url", side_effect=lambda url, i, *a: entry(i, url, d, statuses[i])), \
             patch("main.time.sleep"), patch("main.signal.signal"), \
             patch("transcriber.Transcriber", return_value=MagicMock()), patch("subprocess.run"):
            rc = _run_batch(urls, argparse.Namespace(**{**base_args, "output": d}), 0, "BlackHole", False)
        check("warn のみ → rc=0", rc == 0, f"rc={rc}")

    # 2 本目で例外 → URL ごとの flush により 1 本目は残り、finished=false
    with tempfile.TemporaryDirectory() as d:
        flushed_before_crash: list = []
        def crashing(url, i, *a):
            if i == 1:
                p = Path(d) / "run-summary.json"
                flushed_before_crash.append(p.exists() and json.loads(p.read_text())["total"] == 1)
                raise RuntimeError("boom")
            return entry(i, url, d, "ok")
        raised = False
        with patch("main._process_one_url", side_effect=crashing), patch("main.time.sleep"), \
             patch("main.signal.signal"), patch("transcriber.Transcriber", return_value=MagicMock()), \
             patch("subprocess.run"):
            try:
                _run_batch(urls, argparse.Namespace(**{**base_args, "output": d}), 0, "BlackHole", False)
            except RuntimeError:
                raised = True
        summary = Path(d) / "run-summary.json"
        check("例外は伝播する", raised)
        check("例外が起きる前に 1 本目が flush されている（URL ごと flush）", flushed_before_crash == [True], f"{flushed_before_crash}")
        check("例外前に処理した 1 本は run-summary に残る（URL ごと flush）", summary.exists() and json.loads(summary.read_text())["total"] == 1)
        check("例外時は finished=false", json.loads(summary.read_text())["finished"] is False)


def test_process_one_url_interrupt() -> None:
    """#28: _process_one_url が途中で Ctrl+C（stop_event）されたら status=interrupted、リネーム問い合わせなし。"""
    print("\n[37] _process_one_url の中断経路")
    import argparse, threading, tempfile
    from unittest.mock import MagicMock, patch
    from main import _process_one_url

    url = "https://example.com/mod/scorm/player.php?id=42"
    segs = []
    for i in range(6):
        m = MagicMock(); m.segment_id = i; m.data = np.zeros(16000, dtype=np.float32); m.start_time = 30.0 * i
        segs.append(m)
    cap = MagicMock(); cap.segments.return_value = iter(segs)
    tr = MagicMock(); tr.transcribe.return_value = []
    ev = threading.Event()
    renames: list = []
    times = iter([(30.0, 600.0), (60.0, 600.0)])
    def video_time(*a, **kw):
        t = next(times, (90.0, 600.0))
        if t[0] >= 60.0:
            ev.set()
        return t
    args = argparse.Namespace(keep_active="chrome", verbose=False, timestamps=False, format="txt",
                              output=None, model="tiny", language="ja", segment_duration=30.0,
                              overlap=5.0, keep_interval=20.0, save_interval=60.0)
    with tempfile.TemporaryDirectory() as d, \
         patch("main.navigate_to_url"), patch("main.time.sleep"), \
         patch("main.get_active_tab_id", return_value=42), \
         patch("main.wait_for_page_ready", return_value=url), \
         patch("main.get_moodle_page_info", return_value=("講義", url)), \
         patch("main.create_capture", return_value=cap), \
         patch("main.WindowKeepAlive", return_value=MagicMock()), \
         patch("main._trigger_with_retry"), \
         patch("main.get_viewing_percentage", return_value=10), \
         patch("main.get_video_ended", return_value=False), \
         patch("main.get_video_time", side_effect=video_time), \
         patch("main.click_save_button"), patch("main.click_exit_activity_button"), \
         patch("main._prompt_rename", side_effect=lambda p: renames.append(p) or p), \
         patch("main.OutputWriter"), patch("main._print_progress"):
        args.output = d
        entry = _process_one_url(url, 0, 1, tr, args, 0, "BlackHole 2ch", {"keep_alive": None, "capture": None}, ev)
    check("中断 → status=interrupted", entry["qa"]["status"] == "interrupted", f"{entry['qa']['status']}")
    check("中断の警告文がある", any("Ctrl+C" in w for w in entry["qa"]["warnings"]))
    check("中断時はリネーム問い合わせをしない", len(renames) == 0, f"renames={len(renames)}")


def test_warn_other_audio_apps() -> None:
    """#29: 他アプリの再生検知（preflight の合格ゲートに使う）。"""
    print("\n[38] warn_other_audio_apps")
    from unittest.mock import MagicMock, patch
    import platform_utils
    from platform_utils import warn_other_audio_apps

    def osa(stdout, rc=0):
        m = MagicMock(); m.returncode = rc; m.stdout = stdout; return m

    scripts: list = []
    def fake(script, timeout=10.0):
        scripts.append(script)
        return osa("playing\n") if '"Spotify"' in script else osa("not running\n")
    with patch("platform_utils.SYSTEM", "Darwin"), patch("platform_utils._osascript", side_effect=fake):
        r = warn_other_audio_apps()
    check("Spotify playing → ['Spotify']", r == ["Spotify"], f"{r}")
    check("スクリプトに player state を問い合わせる", all("player state" in s for s in scripts))
    check("Spotify と Music の両方を問い合わせる", len(scripts) == 2 and any('"Music"' in s for s in scripts), f"{len(scripts)}")
    for out in ("paused\n", "stopped\n", "not running\n"):
        with patch("platform_utils.SYSTEM", "Darwin"), patch("platform_utils._osascript", return_value=osa(out)):
            check(f"{out.strip()!r} → []", warn_other_audio_apps() == [])
    with patch("platform_utils.SYSTEM", "Darwin"), patch("platform_utils._osascript", return_value=osa("", rc=1)):
        check("rc≠0 → []", warn_other_audio_apps() == [])
    with patch("platform_utils.SYSTEM", "Darwin"), patch("platform_utils._osascript", return_value=None):
        check("タイムアウト(None) → []", warn_other_audio_apps() == [])
    calls: list = []
    with patch("platform_utils.SYSTEM", "Linux"), patch("platform_utils._osascript", side_effect=lambda *a, **k: calls.append(1)):
        check("非 macOS は osascript を呼ばず []", warn_other_audio_apps() == [] and not calls)

    # tab_text の埋め込み JS は AppleScript 制約（" と \ 禁止）を満たす
    import tab_text
    check("tab_text._TEXT_JS に \" と \\\\ が無い", '"' not in tab_text._TEXT_JS and "\\" not in tab_text._TEXT_JS)


def test_page_capture() -> None:
    """#30: PageAudioCapture のセグメント化・位相・終端・タイムアウト。"""
    print("\n[39] PageAudioCapture（ページ内キャプチャの受け口）")
    import base64
    from page_capture import PageAudioCapture, SAMPLE_RATE

    rms = lambda a: float(np.sqrt(np.mean(np.square(a))))
    cap = PageAudioCapture(segment_duration=2.0, overlap_duration=0.5)
    t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
    tone = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)   # 1 秒
    cap.start()
    # 逆相ステレオを base64(int16 LRLR) で 5 秒ぶん投入
    st = np.stack([tone, -tone], axis=1)
    b64 = base64.b64encode((st * 32767).astype(np.int16).tobytes()).decode()
    for _ in range(5):
        cap.feed_b64(b64)
    segs = []
    while True:
        s = cap.next_segment(timeout=0.1)
        if s is None:
            break
        segs.append(s)
    check("5 秒入力・2 秒セグメント・0.5 秒重複 → 3 本（0, 1.5, 3.0 秒開始）",
          [round(s.start_time, 1) for s in segs] == [0.0, 1.5, 3.0], f"{[round(s.start_time,1) for s in segs]}")
    check("セグメント長は 2 秒", all(len(s.data) == 2 * SAMPLE_RATE for s in segs))
    check("逆相ステレオでも打ち消されない（downmix 共有）", all(rms(s.data) > 0.15 for s in segs), f"{[round(rms(s.data),3) for s in segs]}")
    check("segment_id は連番", [s.segment_id for s in segs] == [0, 1, 2])
    check("seconds_received = 5.0", abs(cap.seconds_received - 5.0) < 1e-6)
    check("タイムアウト時は None（終端ではない）", cap.next_segment(timeout=0.05) is None)
    cap.stop()
    tail = cap.next_segment(timeout=0.5)
    check("stop() で残り（4.5〜5.0 秒の 0.5 秒）は 1 秒未満なので捨て、終端 None", tail is None)
    check("終端後も None を返し続ける", cap.next_segment(timeout=0.05) is None)
    qsize_before = cap._queue.qsize()
    cap.stop()
    check("stop() は冪等（2 回目で終端やセグメントを追加しない）", cap._queue.qsize() == qsize_before)
    got = cap.seconds_received
    cap.feed_pcm(st)
    check("stop() 後の入力は無視", cap.seconds_received == got)

    # 残りが 1 秒以上なら最終セグメントとして流す
    cap2 = PageAudioCapture(segment_duration=2.0, overlap_duration=0.5); cap2.start()
    for _ in range(3):
        cap2.feed_pcm(st)            # 3 秒 → 1 本出て残り 1.5 秒（重複 0.5 含む）
    first = cap2.next_segment(timeout=0.1)
    cap2.stop()
    last = cap2.next_segment(timeout=0.5)
    check("stop(): 残り 1.0 秒（重複除く）を最終セグメントとして出す",
          first is not None and last is not None and abs(len(last.data) / SAMPLE_RATE - 1.0) < 1e-6 and abs(last.start_time - 2.0) < 1e-6,
          f"last={None if last is None else (len(last.data)/SAMPLE_RATE, last.start_time)}")

    # start 前・stop 後の入力は無視
    cap3 = PageAudioCapture(1.0, 0.0)
    cap3.feed_pcm(st)
    check("start() 前の入力は無視", cap3.seconds_received == 0.0)
    # 1ch 入力も受ける
    cap4 = PageAudioCapture(1.0, 0.0); cap4.start(); cap4.feed_pcm(tone)
    check("モノラル入力も受ける", cap4.next_segment(timeout=0.1) is not None)


class _FakeSession:
    """BrowserSession の代替。再生状態と音声チャンクの流れをテストから制御する。

    feed_seconds: start_capture 時に一括投入する秒数（従来モード）
    lazy_chunks:  state() が呼ばれるたびに 1 秒ぶん投入する上限（タイムアウト経路の再現用）。
                  None なら無効。0 なら 1 チャンクも届かない。
    """

    def __init__(self, play_ok=True, player_url="https://m.example.com/mod/scorm/player.php?a=1",
                 duration=60.0, stop_event=None, interrupt_after=None, feed_seconds=0.0,
                 has_video=True, capture_result="capturing sr=16000 state=running", lazy_chunks=None,
                 continuous=False, max_state_calls=400):
        """continuous=True: 本番の ScriptProcessor と同じく、state() が呼ばれるたびに 1 秒ぶんの
        チャンクを**動画終了後も無音で**供給し続ける（タイムアウト経路が救済できない条件）。
        max_state_calls: 止まらない回帰をハングでなく例外（赤）にする上限。"""
        import base64
        self._continuous = continuous
        self._max_state_calls = max_state_calls
        self.play_ok = play_ok
        self.player_url = player_url
        self.duration = duration
        self.calls: list[str] = []
        self.sink = None
        self.cur = 0.0
        self.playing = False
        self.state_calls = 0
        self._stop_event = stop_event
        self._interrupt_after = interrupt_after
        self._feed_seconds = feed_seconds
        self._has_video = has_video
        self._capture_result = capture_result
        self._lazy_left = lazy_chunks
        self.wait_args: dict = {}
        sr = 16000
        t = np.arange(sr) / sr
        tone = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        self._b64_tone = base64.b64encode((np.stack([tone, tone], axis=1) * 32767).astype(np.int16).tobytes()).decode()
        self._b64_silence = base64.b64encode(np.zeros(sr * 2, dtype=np.int16).tobytes()).decode()

    def set_chunk_sink(self, sink): self.sink = sink
    def goto(self, url): self.calls.append("goto"); return url
    def url(self): return self.player_url or "https://m.example.com/login/index.php"
    def title(self): return "講義X | コース | Moodle"
    def pump(self, ms=200): pass
    def has_video(self): return self._has_video
    def wait_for_player(self, contains="player.php", timeout=60.0, login_timeout=600.0,
                        on_login_required=None, should_abort=None, poll_ms=500):
        self.wait_args = {"contains": contains, "timeout": timeout, "login_timeout": login_timeout,
                          "has_abort": should_abort is not None}
        if self.player_url is None:
            if on_login_required: on_login_required("https://m.example.com/login/index.php")
            return None
        return self.player_url
    def start_capture(self):
        self.calls.append("start_capture")
        if self._capture_result != "no_video" and self.sink is not None:
            for _ in range(int(self._feed_seconds)):
                self.sink(self._b64_tone if self.play_ok else self._b64_silence)
        return self._capture_result
    def stop_capture(self): self.calls.append("stop_capture"); return "stopped"
    def play(self):
        self.calls.append("play")
        if self.play_ok:
            self.playing = True
            return "playing"
        return "ERR NotAllowedError"
    def state(self):
        from browser import VideoState
        self.state_calls += 1
        if self.state_calls > self._max_state_calls:
            raise RuntimeError("fake session: state() call limit — 終了処理が走っていない")
        if self._interrupt_after is not None and self.state_calls >= self._interrupt_after and self._stop_event:
            self._stop_event.set()
        if self._lazy_left is not None and self._lazy_left > 0 and self.sink is not None:
            self._lazy_left -= 1
            self.sink(self._b64_tone if self.play_ok else self._b64_silence)
        if self.playing:
            # continuous モードは「state() 1 回 = 1 秒進む = 1 秒ぶんのチャンク」で実時間比 1:1
            self.cur = min(self.duration, self.cur + (1.0 if self._continuous else 10.0))
        ended = self.playing and self.cur >= self.duration
        if self._continuous and self.sink is not None:
            # 再生中はトーン、未再生・終了後は無音。チャンクは枯れない
            self.sink(self._b64_tone if (self.playing and not ended) else self._b64_silence)
        return VideoState(found=True, paused=not self.playing or ended, ended=ended,
                          current_time=self.cur, duration=self.duration, capture_count=1, ctx_state="running")
    def viewing_percentage(self): return int(self.cur / self.duration * 100) if self.playing else 0
    def click_save(self): self.calls.append("save"); return True
    def click_exit(self): self.calls.append("exit"); return True


def test_process_one_url_page() -> None:
    """#31: page エンジンの 1 URL 処理（偽セッション）。再生済み/未再生/ログイン失敗/中断。"""
    print("\n[40] _process_one_url_page（page エンジン）")
    import argparse, threading, tempfile
    from unittest.mock import MagicMock, patch
    from main import _process_one_url_page, PlaybackMonitor
    from transcriber import TranscriptSegment

    def mk_args(d, seg=1.0, ov=0.0):
        return argparse.Namespace(verbose=False, timestamps=False, format="txt", output=d, model="tiny",
                                  language="ja", segment_duration=seg, overlap=ov, save_interval=0,
                                  login_timeout=5.0, player_marker="player.php")
    def mk_tr():
        tr = MagicMock()
        tr.transcribe.side_effect = lambda data, time_offset=0.0: [TranscriptSegment(text=f"本文 {time_offset:.0f} 秒の説明です。", start=time_offset, end=time_offset + 1, segment_id=0)]
        return tr

    # 1) 再生できる: 本文が出て、ended で止まり、保存→離脱を押す。
    #    チャンクは終了後も無音で続く（本番同様）ので、通常経路の停止処理が無効なら
    #    タイムアウト経路は救済できず、state() 上限で RuntimeError になる。
    polls_main: list = []
    orig_poll_m = PlaybackMonitor.poll
    def rec_poll_m(self, silent=None, elapsed=None):
        polls_main.append(silent); return orig_poll_m(self, silent=silent, elapsed=elapsed)
    with tempfile.TemporaryDirectory() as d, patch("main.time.sleep"), patch("main._prompt_rename", side_effect=lambda p: p), \
         patch("main._print_progress"), patch.object(PlaybackMonitor, "poll", rec_poll_m):
        sess = _FakeSession(play_ok=True, duration=60.0, feed_seconds=2, continuous=True)
        t0 = time.monotonic()
        entry = _process_one_url_page(sess.player_url, 0, 1, mk_tr(), mk_args(d), sess, threading.Event())
        took = time.monotonic() - t0
        out_exists = bool(entry["output"]) and Path(entry["output"]).exists()
    check("再生済み: played=True・stop_reason=video.ended", entry["played"] is True and entry["stop_reason"] == "video.ended",
          f"{entry['played']} {entry['stop_reason']}")
    check("再生済み: 通常経路（セグメント到着ごとの poll）で止まる。タイムアウト経路は使わず 3 秒以内",
          took < 3.0 and polls_main and all(s is False for s in polls_main), f"took={took:.1f}s polls={polls_main[:6]}")
    check("再生済み: qa.status=ok", entry["qa"]["status"] == "ok", f"{entry['qa']}")
    check("再生済み: 本文が数えられる（qa.lines>0）", entry["qa"]["lines"] > 0, f"lines={entry['qa']['lines']}")
    check("再生済み: タイトルは | の前", entry["title"] == "講義X")
    check("再生済み: 保存→離脱を押す", sess.calls[-2:] == ["save", "exit"], f"{sess.calls[-4:]}")
    check("再生済み: stop_capture を呼ぶ", "stop_capture" in sess.calls)
    check("再生済み: 出力ファイルが書かれる", out_exists)
    check("再生済み: qa.video_seconds=60", entry["qa"]["video_seconds"] == 60.0)

    # 2) 再生できない: 無音が流れ続け、未開始 → 再試行 → 中止。保存/離脱は押さない
    with tempfile.TemporaryDirectory() as d, patch("main.time.sleep"), patch("main._prompt_rename", side_effect=lambda p: p), \
         patch("main._print_progress"):
        sess = _FakeSession(play_ok=False, duration=60.0, feed_seconds=2, continuous=True)
        t0 = time.monotonic()
        entry = _process_one_url_page(sess.player_url, 0, 1, mk_tr(), mk_args(d), sess, threading.Event())
        took = time.monotonic() - t0
    check("未再生: played=False・中止で終了", entry["played"] is False and "中止" in entry["stop_reason"], f"{entry['stop_reason']}")
    check("未再生: 通常経路で止まる（3 秒以内）", took < 3.0, f"took={took:.1f}s")
    check("未再生: qa.status=failed", entry["qa"]["status"] == "failed", f"{entry['qa']['warnings']}")
    check("未再生: 保存/離脱を押さない", "save" not in sess.calls and "exit" not in sess.calls, f"{sess.calls}")
    check("未再生: 再生を複数回試みた（初回 5 回 + 再試行）", sess.calls.count("play") >= 5 + PlaybackMonitor.NOT_STARTED_MAX_RETRY,
          f"plays={sess.calls.count('play')}")

    # 3) ログイン待ちタイムアウト（プレーヤー未到達）→ failed、出力なし
    with tempfile.TemporaryDirectory() as d, patch("main.time.sleep"), patch("main._print_progress"):
        sess = _FakeSession(player_url=None)
        entry = _process_one_url_page("https://m.example.com/mod/scorm/player.php?a=9", 0, 1, mk_tr(), mk_args(d), sess, threading.Event())
    check("プレーヤー未到達: failed・output=None", entry["qa"]["status"] == "failed" and entry["output"] is None, f"{entry['qa']}")
    check("プレーヤー未到達: 再生もキャプチャもしない", "play" not in sess.calls and "start_capture" not in sess.calls)

    # 4) Ctrl+C（stop_event）→ interrupted、リネームなし
    renames: list = []
    ev = threading.Event()
    with tempfile.TemporaryDirectory() as d, patch("main.time.sleep"), patch("main._prompt_rename", side_effect=lambda p: renames.append(p) or p), \
         patch("main._print_progress"):
        sess = _FakeSession(play_ok=True, duration=600.0, feed_seconds=12, stop_event=ev, interrupt_after=8)
        entry = _process_one_url_page(sess.player_url, 0, 1, mk_tr(), mk_args(d), sess, ev)
    check("中断: status=interrupted", entry["qa"]["status"] == "interrupted", f"{entry['qa']['status']}")
    check("中断: リネーム問い合わせなし・離脱を押さない", not renames and "exit" not in sess.calls)

    # 5) 引数の配線: player_marker / login_timeout / 中断コールバックが wait_for_player に渡る
    with tempfile.TemporaryDirectory() as d, patch("main.time.sleep"), patch("main._prompt_rename", side_effect=lambda p: p), \
         patch("main._print_progress"):
        sess = _FakeSession(play_ok=True, duration=60.0, feed_seconds=12)
        a = mk_args(d); a.player_marker = "lesson.php"; a.login_timeout = 123.0
        _process_one_url_page(sess.player_url, 0, 1, mk_tr(), a, sess, threading.Event())
    check("wait_for_player に player_marker / login_timeout / 中断判定が渡る",
          sess.wait_args == {"contains": "lesson.php", "timeout": 60.0, "login_timeout": 123.0, "has_abort": True}, f"{sess.wait_args}")

    # 6) video 要素なし → 再生もキャプチャ継続もせず、動画要素なしで failed
    import main as _main
    with tempfile.TemporaryDirectory() as d, patch("main.time.sleep"), patch("main._prompt_rename", side_effect=lambda p: p), \
         patch("main._print_progress"), patch.object(_main, "PAGE_VIDEO_WAIT", 0.0):
        sess = _FakeSession(has_video=False, capture_result="no_video")
        entry = _process_one_url_page(sess.player_url, 0, 1, mk_tr(), sess and mk_args(d), sess, threading.Event())
        out_exists = bool(entry["output"]) and Path(entry["output"]).exists()
    check("no_video: stop_reason=動画要素なし・failed", entry["stop_reason"] == "動画要素なし" and entry["qa"]["status"] == "failed",
          f"{entry['stop_reason']} {entry['qa']['status']}")
    check("no_video: 再生・保存・離脱を試みない", not any(c in sess.calls for c in ("play", "save", "exit")), f"{sess.calls}")
    check("no_video: 出力ファイルは閉じて残る（ヘッダのみ）", out_exists)

    # 7) 音声チャンクが途中で止まる（動画は進んで終了）→ タイムアウト経路で poll(silent=True) → video.ended で終了
    polls: list = []
    orig_poll = PlaybackMonitor.poll
    def rec_poll(self, silent=None, elapsed=None):
        polls.append(silent); return orig_poll(self, silent=silent, elapsed=elapsed)
    with tempfile.TemporaryDirectory() as d, patch("main.time.sleep"), patch("main._prompt_rename", side_effect=lambda p: p), \
         patch("main._print_progress"), patch.object(PlaybackMonitor, "poll", rec_poll):
        sess = _FakeSession(play_ok=True, duration=60.0, feed_seconds=0, lazy_chunks=0)   # 1 チャンクも届かない
        a = mk_args(d, seg=1.0); a.page_poll_timeout = 0.05
        t0 = time.monotonic()
        entry = _process_one_url_page(sess.player_url, 0, 1, mk_tr(), a, sess, threading.Event())
        took = time.monotonic() - t0
    check("チャンク 0 件: タイムアウト経路で poll(silent=True) が走る", polls and all(s is True for s in polls), f"polls={polls[:5]}")
    check("チャンク 0 件: 動画終了を検出してループが終わる", entry["stop_reason"] == "video.ended" and took < 20, f"{entry['stop_reason']} took={took:.1f}s")
    check("チャンク 0 件: 本文なし → failed（偽成功にならない）", entry["qa"]["status"] == "failed" and entry["qa"]["lines"] == 0)
    check("チャンク 0 件: stop_capture / capture 終了処理が走る", "stop_capture" in sess.calls)
    check("チャンク 0 件: audio_seconds_received=0 を記録", entry.get("audio_seconds_received") == 0.0, f"{entry.get('audio_seconds_received')}")

    # 8) 途中でチャンクが止まり動画も進まない（再生停止）→ 停滞で終了、ハングしない
    with tempfile.TemporaryDirectory() as d, patch("main.time.sleep"), patch("main._prompt_rename", side_effect=lambda p: p), \
         patch("main._print_progress"):
        sess = _FakeSession(play_ok=True, duration=600.0, feed_seconds=3, lazy_chunks=0)
        a = mk_args(d, seg=1.0); a.page_poll_timeout = 0.05
        # 3 秒ぶん届いた後は無音・停止: state() の進行を止める
        orig_state = sess.state
        def frozen_state():
            st = orig_state(); sess.cur = 20.0; return st
        sess.state = frozen_state
        t0 = time.monotonic()
        entry = _process_one_url_page(sess.player_url, 0, 1, mk_tr(), a, sess, threading.Event())
        took = time.monotonic() - t0
    check("チャンク停止 + 再生停止: 停滞 or 無音で終了しハングしない", entry["stop_reason"] != "" and took < 20,
          f"{entry['stop_reason']} took={took:.1f}s")
    check("チャンク停止: 届いた音声が動画長の半分未満 → 警告", any("半分未満" in w for w in entry["qa"]["warnings"]), f"{entry['qa']['warnings']}")

    # 9) 字/分は十分（長文）だが届いた音声が半分未満 → ok ではなく warn に格上げ
    def mk_tr_long():
        tr = MagicMock()
        tr.transcribe.side_effect = lambda data, time_offset=0.0: [TranscriptSegment(
            text=("長い本文" + f"{time_offset:.0f}" + "あ" * 120), start=time_offset, end=time_offset + 1, segment_id=0)]
        return tr
    with tempfile.TemporaryDirectory() as d, patch("main.time.sleep"), patch("main._prompt_rename", side_effect=lambda p: p), \
         patch("main._print_progress"):
        sess = _FakeSession(play_ok=True, duration=60.0, feed_seconds=3, lazy_chunks=0)
        a = mk_args(d, seg=1.0); a.page_poll_timeout = 0.05
        orig_state = sess.state
        def frozen_state():
            st = orig_state(); sess.cur = 20.0; return st
        sess.state = frozen_state
        entry = _process_one_url_page(sess.player_url, 0, 1, mk_tr_long(), a, sess, threading.Event())
    check("半分未満: 字/分が十分でも status は warn に格上げ", entry["qa"]["status"] == "warn" and entry["qa"]["chars_per_min"] >= 40,
          f"{entry['qa']['status']} cpm={entry['qa']['chars_per_min']} {entry['qa']['warnings']}")

    # 10) AudioContext が 16kHz を拒否 → 偽成功にせず failed
    with tempfile.TemporaryDirectory() as d, patch("main.time.sleep"), patch("main._prompt_rename", side_effect=lambda p: p), \
         patch("main._print_progress"):
        sess = _FakeSession(play_ok=True, duration=60.0, capture_result="capturing sr=48000 state=running")
        entry = _process_one_url_page(sess.player_url, 0, 1, mk_tr(), mk_args(d), sess, threading.Event())
    check("サンプルレート不一致 → failed・再生しない", entry["qa"]["status"] == "failed" and "サンプルレート" in entry["stop_reason"]
          and "play" not in sess.calls, f"{entry['stop_reason']} {sess.calls}")


def test_page_engine_live() -> None:
    """#32: 実 Chrome（専用プロファイル・ヘッドレス）でページ内キャプチャ → 文字起こしループを通す。

    ローカル HTTP で配信した逆相ステレオのテスト動画（iframe 内）を使う。Moodle 不要・ユーザーの Chrome 不操作。
    playwright / Chrome が無い環境、または MT_SKIP_BROWSER=1 ではスキップ。
    """
    print("\n[41] page エンジン実機 E2E（ヘッドレス Chrome・ローカル動画）")
    import os, argparse, tempfile, threading, wave
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from unittest.mock import MagicMock, patch
    if os.environ.get("MT_SKIP_BROWSER") == "1":
        check("page エンジン E2E", True, "スキップ (MT_SKIP_BROWSER=1)"); return
    try:
        import playwright  # noqa: F401
    except ImportError:
        check("page エンジン E2E", True, "スキップ (playwright 未導入)"); return
    if not Path("/Applications/Google Chrome.app").exists():
        check("page エンジン E2E", True, "スキップ (Google Chrome 無し)"); return

    from browser import BrowserSession
    from main import _process_one_url_page
    from transcriber import TranscriptSegment, audio_rms

    with tempfile.TemporaryDirectory() as d:
        fx = Path(d) / "fx"; fx.mkdir()
        sr = 48000; t = np.arange(sr * 8) / sr
        sig = 0.3 * np.sin(2 * np.pi * 440 * t)
        st = np.stack([sig, -sig], axis=1)   # 逆相ステレオ（実機で観測した形）
        with wave.open(str(fx / "tone.wav"), "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr); w.writeframes((st * 32767).astype(np.int16).tobytes())
        (fx / "player.html").write_text(
            '<!doctype html><html><head><meta charset="utf-8"><title>テスト講義 | コース | TestMoodle</title></head>'
            '<body><p>視聴状況 0%</p><div id="save"><input type="button" value="保存" onclick="window.__saved=(window.__saved||0)+1"></div>'
            '<div id="region-main"><a href="#" onclick="window.__exited=true;return false;">活動から抜ける</a></div>'
            '<iframe src="inner.html" width="640" height="300"></iframe></body></html>', encoding="utf-8")
        (fx / "inner.html").write_text('<!doctype html><html><body><video src="tone.wav"></video></body></html>')
        srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(fx)))
        th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
        port = srv.server_address[1]
        url = f"http://127.0.0.1:{port}/player.html"

        rms_seen: list[float] = []
        tr = MagicMock()
        def fake_transcribe(data, time_offset=0.0):
            rms_seen.append(audio_rms(data))
            return [TranscriptSegment(text=f"テスト本文 {time_offset:.0f} 秒", start=time_offset, end=time_offset + 1, segment_id=0)]
        tr.transcribe.side_effect = fake_transcribe

        args = argparse.Namespace(verbose=False, timestamps=False, format="txt", output=str(Path(d) / "out"),
                                  model="tiny", language="ja", segment_duration=3.0, overlap=0.5, save_interval=0,
                                  login_timeout=5.0, player_marker="player.html")
        session = BrowserSession(profile_dir=Path(d) / "profile", headless=True)
        try:
            session.start(timeout=90)
            check("専用 Chrome 起動（ヘッドレス・別プロファイル）", True)
            with patch("main._prompt_rename", side_effect=lambda p: p), patch("main._print_progress"):
                entry = _process_one_url_page(url, 0, 1, tr, args, session, threading.Event())
            check("E2E: 再生を実測できた (played=True)", entry["played"] is True, f"{entry}")
            check("E2E: 終了を検出 (video.ended / 末尾)", entry["stop_reason"] in ("video.ended", "動画末尾に到達", "動画末尾に到達（巻き戻し検出）"),
                  f"{entry['stop_reason']}")
            check("E2E: タイトルをページから取得", entry["title"] == "テスト講義", f"{entry['title']}")
            check("E2E: 音声がページ内から届き、逆相でも打ち消されない (RMS>0.15)", bool(rms_seen) and max(rms_seen) > 0.15,
                  f"rms={[round(r, 3) for r in rms_seen]}")
            check("E2E: 本文が書かれた", entry["qa"]["lines"] > 0 and Path(entry["output"]).exists())
            check("E2E: 保存ボタンと離脱リンクをページ内で押した",
                  session.evaluate("(window.__saved||0) >= 1 && window.__exited === true"))
            check("E2E: qa.status=ok・video_seconds=8.0", entry["qa"]["status"] == "ok" and entry["qa"]["video_seconds"] == 8.0,
                  f"{entry['qa']['status']} {entry['qa']['video_seconds']}")
            check("E2E: 届いた音声秒数が動画長に近い", 6.0 <= entry["audio_seconds_received"] <= 20.0, f"{entry['audio_seconds_received']}")
        finally:
            session.close()
            srv.shutdown()


def test_course_page_dump_and_page_batch() -> None:
    """#33: バッチ終了時のコースページ保存と、session 付き _run_batch が page 経路を通ること。"""
    print("\n[42] course-page.txt 保存 / page エンジンのバッチ配線")
    import argparse, json, tempfile
    from unittest.mock import MagicMock, patch
    from main import _dump_course_page, _run_batch

    class S:
        def __init__(self, url, text="シラバス\n最終レポートを提出してください"):
            self._url = url; self._text = text; self.evals = 0
        def url(self): return self._url
        def evaluate(self, js): self.evals += 1; return self._text

    with tempfile.TemporaryDirectory() as d:
        s = S("https://m.example.com/moodle/course/view.php?id=1")
        path = _dump_course_page(s, Path(d))
        check("コースページに居れば course-page.txt を書く", path is not None and path.exists())
        body = path.read_text(encoding="utf-8") if path else ""
        check("1 行目が URL・本文を含む", body.startswith("# URL: https://m.example.com/moodle/course/view.php?id=1") and "最終レポート" in body)
        s2 = S("https://m.example.com/moodle/mod/scorm/player.php?a=1")
        check("プレーヤーに居るときは保存しない", _dump_course_page(s2, Path(d)) is None and s2.evals == 0)

    # session を渡すと _process_one_url_page が使われ、終了時に course page を保存する
    urls = ["https://m.example.com/x?a=1", "https://m.example.com/x?a=2"]
    with tempfile.TemporaryDirectory() as d:
        sess = S("https://m.example.com/moodle/course/view.php?id=1")
        calls = {"page": 0, "bh": 0}
        def page_runner(url, i, total, tr, args, session, stop_event):
            calls["page"] += 1
            return {"index": i, "url": url, "page_url": url, "title": f"講義{i}", "output": str(Path(d) / f"講義{i}.txt"),
                    "stop_reason": "video.ended", "played": True, "elapsed_seconds": 1.0,
                    "qa": {"status": "ok", "lines": 10, "chars": 300, "video_seconds": 60.0, "chars_per_min": 300.0,
                           "repeated_ratio": 0.0, "hallucination_ratio": 0.0, "warnings": []}}
        with patch("main._process_one_url_page", side_effect=page_runner), \
             patch("main._process_one_url", side_effect=lambda *a, **k: calls.__setitem__("bh", calls["bh"] + 1)), \
             patch("main.time.sleep"), patch("main.signal.signal"), \
             patch("transcriber.Transcriber", return_value=MagicMock()), patch("subprocess.run"):
            rc = _run_batch(urls, argparse.Namespace(model="tiny", language="ja", cpu_threads=4, restore_to=None,
                                                     json_summary=None, output=d), session=sess)
        check("session あり → page 経路のみ・全 URL", calls == {"page": 2, "bh": 0} and rc == 0, f"{calls} rc={rc}")
        check("バッチ終了時に course-page.txt を保存", (Path(d) / "course-page.txt").exists())
        check("run-summary.json も同じディレクトリ", (Path(d) / "run-summary.json").exists())

    # 1 本目がプレーヤー未到達（output=None）でも落ちず、サマリーは args.output に書かれる
    import subprocess as _sp
    with tempfile.TemporaryDirectory() as d:
        sess = S("https://m.example.com/moodle/course/view.php?id=1")
        def runner(url, i, total, tr, args, session, stop_event):
            if i == 0:
                return {"index": 0, "url": url, "page_url": url, "title": None, "output": None,
                        "stop_reason": "プレーヤー到達失敗", "played": False, "elapsed_seconds": 1.0,
                        "qa": {"status": "failed", "lines": 0, "chars": 0, "video_seconds": -1.0, "chars_per_min": 0.0,
                               "repeated_ratio": 0.0, "hallucination_ratio": 0.0, "warnings": ["x"]}}
            return page_runner(url, i, total, tr, args, session, stop_event)
        with patch("main._process_one_url_page", side_effect=runner), patch("main.time.sleep"), \
             patch("main.signal.signal"), patch("transcriber.Transcriber", return_value=MagicMock()), patch("subprocess.run"):
            rc = _run_batch(urls, argparse.Namespace(model="tiny", language="ja", cpu_threads=4, restore_to=None,
                                                     json_summary=None, output=d), session=sess)
        summary = Path(d) / "run-summary.json"
        check("1 本目 output=None でも例外なく rc=2", rc == 2, f"rc={rc}")
        check("サマリーは args.output 配下に書かれる", summary.exists())
        doc = json.loads(summary.read_text()) if summary.exists() else {}
        check("2 件とも記録・failed=1", doc.get("total") == 2 and doc.get("failed") == 1, f"{doc.get('total')} {doc.get('failed')}")
        check("course-page.txt も同じディレクトリ（CWD ではない）", (Path(d) / "course-page.txt").exists())
        # copy_to_downloads は output=None をスキップして落ちない
        (Path(d) / "講義1.txt").write_text("x")
        r = _sp.run([sys.executable, ".claude/skills/moodle-transcribe/copy_to_downloads.py", str(summary), "--dest", str(Path(d) / "dl")],
                    capture_output=True, text=True)
        check("copy_to_downloads: output=None をスキップし rc=0", r.returncode == 0 and "スキップ" in r.stdout, f"rc={r.returncode} {r.stdout[-200:]} {r.stderr[-200:]}")


def test_wait_for_player_unit() -> None:
    """#34: wait_for_player（ログイン待ち）を Chrome なしで拘束。永久ループ・中断・外部 SSO。"""
    print("\n[43] BrowserSession.wait_for_player（単体）")
    from browser import BrowserSession

    class Stub(BrowserSession):
        def __init__(self, urls):
            self._urls = list(urls); self._i = 0; self.front = 0; self.pumps = 0
        def url(self):
            u = self._urls[min(self._i, len(self._urls) - 1)]; return u
        def pump(self, ms=500):
            self.pumps += 1; self._i += 1
            time.sleep(ms / 1000.0)
            if self.pumps > 20000:
                raise RuntimeError("wait_for_player が終わらない（永久ループ）")
        def bring_to_front(self): self.front += 1

    P = "https://m.example.com/mod/scorm/player.php?a=1"
    L = "https://m.example.com/login/index.php"
    notified: list = []
    s = Stub([L, L, P])
    r = s.wait_for_player(timeout=0.5, login_timeout=5.0, on_login_required=notified.append, poll_ms=1)
    check("login → player で URL を返す", r == P)
    check("on_login_required は 1 回・bring_to_front は 1 回", len(notified) == 1 and s.front == 1, f"{len(notified)} {s.front}")

    s = Stub([L])
    t0 = time.monotonic(); r = s.wait_for_player(timeout=0.05, login_timeout=0.3, poll_ms=1); took = time.monotonic() - t0
    check("login のまま → login_timeout で None", r is None and 0.25 < took < 2.0, f"{r} {took:.2f}s")

    s = Stub(["https://m.example.com/mod/scorm/view.php?id=1"])
    t0 = time.monotonic(); r = s.wait_for_player(timeout=0.2, login_timeout=5.0, poll_ms=1); took = time.monotonic() - t0
    check("login 無し・marker 無し → timeout で None（timeout ぶんは待つ）", r is None and 0.2 <= took < 1.5, f"{took:.2f}s")

    # 修正対象だった永久ループ: login → /my/（marker も login も含まない）
    s = Stub([L, "https://m.example.com/my/"])
    t0 = time.monotonic(); r = s.wait_for_player(timeout=0.05, login_timeout=0.3, poll_ms=1); took = time.monotonic() - t0
    check("login → 非 marker ページ → login_timeout 以内に None（永久ループしない）", r is None and took < 2.0, f"{r} {took:.2f}s")

    # 外部 SSO（/login/ を含まない idp ドメイン）もログイン待ち扱い
    s = Stub(["https://idp.example.ac.jp/saml2/SSO", P])
    r = s.wait_for_player(timeout=0.05, login_timeout=5.0, poll_ms=1)
    check("外部 SSO (idp/saml) もログイン待ちとして扱い、戻れば URL", r == P and s.front == 1)
    check("looks_like_login: 通常のコースページは誤判定しない",
          not BrowserSession.looks_like_login("https://moodle.gs.chiba-u.jp/moodle/course/view.php?id=48556"))
    negatives = ["https://moodle.gs.chiba-u.jp/moodle/mod/lesson/view.php?id=1",          # "sso" ⊂ lesson
                 "https://moodle.gs.chiba-u.jp/moodle/mod/book/view.php?id=2&chapter=authors",  # "auth" ⊂ authors
                 "https://moodle.gs.chiba-u.jp/moodle/mod/assign/view.php?id=3",
                 "https://moodle.gs.chiba-u.jp/moodle/mod/scorm/player.php?a=1", "about:blank"]
    positives = ["https://moodle.gs.chiba-u.jp/moodle/login/index.php", "https://idp.example.ac.jp/saml2/SSO",
                 "https://moodle.gs.chiba-u.jp/moodle/auth/shibboleth/index.php", "https://sso.example.com/", 
                 "https://login.microsoftonline.com/x", "https://m.example.com/Shibboleth.sso/Login"]
    check("looks_like_login: lesson/authors/assign/player を誤判定しない", not any(BrowserSession.looks_like_login(u) for u in negatives),
          f"{[u for u in negatives if BrowserSession.looks_like_login(u)]}")
    check("looks_like_login: login/idp/auth/sso/shibboleth を拾う", all(BrowserSession.looks_like_login(u) for u in positives),
          f"{[u for u in positives if not BrowserSession.looks_like_login(u)]}")

    # 中断（Ctrl+C の stop_event）で即 None
    s = Stub([L])
    r = s.wait_for_player(timeout=5.0, login_timeout=600.0, should_abort=lambda: True, poll_ms=1)
    check("should_abort → 即 None", r is None and s.pumps == 0)


def test_browser_session_thread_boundary() -> None:
    """#35: BrowserSession の _call / start / close（Playwright なし）。"""
    print("\n[44] BrowserSession スレッド境界")
    import queue as _q
    from browser import BrowserSession

    class Stub(BrowserSession):
        def _run(self):
            # Playwright を起こさずコマンドを捌くだけ
            if getattr(self, "fail_start", False):
                self._start_error = RuntimeError("chrome missing"); self._ready.set(); return
            self._ready.set()
            while not self._stop.is_set():
                try:
                    cmd = self._q.get(timeout=0.02)
                except _q.Empty:
                    continue
                try:
                    cmd.result = cmd.fn()
                except BaseException as e:  # noqa: BLE001
                    cmd.error = e
                finally:
                    cmd.done.set()

    s = Stub(profile_dir="/tmp/x")
    try:
        s._call(lambda: 1)
        check("未 start の _call → RuntimeError", False)
    except RuntimeError:
        check("未 start の _call → RuntimeError", True)
    s.start(timeout=5)
    check("start 後に _call が結果を返す", s._call(lambda: 41 + 1) == 42)
    def boom(): raise ValueError("inner")
    try:
        s._call(boom); check("fn の例外は呼び出し側へ伝播", False)
    except ValueError as e:
        check("fn の例外は呼び出し側へ伝播", str(e) == "inner")
    try:
        s._call(lambda: time.sleep(0.3), timeout=0.05); check("timeout 超過 → TimeoutError", False)
    except TimeoutError:
        check("timeout 超過 → TimeoutError", True)
    s.close()
    check("close() 後は _thread が None", s._thread is None)

    s2 = Stub(profile_dir="/tmp/x"); s2.fail_start = True
    try:
        s2.start(timeout=5); check("起動失敗 → start() が RuntimeError", False)
    except RuntimeError as e:
        check("起動失敗 → start() が RuntimeError（原因を保持）", "chrome missing" in str(e) and isinstance(e.__cause__, RuntimeError))


def test_run_page_engine_branch() -> None:
    """#36: run() の page 分岐（BrowserSession をパッチ）。"""
    print("\n[45] run() の page エンジン分岐")
    from unittest.mock import MagicMock, patch
    from main import build_parser, run

    def go(argv, start_error=None, batch_rc=0):
        sess = MagicMock()
        if start_error:
            sess.start.side_effect = RuntimeError(start_error)
        with patch("browser.BrowserSession", return_value=sess) as BS, \
             patch("main._run_batch", return_value=batch_rc) as rb:
            rc = run(build_parser().parse_args(argv))
        return rc, sess, BS, rb

    rc, sess, BS, rb = go(["--moodle-url", "https://m.example.com/mod/scorm/player.php?a=1", "--profile-dir", "/tmp/prof", "--headless"])
    check("--moodle-url がフル URL なら 1 本のバッチに昇格", rb.called and rb.call_args.args[0] == ["https://m.example.com/mod/scorm/player.php?a=1"])
    check("profile_dir / headless が BrowserSession に渡る", BS.call_args.kwargs["profile_dir"] == Path("/tmp/prof") and BS.call_args.kwargs["headless"] is True)
    check("正常終了で session.close()", sess.close.called and rc == 0)

    rc, sess, BS, rb = go(["--moodle-url", "example.com"])
    check("URL 無し（パターンのみ）→ rc=1・ブラウザを起こさない", rc == 1 and not BS.called)

    rc, sess, BS, rb = go(["--urls", "https://m.example.com/x?a=1"], start_error="no chrome")
    check("start 失敗 → rc=1・バッチを回さない", rc == 1 and not rb.called)

    rc, sess, BS, rb = go(["--urls", "https://m.example.com/x?a=1", "https://m.example.com/x?a=2"], batch_rc=2)
    check("バッチの rc をそのまま返し、close する", rc == 2 and sess.close.called)


def test_monitor_total_guard() -> None:
    """#37: PlaybackMonitor.total は最後の poll で取得失敗(-1)しても直前の正の値を保つ。"""
    print("\n[46] PlaybackMonitor.total の保持")
    from unittest.mock import patch
    from main import PlaybackMonitor
    times = iter([(30.0, 262.0), (60.0, 262.0), (-1.0, -1.0)])
    with patch("main.get_viewing_percentage", return_value=-1), patch("main.get_video_ended", return_value=False), \
         patch("main.get_video_time", side_effect=lambda *a, **k: next(times)), patch("main._print_progress"):
        m = PlaybackMonitor(1, "chrome")
        for _ in range(3):
            m.poll(silent=False)
    check("取得失敗後も total=262 を保持", m.total == 262.0, f"{m.total}")

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
    test_progress_bar()
    test_multi_url_utils()
    test_window_keep_alive_save_interval()
    test_new_args()
    test_pct100_auto_stop()
    test_single_url_unique_path()
    test_run_batch_all_urls()
    test_run_batch_stop_event()
    test_process_one_url_navigate_and_buttons()
    test_play_btn_pos_js_iframe_priority()
    test_keep_active_only_mode()
    test_no_transcribe_requires_url()
    test_main_hooks()
    test_capture_stop_idempotent()
    test_transcriber_dedup_and_reset()
    test_playback_monitor_fallbacks()
    test_keep_alive_ended_guard()
    test_video_ended_tristate()
    test_tab_condition_string_compare()
    test_trigger_video_play_verifies_playback()
    test_playback_monitor_not_started()
    test_downmix_antiphase()
    test_boundary_dedup_fuzzy()
    test_playback_monitor_rewind_after_end()
    test_callers_skip_save_exit_when_not_played()
    test_qa_metrics()
    test_run_batch_summary()
    test_process_one_url_returns_entry()
    test_qa_boundaries()
    test_run_batch_interrupt_and_exception()
    test_process_one_url_interrupt()
    test_warn_other_audio_apps()
    test_page_capture()
    test_process_one_url_page()
    test_page_engine_live()
    test_course_page_dump_and_page_batch()
    test_wait_for_player_unit()
    test_browser_session_thread_boundary()
    test_run_page_engine_branch()
    test_monitor_total_guard()

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
