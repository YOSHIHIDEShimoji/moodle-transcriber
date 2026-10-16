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
            _process_one_url(url, 0, 1, tr, args, 0, "BlackHole 2ch", {"keep_alive": None, "capture": None},
                             threading.Event())
        return saves, exits, triggers, ka, cap

    saves, exits, triggers, ka, cap = run_process((0.0, 262.0), False, 0)
    check("_process_one_url 未再生: 保存クリック 0 回", len(saves) == 0, f"saves={len(saves)}")
    check("_process_one_url 未再生: 離脱クリック 0 回", len(exits) == 0, f"exits={len(exits)}")
    check("_process_one_url 未再生: 再生リトライが打たれた（初回 + 再試行）",
          len(triggers) == 1 + PlaybackMonitor.NOT_STARTED_MAX_RETRY, f"triggers={len(triggers)}")
    check("_process_one_url 未再生: keep_alive.mark_ended/stop は呼ばれる", ka.mark_ended.called and ka.stop.called)
    check("_process_one_url 未再生: capture.stop は呼ばれる", cap.stop.called)

    saves, exits, _, _, _ = run_process((3600.0, 3600.0), True, 100)
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
        with tempfile.TemporaryDirectory() as d:
            args = build_parser().parse_args(["--moodle-url", "example.com", "--no-auto-routing",
                                              "--output", d, "--model", "tiny"])
            with patch.dict(sys.modules, {"sounddevice": sd_mod}), \
                 patch("main.find_loopback_device", return_value=0), \
                 patch("main.get_tab_id_by_pattern", return_value=42), \
                 patch("main.get_tab_url", return_value=url), \
                 patch("main.get_moodle_page_info", return_value=("講義", url)), \
                 patch("main.create_capture", return_value=cap), \
                 patch("main.WindowKeepAlive", return_value=ka), \
                 patch("main._trigger_with_retry"), \
                 patch("main.get_viewing_percentage", return_value=pct), \
                 patch("main.get_video_ended", return_value=ended), \
                 patch("main.get_video_time", return_value=video_time), \
                 patch("main.click_save_button", side_effect=lambda *a, **kw: saves.append(1)), \
                 patch("main.click_exit_activity_button", side_effect=lambda *a, **kw: exits.append(1)), \
                 patch("main._prompt_rename", side_effect=lambda p: p), \
                 patch("main.OutputWriter"), patch("main._print_progress"), \
                 patch("main.time.sleep"), patch("main.signal.signal"), \
                 patch("transcriber.Transcriber", return_value=MagicMock(transcribe=MagicMock(return_value=[]))):
                rc = run(args)
        return rc, saves, exits, ka

    rc, saves, exits, ka = run_single((0.0, 262.0), False, 0)
    check("run() 未再生: rc=0 で終了", rc == 0, f"rc={rc}")
    check("run() 未再生: 保存/離脱 0 回", len(saves) == 0 and len(exits) == 0, f"saves={len(saves)} exits={len(exits)}")
    check("run() 未再生: 停止分岐に到達（keep_alive.mark_ended）", ka.mark_ended.called)
    rc, saves, exits, _ = run_single((3600.0, 3600.0), True, 100)
    check("run() 再生済み(対照): 保存/離脱 1 回ずつ", len(saves) == 1 and len(exits) == 1,
          f"saves={len(saves)} exits={len(exits)}")

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
