"""Moodle講義音声 オンデバイス自動文字起こし"""

import argparse
import platform
import re
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from capture import create_capture, find_loopback_device, list_all_devices
from output import OutputFormat, OutputWriter, _fmt_ts
from platform_utils import (
    WindowKeepAlive,
    click_save_button,
    get_moodle_page_info,
    get_viewing_percentage,
    navigate_to_url,
    restore_audio_output,
    setup_audio_for_capture,
    trigger_video_play,
)
from transcriber import Transcriber

SYSTEM = platform.system()


# ─── ターミナルカラー ──────────────────────────────────────────────────────────

def _c(s: str, code: str = "0") -> str:
    """tty のときだけ ANSI カラーを適用する。"""
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"

_GRN  = lambda s: _c(s, "32")       # 緑
_CYN  = lambda s: _c(s, "36")       # シアン
_BLU  = lambda s: _c(s, "34")       # 青
_YLW  = lambda s: _c(s, "33")       # 黄
_DIM  = lambda s: _c(s, "2")        # 暗め
_BOLD = lambda s: _c(s, "1")        # 太字
_BGRN = lambda s: _c(s, "1;32")     # 太字緑


def _print_session_info(rows: list[tuple[str, str]]) -> None:
    """セッション情報をきれいに表示する。"""
    label_w = max(len(r[0]) for r in rows)
    bar = _DIM("  " + "─" * (label_w + 36))
    print(bar)
    for label, value in rows:
        print(f"  {_DIM(f'{label:<{label_w}}')}  {value}")
    print(bar)


# ─── ユーティリティ ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Moodle講義音声をオンデバイスで文字起こし (BlackHole / WASAPI / PulseAudio + Whisper)"
    )
    p.add_argument(
        "-o", "--output",
        default=None,
        help="出力ファイルパス。指定時はout/YYYYMMDD/を使わずそのまま保存 (例: -o 解剖学_第3回)",
    )
    p.add_argument("-f", "--format", choices=["txt", "srt", "vtt"], default="txt", help="出力フォーマット")
    p.add_argument(
        "-m", "--model",
        choices=["tiny", "base", "small", "medium", "large-v3"],
        default="large-v3",
        help="Whisperモデルサイズ（デフォルト: large-v3）",
    )
    p.add_argument("-l", "--language", default="ja", help="文字起こし言語 ISO 639-1（デフォルト: ja）")
    p.add_argument("-d", "--device", type=int, default=None, help="入力デバイスインデックス（デフォルト: 自動検出）")
    p.add_argument("--segment-duration", type=float, default=30.0, help="セグメント長（秒）")
    p.add_argument("--overlap", type=float, default=5.0, help="オーバーラップ長（秒）")
    p.add_argument("--cpu-threads", type=int, default=8, help="CPUスレッド数")
    p.add_argument(
        "--keep-active",
        metavar="BROWSER",
        default=None,
        help="Moodleタブをアクティブに保つ: chrome / arc / safari / firefox / edge",
    )
    p.add_argument(
        "--moodle-url",
        metavar="URL_PATTERN",
        default=None,
        help="MoodleタブのURLパターン (例: 'moodle.example.com')。--keep-active と併用で特定タブに注入",
    )
    p.add_argument(
        "--keep-interval",
        type=float,
        default=20.0,
        help="JS再注入の間隔（秒）（デフォルト: 20）",
    )
    p.add_argument(
        "--save-interval",
        type=float,
        default=60.0,
        help="「視聴状況を保存」ボタンクリック間隔（秒）（デフォルト: 60、0 で無効）",
    )
    p.add_argument(
        "--urls",
        metavar="URL",
        nargs="+",
        help="複数 URL を順次処理（例: --urls URL1 URL2 URL3）",
    )
    p.add_argument(
        "--url-file",
        metavar="FILE",
        help="URL を1行1件で記載したファイル（--urls と組み合わせ可）",
    )
    p.add_argument(
        "--no-auto-routing",
        action="store_true",
        help="音声出力の自動切替を無効にする（手動で設定済みの場合）",
    )
    p.add_argument(
        "--restore-to",
        metavar="DEVICE",
        default=None,
        help="終了時に戻す出力デバイス名 (例: 'MacBook Proのスピーカー')。省略時は起動前のデバイスに戻す",
    )
    p.add_argument("--list-devices", action="store_true", help="利用可能な入力デバイス一覧を表示して終了")
    p.add_argument(
        "--reset-audio",
        action="store_true",
        help="音声出力をスピーカーに戻して終了 (--restore-to で対象デバイスを指定可)",
    )
    p.add_argument(
        "--timestamps",
        action="store_true",
        help="各行に [HH:MM:SS] タイムスタンプを付与（デフォルト: オフ）",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="詳細ログを表示")
    return p


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:80]


def _prompt_rename(path: Path) -> Path:
    """Ctrl+C 後に現在のファイル名を表示し、変更するか確認する。"""
    print(f"\n現在のファイル名: {_CYN(str(path))}")
    try:
        answer = input("変更する場合は新しい名前を入力 (Enter でそのまま): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return path
    if not answer:
        return path
    new_name = _sanitize_filename(answer)
    new_path = path.parent / f"{new_name}{path.suffix}"
    if new_path == path:
        return path
    path.rename(new_path)
    print(f"→ {_CYN(str(new_path))} に変更しました")
    return new_path


def _unique_path(path: Path) -> Path:
    """既存ファイルと被る場合は _2, _3 ... を付けて一意にする。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    parent = path.parent
    i = 2
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def _url_to_pattern(url: str) -> str:
    """Full URL からタブ検索に使う一意パターンを抽出する。"""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "id" in qs:
        return f"id={qs['id'][0]}"
    return parsed.netloc + parsed.path


def _collect_urls(args: argparse.Namespace) -> list[str]:
    urls: list[str] = []
    if args.urls:
        urls.extend(args.urls)
    if args.url_file:
        p = Path(args.url_file)
        if p.exists():
            urls.extend(line.strip() for line in p.read_text().splitlines() if line.strip())
    return urls


def _process_one_url(
    url: str,
    idx: int,
    total: int,
    transcriber: Transcriber,
    args: argparse.Namespace,
    device_index: int | None,
    device_name: str,
    current: dict,
    stop_event: threading.Event,
) -> None:
    browser = args.keep_active
    url_pattern = _url_to_pattern(url)

    print(f"\n{_BOLD(f'[{idx+1}/{total}]')} {_CYN(url)}")

    navigate_to_url(url, browser)
    print("  ページ読み込み中...")
    time.sleep(6)

    page_title, page_url = get_moodle_page_info(url_pattern, browser)

    fmt = OutputFormat(args.format)
    if args.output:
        base = args.output if total == 1 else f"{args.output}_{idx+1:02d}"
        output_path = _unique_path(Path(f"{base}.{fmt.value}"))
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        now = datetime.now()
        out_dir = Path("out") / now.strftime("%Y%m%d")
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = _sanitize_filename(page_title) if page_title else f"lecture_{now.strftime('%H%M%S')}"
        output_path = _unique_path(out_dir / f"{filename}.{fmt.value}")

    info_rows: list[tuple[str, str]] = []
    if page_title:
        info_rows.append(("ページ", _GRN(page_title)))
    info_rows.append(("デバイス", _CYN(f"[{device_index}] {device_name}")))
    info_rows.append(("出力", _CYN(str(output_path))))
    _print_session_info(info_rows)

    writer = OutputWriter(output_path, fmt, args.model, args.language,
                          title=page_title, page_url=page_url or url)
    capture = create_capture(device_index, args.segment_duration, args.overlap)
    keep_alive = WindowKeepAlive(
        browser, args.keep_interval,
        url_pattern=url_pattern,
        save_interval=args.save_interval,
    )
    current["keep_alive"] = keep_alive
    current["capture"] = capture

    print(f"\n{_BGRN('▶  録音開始')}  {_DIM('Ctrl+C で全停止')}\n")
    capture.start()
    keep_alive.start()
    trigger_video_play(url_pattern, browser)

    try:
        for segment in capture.segments():
            if stop_event.is_set():
                break
            if args.verbose:
                print(f"[セグメント {segment.segment_id}] {segment.start_time:.1f}秒〜")
            results = transcriber.transcribe(segment.data, time_offset=segment.start_time)
            for r in results:
                prefix = f"{_DIM(f'[{_fmt_ts(r.start)}]')} " if args.timestamps else ""
                print(f"{prefix}{r.text}")
                writer.append(r)
            pct = get_viewing_percentage(url_pattern, browser)
            if pct >= 100:
                print(f"\n{_BGRN('視聴完了')} ({pct}%) — 次の URL へ")
                capture.stop()
    finally:
        keep_alive.stop()
        writer.finalize()

    print(f"{_BGRN('保存完了')}: {_CYN(str(output_path))}")
    current["keep_alive"] = None
    current["capture"] = None


def _run_batch(
    urls: list[str],
    args: argparse.Namespace,
    device_index: int | None,
    device_name: str,
    routing_changed: bool,
) -> int:
    print(f"\n{_BOLD(f'{len(urls)} 件の講義を順次処理します')}")
    print("  Whisperモデルを読み込み中...")

    transcriber = Transcriber(
        model_size=args.model,
        language=args.language,
        cpu_threads=args.cpu_threads,
    )

    stop_event = threading.Event()
    current: dict = {"keep_alive": None, "capture": None}

    def handle_sigint(sig, frame):
        if not stop_event.is_set():
            stop_event.set()
            print("\n停止中... バッファを処理しています")
            if current["keep_alive"]:
                current["keep_alive"].stop()
            if current["capture"]:
                current["capture"].stop()

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        for i, url in enumerate(urls):
            if stop_event.is_set():
                break
            _process_one_url(url, i, len(urls), transcriber, args,
                             device_index, device_name, current, stop_event)
            if not stop_event.is_set() and i < len(urls) - 1:
                print(f"\n  次の URL まで {_DIM('5秒待機...')}")
                time.sleep(5)
    finally:
        if routing_changed:
            restore_audio_output(restore_to=args.restore_to)

    if not stop_event.is_set():
        if SYSTEM == "Darwin":
            import subprocess as _sp
            _sp.run(
                ["osascript", "-e",
                 f'display notification "{len(urls)}件の講義を文字起こし完了" with title "moodle-transcriber"'],
                capture_output=True,
            )
        print(f"\n{_BGRN('全講義の処理が完了しました')} ({len(urls)}件)")

    return 0


def run(args: argparse.Namespace) -> int:
    if args.list_devices:
        list_all_devices()
        return 0

    if args.reset_audio:
        import shutil, subprocess as sp
        if shutil.which("SwitchAudioSource"):
            target = args.restore_to or "MacBook Proのスピーカー"
            sp.run(["SwitchAudioSource", "-s", target], capture_output=True)
            print(f"音声出力 → {_CYN(repr(target))} に切り替えました")
        else:
            print("エラー: brew install switchaudio-osx が必要です")
        return 0

    if args.moodle_url and not args.keep_active:
        args.keep_active = "chrome"

    # 複数 URL モードの判定（--urls / --url-file）
    urls = _collect_urls(args)
    if urls and not args.keep_active:
        args.keep_active = "chrome"

    # 音声ルーティング自動切替（macOS + SwitchAudioSource がある場合）
    routing_changed = False
    if not args.no_auto_routing:
        routing_changed = setup_audio_for_capture()

    # デバイス検出
    if SYSTEM != "Windows":
        try:
            device_index = args.device if args.device is not None else find_loopback_device()
        except RuntimeError as e:
            print(f"エラー: {e}", file=sys.stderr)
            if routing_changed:
                restore_audio_output()
            return 1
        import sounddevice as sd
        device_name = sd.query_devices(device_index)['name']
    else:
        device_index = None
        device_name = "WASAPI Loopback（自動検出）"

    # 複数 URL モード
    if urls:
        return _run_batch(urls, args, device_index, device_name, routing_changed)

    # ─── 以下、シングル URL モード（既存の動作） ─────────────────────────────

    # ページ情報取得（Chrome タブの DOM から読む、サーバーリクエストなし）
    page_title, page_url = None, None
    if args.moodle_url and args.keep_active:
        page_title, page_url = get_moodle_page_info(args.moodle_url, args.keep_active)

    # 出力パス: -o 指定時はそのまま保存、未指定は ./out/YYYYMMDD/ 以下に自動生成
    fmt = OutputFormat(args.format)
    if args.output:
        output_path = Path(f"{args.output}.{fmt.value}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        now = datetime.now()
        out_dir = Path("out") / now.strftime("%Y%m%d")
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = _sanitize_filename(page_title) if page_title else f"lecture_{now.strftime('%H%M%S')}"
        output_path = out_dir / f"{filename}.{fmt.value}"

    # セッション情報を整形表示
    info_rows: list[tuple[str, str]] = []
    if page_title:
        info_rows.append(("ページ", _GRN(page_title)))
    info_rows.append(("デバイス", _CYN(f"[{device_index}] {device_name}")))
    info_rows.append(("出力", _CYN(str(output_path))))
    info_rows.append(("モデル", _BLU(f"{args.model} (int8) | {args.language}")))
    _print_session_info(info_rows)

    print(_YLW("  音量 0 でも BlackHole 録音は継続します"))
    print("  Whisperモデルを読み込み中...")

    transcriber = Transcriber(
        model_size=args.model,
        language=args.language,
        cpu_threads=args.cpu_threads,
    )
    writer = OutputWriter(output_path, fmt, args.model, args.language,
                          title=page_title, page_url=page_url)
    capture = create_capture(device_index, args.segment_duration, args.overlap)

    keep_alive: WindowKeepAlive | None = None
    if args.keep_active:
        keep_alive = WindowKeepAlive(
            args.keep_active, args.keep_interval,
            url_pattern=args.moodle_url,
            save_interval=args.save_interval,
        )

    shutdown_requested = False

    def handle_sigint(sig, frame):
        nonlocal shutdown_requested
        if not shutdown_requested:
            shutdown_requested = True
            print("\n停止中... バッファを処理しています")
            if keep_alive:
                keep_alive.stop()
            capture.stop()

    signal.signal(signal.SIGINT, handle_sigint)

    print(f"\n{_BGRN('▶  録音開始')}  Moodleで動画を再生してください。{_DIM('Ctrl+C で停止')}\n")
    capture.start()
    if keep_alive:
        keep_alive.start()

    try:
        for segment in capture.segments():
            if args.verbose:
                print(f"[セグメント {segment.segment_id}] {segment.start_time:.1f}秒〜")
            results = transcriber.transcribe(segment.data, time_offset=segment.start_time)
            for r in results:
                prefix = f"{_DIM(f'[{_fmt_ts(r.start)}]')} " if args.timestamps else ""
                print(f"{prefix}{r.text}")
                writer.append(r)
    finally:
        writer.finalize()
        if routing_changed:
            restore_audio_output(restore_to=args.restore_to)
        output_path = _prompt_rename(output_path)
        print(f"\n{_BGRN('保存完了')}: {_CYN(str(output_path))}")

    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
