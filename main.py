"""Moodle講義音声 オンデバイス自動文字起こし"""

import argparse
import logging
import logging.handlers
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import parse_qs, urlparse

import numpy as np

from capture import create_capture, find_loopback_device, list_all_devices
from output import OutputFormat, OutputWriter, _fmt_ts
from platform_utils import (
    warn_other_audio_apps,
    TabTarget,
    WindowKeepAlive,
    click_exit_activity_button,
    click_save_button,
    get_active_tab_id,
    get_active_tab_url,
    get_moodle_page_info,
    get_tab_id_by_pattern,
    get_tab_url,
    get_video_ended,
    get_video_time,
    get_viewing_percentage,
    navigate_to_url,
    restore_audio_output,
    setup_audio_for_capture,
    trigger_video_play,
    wait_for_page_ready,
)
from transcriber import is_silent
from qa import qa_metrics, write_run_summary, format_qa_line

# faster-whisper のロードを遅延させるため Transcriber は使う直前に import する。
# --no-transcribe 時はモデル読み込みコスト（数百MB）を避けたい。
if TYPE_CHECKING:
    from transcriber import Transcriber
    from browser import BrowserSession

SYSTEM = platform.system()


def _setup_logging() -> logging.Logger:
    # テストや別環境からログ先を差し替えられるようにする（本番ログに偽シグネチャを混ぜない）
    log_dir = Path(os.environ.get("MOODLE_TRANSCRIBER_LOG_DIR", "logs"))
    log_dir.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "moodle-transcriber.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger = logging.getLogger("moodle-transcriber")
    _logger.setLevel(logging.DEBUG)
    _logger.addHandler(handler)
    return _logger


logger = _setup_logging()


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


def _fmt_progress(pct: int, cur_s: float, total_s: float) -> str:
    bar_len = 20
    filled = int(bar_len * max(0, min(pct, 100)) / 100)
    bar = "█" * filled + "░" * (bar_len - filled)
    cur = _fmt_ts(cur_s) if cur_s >= 0 else "--:--:--"
    tot = _fmt_ts(total_s) if total_s > 0 else "--:--:--"
    return f"  視聴 {bar} {pct:3d}%  [{cur} / {tot}]"


# 外部から差し替え可能な hook（None なら CLI 既定動作）
_progress_hook = None  # type: ignore[var-annotated]
_rename_hook = None  # type: ignore[var-annotated]


def _print_progress(pct: int, cur_s: float, total_s: float) -> None:
    """進捗バーを表示する。TTY なら \\r で同じ行を上書き、非TTYなら改行付き。

    ログファイル (logs/moodle-transcriber.log) には書き出さない（DEBUG ログの
    pct/ended 値だけで十分なため、進捗バー文字列をログから除外する）。

    `_progress_hook` がセットされていれば CLI 出力をスキップしてそちらに委譲する。
    """
    if _progress_hook is not None:
        _progress_hook(pct, cur_s, total_s)
        return
    line = _DIM(_fmt_progress(pct, cur_s, total_s))
    if sys.stdout.isatty():
        # \r で行頭に戻し、行末まで消去（ESC[K）してから書き直す
        sys.stdout.write(f"\r\033[K{line}")
        sys.stdout.flush()
    else:
        print(line)


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
        help="出力ディレクトリ。指定時はout/YYYYMMDD/の代わりにそのディレクトリへ保存 (例: -o 解剖学)",
    )
    p.add_argument("-f", "--format", choices=["txt", "srt", "vtt"], default="txt", help="出力フォーマット")
    p.add_argument(
        "-m", "--model",
        choices=["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"],
        default="large-v3-turbo",
        help="Whisperモデルサイズ（デフォルト: large-v3-turbo）",
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
    p.add_argument(
        "--engine", choices=["page", "blackhole"], default="page",
        help="page: 専用 Chrome（別プロファイル）を Playwright で動かしページ内で音声を取る（既定・ユーザーの Chrome に触らない） / "
             "blackhole: 従来方式（ユーザーの Chrome を AppleScript で操作し BlackHole で録音）",
    )
    p.add_argument(
        "--profile-dir", default=None,
        help="page エンジンの Chrome プロファイル（既定: ~/.moodle-transcriber/chrome-profile）",
    )
    p.add_argument("--headless", action="store_true", help="page エンジンのブラウザを非表示で起動（ログインが必要なときは使えない）")
    p.add_argument(
        "--login-timeout", type=float, default=600.0,
        help="page エンジンでログイン画面に落ちたとき、本人のログインを待つ秒数（既定 600）",
    )
    p.add_argument(
        "--player-marker", default="player.php",
        help="page エンジンで『プレーヤーページに到達した』と判定する URL の部分文字列（既定 player.php）",
    )
    p.add_argument(
        "--json-summary", metavar="PATH", default=None,
        help="複数 URL モードの実行サマリー(JSON)の出力先（デフォルト: 出力ディレクトリ/run-summary.json）",
    )
    p.add_argument(
        "--no-transcribe",
        action="store_true",
        help="文字起こしせず Chrome タブの自動再生・視聴管理だけ行う（URL必須）",
    )
    return p


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:80]


def _trigger_with_retry(target: "TabTarget", browser: str, attempts: int = 5, interval: float = 3.0) -> None:
    """動画再生を試みる。SCORMプレーヤーの初期化待ちのためリトライする。"""
    for i in range(attempts):
        if trigger_video_play(target, browser):
            logger.info("video play triggered (attempt %d)", i + 1)
            return
        logger.warning("trigger_video_play: not started (attempt %d/%d)", i + 1, attempts)
        if i < attempts - 1:
            time.sleep(interval)
    print("  警告: 動画の再生を開始できませんでした。手動で再生してください。")
    logger.warning("trigger_video_play: gave up after %d attempts", attempts)


def _prompt_rename(path: Path, timeout: int = 60) -> Path:
    if _rename_hook is not None:
        return _rename_hook(path)
    use_alarm = SYSTEM != "Windows" and hasattr(signal, "SIGALRM")
    print(f"\n現在のファイル名: {_CYN(str(path))}")
    print(f"変更する場合は新しい名前を編集 ({timeout}秒で自動確定): ", end="", flush=True)

    prefill = path.stem
    try:
        import readline
        def _hook():
            readline.insert_text(prefill)
            readline.redisplay()
        readline.set_pre_input_hook(_hook)
    except Exception:
        readline = None  # type: ignore[assignment]

    answer = ""
    if use_alarm:
        def _alarm(sig, frame):
            raise TimeoutError()
        old_handler = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(timeout)
    try:
        answer = input("").strip()
    except (TimeoutError, EOFError, KeyboardInterrupt):
        print()
    finally:
        if use_alarm:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
        if readline is not None:
            try:
                readline.set_pre_input_hook(None)
            except Exception:
                pass
    if not answer or answer == prefill:
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
    """Full URL からタブ検索に使う一意パターンを抽出する。

    可能なら "player.php?a=123" のようにパス付きで返し、
    "a=123" が無関係なタブの URL（scoid=1230 等）に部分一致する事故を減らす。
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "id" in qs:
        cand = f"view.php?id={qs['id'][0]}"
        return cand if cand in url else f"id={qs['id'][0]}"
    # SCORM player URL: player.php?a=XXXXX (リダイレクト後)
    if "a" in qs:
        cand = f"player.php?a={qs['a'][0]}"
        return cand if cand in url else f"a={qs['a'][0]}"
    return parsed.netloc + parsed.path


def _resolve_tab_target(url: str, browser: str) -> tuple["TabTarget", str, str]:
    """navigate 直後に呼び、追跡すべきタブと実 URL を解決する。

    Returns: (target, url_pattern, actual_url)
    - タブ id が取れればそれを target にする（ユーザーがタブを切り替えても誤爆しない）
    - リダイレクト完了は固定 sleep でなくポーリングで待つ
    """
    tab_id = get_active_tab_id(browser)
    actual_url = wait_for_page_ready(tab_id, browser) or url
    url_pattern = _url_to_pattern(actual_url)
    target: TabTarget = tab_id if tab_id is not None else url_pattern
    logger.info("target resolved: tab_id=%s pattern=%r (actual_url: %r)",
                tab_id, url_pattern, actual_url)
    return target, url_pattern, actual_url


class PlaybackMonitor:
    """動画終了の判定器。video.ended を一次シグナルに、取得不能でも止まれる
    フォールバック（currentTime 停滞 / pct=100 / 無音継続 / 総尺超過）を重ねる。

    背景: SCORM 教材の iframe 構成によっては JS から video 要素に到達できず、
    ended/pct が一切取れない（ログ上 pct=-1 ended=None が延々と続く）。
    従来はその場合に永遠に止まらなかった。
    """

    STALL_POLLS = 4          # currentTime が動かない連続 poll 数 → 終了扱い
    NEAR_END_POLLS = 2       # currentTime が総尺付近で停滞する連続 poll 数
    PCT100_POLLS = 3         # 視聴状況 100% の連続 poll 数
    SILENCE_AFTER_AUDIO = 6  # 音が出た後の無音セグメント数 → 終了扱い（約3分）
    SILENCE_RETRIGGER = 4    # 一度も音が出ないとき再生リトライを打つ無音数（約2分）
    SILENCE_NO_AUDIO = 12    # 一度も音が出ないまま諦めるまでの無音数（約6分）
    BLIND_WARN_POLLS = 10    # 全シグナル不能の警告を出すまでの poll 数
    NOT_STARTED_RETRIGGER = 2  # currentTime が 0 のままの poll 数 → 再生リトライ（約1分ごと）
    NOT_STARTED_MAX_RETRY = 2  # 未開始リトライの上限
    NOT_STARTED_GIVEUP = 8     # 未開始のまま諦める poll 数（約4分）
    STARTED_CUR = 1.0          # これ以上 currentTime が進んだら「再生開始済み」

    def __init__(
        self,
        target: "TabTarget",
        browser: str,
        retrigger: "Callable[[], None] | None" = None,
        probe: "object | None" = None,
    ):
        """probe: pct() / ended() / time() を持つオブジェクト。None なら従来の
        AppleScript 経路（platform_utils の関数）を使う。page エンジンは BrowserSession を渡す。"""
        self._target = target
        self._browser = browser
        self._retrigger = retrigger
        self._probe = probe
        self._last_cur = -1.0
        self._stall = 0
        self._near_end = 0
        self._pct100 = 0
        self._silent_run = 0
        self._heard_audio = False
        self._retriggered = False
        self._blind = 0
        self._blind_warned = False
        self._not_started = 0
        self._not_started_retries = 0
        self._max_cur = -1.0
        self.total = -1.0   # 直近に観測した動画総尺（秒）。QA の分母に使う
        # 再生が実際に始まった（currentTime が進んだ）ことを観測したか。
        # False のまま終了した場合、呼び出し側は「視聴状況を保存」「活動から抜ける」を押さない。
        self.played = False

    def poll(self, silent: bool | None = None, elapsed: float | None = None) -> tuple[bool, str]:
        """1 セグメント（または poll_interval）ごとに呼ぶ。

        Returns: (終了すべきか, 理由)
        """
        if self._probe is not None:
            pct = self._probe.pct()
            ended = self._probe.ended()
            cur, total = self._probe.time()
        else:
            pct = get_viewing_percentage(self._target, self._browser)
            ended = get_video_ended(self._target, self._browser)
            cur, total = get_video_time(self._target, self._browser)
        logger.debug("poll: pct=%d ended=%s cur=%.1f total=%.1f silent=%s elapsed=%s",
                     pct, ended, cur, total, silent, elapsed)
        if total > 0:
            self.total = total

        # 進捗表示（pct が取れなければ currentTime/duration から算出）
        disp_pct = pct
        if disp_pct < 0 and cur >= 0 and total > 0:
            disp_pct = round(cur / total * 100)
        if disp_pct >= 0:
            _print_progress(disp_pct, cur, total)

        # 再生が実際に始まった（currentTime が進んだ / ended を観測した）
        if cur >= self.STARTED_CUR or ended is True:
            self.played = True

        # ── 一次シグナル: video.ended ────────────────────────────────
        if ended is True:
            return True, "video.ended"

        # ── フォールバック1: currentTime が総尺付近で停滞 ─────────────
        if cur >= 0 and total > 0 and cur >= total - 1.5:
            self._near_end += 1
            if self._near_end >= self.NEAR_END_POLLS:
                return True, "動画末尾に到達"
        else:
            self._near_end = 0

        # ── フォールバック3: 視聴状況 100% が継続 ─────────────────────
        # 未開始判定より前に置く: 既視聴 (pct=100) の URL で再生が始まらなくても
        # 4 分待たずに終了する。視聴済みなので保存/離脱クリックは有効 (played=True)。
        if pct >= 100:
            self._pct100 += 1
            if self._pct100 >= self.PCT100_POLLS:
                self.played = True
                return True, "視聴状況 100%"
        else:
            self._pct100 = 0

        # ── 再生未開始: currentTime が 0 付近のまま動かない ───────────
        # 停滞（フォールバック2）と区別する。未開始のまま「視聴完了」扱いにすると
        # 空ファイルを保存して「活動から抜ける」まで押してしまう（2026-10-16 に実測）。
        if cur >= 0 and not self.played:
            self._not_started += 1
            if (self._not_started % self.NOT_STARTED_RETRIGGER == 0
                    and self._not_started_retries < self.NOT_STARTED_MAX_RETRY
                    and self._retrigger):
                self._not_started_retries += 1
                print("\n  警告: 動画が再生されていません。再生を再試行します...")
                logger.warning("video not started — retriggering playback (%d/%d)",
                               self._not_started_retries, self.NOT_STARTED_MAX_RETRY)
                self._retrigger()
            if self._not_started >= self.NOT_STARTED_GIVEUP:
                return True, "再生を開始できないため中止"
            self._last_cur = cur
            return False, ""

        # ── 末尾到達後の巻き戻し: SCORM プレーヤーは終了時に ended を立てず
        # currentTime を 0 に戻すことがある（2026-10-16 実測: 258s → 0s）。
        # 停滞判定（約 75 秒）を待たずに終了扱いにする。
        if cur >= 0:
            self._max_cur = max(self._max_cur, cur)
            if (self.played and total > 0 and self._max_cur >= total - 5.0
                    and cur < self.STARTED_CUR):
                return True, "動画末尾に到達（巻き戻し検出）"

        # ── フォールバック2: currentTime の停滞（再生が止まったまま）──
        if cur >= 0:
            if self._last_cur >= 0 and abs(cur - self._last_cur) < 0.5:
                self._stall += 1
            else:
                self._stall = 0
            self._last_cur = cur
            if self._stall >= self.STALL_POLLS:
                return True, "再生停止（currentTime 停滞）"

        # ── フォールバック4: 無音の継続（文字起こしモードのみ）────────
        if silent is not None:
            if silent:
                self._silent_run += 1
                if self._heard_audio:
                    if self._silent_run >= self.SILENCE_AFTER_AUDIO:
                        return True, "無音継続（音声終了とみなす）"
                else:
                    if (self._silent_run == self.SILENCE_RETRIGGER
                            and not self._retriggered and self._retrigger):
                        print("\n  警告: 音声が検出されません。再生を再試行します...")
                        logger.warning("no audio yet — retriggering playback")
                        self._retriggered = True
                        self._retrigger()
                    if self._silent_run >= self.SILENCE_NO_AUDIO:
                        return True, "音声が検出できないため中止"
            else:
                self._heard_audio = True
                self._silent_run = 0

        # ── フォールバック5: 総尺超過ウォッチドッグ ───────────────────
        if elapsed is not None and total > 0 and elapsed > total * 1.5 + 120:
            return True, "録音時間が総尺を大幅超過（watchdog）"

        # ── 全シグナル不能の警告（keep-active モード等）───────────────
        if pct < 0 and ended is None and cur < 0:
            self._blind += 1
            if self._blind >= self.BLIND_WARN_POLLS and not self._blind_warned:
                self._blind_warned = True
                print("\n  警告: 動画の進捗・終了を検出できません（SCORM 構成が非対応）。"
                      "終了検出は無音判定などに依存します。")
                logger.warning("playback signals unavailable (blind mode)")
        else:
            self._blind = 0

        return False, ""


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
    transcriber: "Transcriber",
    args: argparse.Namespace,
    device_index: int | None,
    device_name: str,
    current: dict,
    stop_event: threading.Event,
) -> dict:
    """1 本処理して結果 dict（url/title/output/qa など）を返す。run-summary.json の 1 エントリ。"""
    browser = args.keep_active

    print(f"\n{_BOLD(f'[{idx+1}/{total}]')} {_CYN(url)}")

    # 動画（URL）ごとに重複除去カーソルをリセットする。
    # しないと 2 本目以降の冒頭が前の動画との「重複」と誤判定され丸ごと欠落する。
    transcriber.reset()

    navigate_to_url(url, browser)
    print("  ページ読み込み中...")
    target, url_pattern, actual_url = _resolve_tab_target(url, browser)

    page_title, page_url = get_moodle_page_info(target, browser)
    logger.debug("page info resolved title=%r", page_title)

    fmt = OutputFormat(args.format)
    now = datetime.now()
    out_dir = Path(args.output) if args.output else Path("out") / now.strftime("%Y%m%d")
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
                          title=page_title, page_url=page_url or url,
                          backend=getattr(transcriber, "backend_desc", None))
    capture = create_capture(device_index, args.segment_duration, args.overlap)
    keep_alive = WindowKeepAlive(
        browser, args.keep_interval,
        target=target,
        save_interval=args.save_interval,
    )
    current["keep_alive"] = keep_alive
    current["capture"] = capture

    print(f"\n{_BGRN('▶  録音開始')}  {_DIM('Ctrl+C で全停止')}\n")
    capture.start()
    logger.debug("capture started")
    keep_alive.start()
    _trigger_with_retry(target, browser)

    monitor = PlaybackMonitor(target, browser,
                              retrigger=lambda: _trigger_with_retry(target, browser))
    texts: list[str] = []
    stop_reason = ""
    elapsed = 0.0
    started_at = time.monotonic()
    try:
        for segment in capture.segments():
            if stop_event.is_set():
                break
            silent = is_silent(segment.data)
            if args.verbose:
                print(f"[セグメント {segment.segment_id}] {segment.start_time:.1f}秒〜"
                      + (" (無音スキップ)" if silent else ""))
            if not silent:
                results = transcriber.transcribe(segment.data, time_offset=segment.start_time)
                for r in results:
                    prefix = f"{_DIM(f'[{_fmt_ts(r.start)}]')} " if args.timestamps else ""
                    print(f"{prefix}{r.text}")
                    writer.append(r)
                    texts.append(r.text)
            elapsed = segment.start_time + len(segment.data) / 16000.0
            should_stop, reason = monitor.poll(silent=silent, elapsed=elapsed)
            if should_stop:
                stop_reason = reason
                print(f"\n{_BGRN('視聴完了')} ({reason}) — 保存中...")
                logger.info("stopping: %s", reason)
                # 先に keep-alive を止める（join 込み）。止めずに保存→離脱すると
                # 残った tick が play() を注入して動画を再開させることがある。
                keep_alive.mark_ended()
                keep_alive.stop()
                if monitor.played:
                    click_save_button(target, browser)
                    time.sleep(1)
                    click_exit_activity_button(target, browser)
                else:
                    logger.warning("video never played — skipping save/exit clicks")
                capture.stop()
                break
    finally:
        keep_alive.stop()
        capture.stop()   # 冪等。例外経路でもストリームを確実に閉じる
        writer.finalize()

    interrupted = stop_event.is_set()
    if not interrupted:
        output_path = _prompt_rename(output_path)
    qa = qa_metrics(texts, monitor.total, monitor.played, stop_reason)
    if interrupted:
        qa["status"] = "interrupted"
        qa["warnings"].append("Ctrl+C で中断")
    entry = {
        "index": idx,
        "url": url,
        "page_url": page_url or url,
        "title": page_title,
        "output": str(output_path),
        "stop_reason": stop_reason,
        "played": monitor.played,
        "elapsed_seconds": round(time.monotonic() - started_at, 1),
        "qa": qa,
    }
    logger.info("qa: %s", {k: v for k, v in qa.items() if k != "warnings"})
    for w in qa["warnings"]:
        logger.warning("qa: %s", w)
    _title = _BGRN(page_title) if page_title else _DIM(url)
    print(f"\n  {_BGRN('✔')}  {_title}  {_BGRN('done!')}")
    print(f"  {_DIM('URL')}   {_DIM(page_url or url)}")
    print(f"  {_DIM('保存')}  {_CYN(str(output_path))}")
    print(format_qa_line(entry))
    if idx + 1 < total:
        print(f"  {_DIM(f'[{idx+2}/{total}] 次の URL へ...')}")
    current["keep_alive"] = None
    current["capture"] = None
    return entry


def _keep_active_one_url(
    url: str,
    idx: int,
    total: int,
    args: argparse.Namespace,
    stop_event: threading.Event,
    current: dict,
) -> None:
    """文字起こしなしで Chrome タブの自動再生・視聴完了監視・離脱まで行う軽量版。"""
    browser = args.keep_active

    print(f"\n{_BOLD(f'[{idx+1}/{total}]')} {_CYN(url)}")

    navigate_to_url(url, browser)
    print("  ページ読み込み中...")
    target, url_pattern, actual_url = _resolve_tab_target(url, browser)

    page_title, page_url = get_moodle_page_info(target, browser)
    info_rows: list[tuple[str, str]] = []
    if page_title:
        info_rows.append(("ページ", _GRN(page_title)))
    info_rows.append(("URL", _CYN(page_url or url)))
    _print_session_info(info_rows)

    keep_alive = WindowKeepAlive(
        browser, args.keep_interval,
        target=target,
        save_interval=args.save_interval,
    )
    current["keep_alive"] = keep_alive

    print(f"\n{_BGRN('▶  再生開始')}  {_DIM('Ctrl+C で全停止')}\n")
    keep_alive.start()
    _trigger_with_retry(target, browser)

    poll_interval = max(5.0, args.segment_duration / 2)
    monitor = PlaybackMonitor(target, browser)
    start_t = time.monotonic()
    try:
        while not stop_event.is_set():
            if stop_event.wait(poll_interval):
                break
            should_stop, reason = monitor.poll(elapsed=time.monotonic() - start_t)
            if should_stop:
                print(f"\n{_BGRN('視聴完了')} ({reason}) — 保存中...")
                logger.info("keep-active stopping: %s", reason)
                keep_alive.mark_ended()
                keep_alive.stop()
                if monitor.played:
                    click_save_button(target, browser)
                    time.sleep(1)
                    click_exit_activity_button(target, browser)
                else:
                    logger.warning("video never played — skipping save/exit clicks")
                break
    finally:
        keep_alive.stop()

    _title = _BGRN(page_title) if page_title else _DIM(url)
    print(f"\n  {_BGRN('✔')}  {_title}  {_BGRN('done!')}")
    if idx + 1 < total:
        print(f"  {_DIM(f'[{idx+2}/{total}] 次の URL へ...')}")
    current["keep_alive"] = None


def _run_keep_active_batch(urls: list[str], args: argparse.Namespace) -> int:
    print(f"\n{_BOLD(f'{len(urls)} 件のURLを順次再生します')} {_DIM('(文字起こしなし)')}")

    stop_event = threading.Event()
    current: dict = {"keep_alive": None}

    def handle_sigint(sig, frame):
        if not stop_event.is_set():
            stop_event.set()
            print("\n停止中...")
            if current["keep_alive"]:
                current["keep_alive"].stop()

    signal.signal(signal.SIGINT, handle_sigint)

    for i, url in enumerate(urls):
        if stop_event.is_set():
            break
        _keep_active_one_url(url, i, len(urls), args, stop_event, current)
        if not stop_event.is_set() and i < len(urls) - 1:
            print(f"\n  次の URL まで {_DIM('5秒待機...')}")
            time.sleep(5)

    if not stop_event.is_set():
        print(f"\n{_BGRN('全URLの再生が完了しました')} ({len(urls)}件)")
    return 0



PAGE_VIDEO_WAIT = 30.0    # video 要素の出現を待つ秒数
PAGE_POLL_EXTRA = 5.0     # 音声チャンクが来ないとき状態を見に行く間隔 = segment_duration + これ


class _SessionProbe:
    """PlaybackMonitor 用: BrowserSession から pct / ended / time を読む。"""

    def __init__(self, session: "BrowserSession"):
        self._s = session

    def pct(self) -> int:
        try:
            return self._s.viewing_percentage()
        except Exception:  # noqa: BLE001
            return -1

    def ended(self) -> bool | None:
        try:
            st = self._s.state()
        except Exception:  # noqa: BLE001
            return None
        return st.ended if st.found else None

    def time(self) -> tuple[float, float]:
        try:
            st = self._s.state()
        except Exception:  # noqa: BLE001
            return -1.0, -1.0
        if not st.found:
            return -1.0, -1.0
        return st.current_time, st.duration


def _page_play_with_retry(session: "BrowserSession", attempts: int = 5, settle: float = 0.5) -> bool:
    """video.play() を呼び、実際に paused=false になったことを確認する。"""
    for i in range(attempts):
        res = session.play()
        logger.debug("page play() -> %r (attempt %d)", res, i + 1)
        for _ in range(6):
            time.sleep(settle)
            st = session.state()
            if st.playing:
                logger.info("video play triggered (attempt %d)", i + 1)
                return True
        logger.warning("trigger_video_play: not started (attempt %d/%d)", i + 1, attempts)
        time.sleep(2.0)
    print("  警告: 動画の再生を開始できませんでした。")
    logger.warning("trigger_video_play: gave up after %d attempts", attempts)
    return False


def _process_one_url_page(
    url: str,
    idx: int,
    total: int,
    transcriber: "Transcriber",
    args: argparse.Namespace,
    session: "BrowserSession",
    stop_event: threading.Event,
) -> dict:
    """page エンジン: 専用 Chrome で 1 本処理して結果 dict を返す。

    BlackHole 版 `_process_one_url` と同じ entry 形式。違いは
    - 遷移・再生・状態取得・保存/離脱クリックが BrowserSession（Playwright）
    - 音声が PageAudioCapture（ページ内 Web Audio → binding）
    - ログイン画面に落ちたら本人のログインを待つ
    """
    from page_capture import PageAudioCapture

    print(f"\n{_BOLD(f'[{idx+1}/{total}]')} {_CYN(url)}")
    transcriber.reset()
    started_at = time.monotonic()

    def _entry(page_title, page_url, output_path, texts, stop_reason, played, video_seconds, extra_warn=None):
        qa = qa_metrics(texts, video_seconds, played, stop_reason)
        if stop_event.is_set():
            qa["status"] = "interrupted"
            qa["warnings"].append("Ctrl+C で中断")
        if extra_warn:
            qa["warnings"].append(extra_warn)
        entry = {
            "index": idx, "url": url, "page_url": page_url or url, "title": page_title,
            "output": str(output_path) if output_path else None, "stop_reason": stop_reason,
            "played": played, "elapsed_seconds": round(time.monotonic() - started_at, 1), "qa": qa,
        }
        logger.info("qa: %s", {k: v for k, v in qa.items() if k != "warnings"})
        for w in qa["warnings"]:
            logger.warning("qa: %s", w)
        return entry

    # ── 遷移（ログイン待ちを含む） ──
    session.set_chunk_sink(None)
    session.goto(url)
    print("  ページ読み込み中...")

    def _on_login(login_url: str) -> None:
        print(_YLW("  Moodle がログイン画面です。専用ブラウザのウィンドウでログインしてください"
                   f"（{int(args.login_timeout // 60)} 分待ちます）"))
        logger.info("login required: %s", login_url)

    actual_url = session.wait_for_player(contains=getattr(args, "player_marker", "player.php"),
                                         timeout=60.0, login_timeout=args.login_timeout,
                                         on_login_required=_on_login, should_abort=stop_event.is_set)
    if stop_event.is_set():
        return _entry(None, session.url(), None, [], "", False, -1.0)
    if actual_url is None:
        logger.warning("page: player not reached for %s (url=%s)", url, session.url())
        print(_YLW("  プレーヤーに到達できませんでした（ログイン待ちタイムアウト or 想定外のページ）"))
        return _entry(None, session.url(), None, [], "プレーヤー到達失敗", False, -1.0)
    logger.info("target resolved (page engine): %s", actual_url)

    # video 要素の出現を待つ（SCORM iframe の読み込み）
    deadline = time.monotonic() + PAGE_VIDEO_WAIT
    while not session.has_video() and time.monotonic() < deadline:
        session.pump(500)
    raw_title = session.title() or ""
    page_title = (raw_title.split("|")[0].strip() or None) if raw_title else None
    page_url = actual_url
    logger.debug("page info resolved title=%r", page_title)

    fmt = OutputFormat(args.format)
    now = datetime.now()
    out_dir = Path(args.output) if args.output else Path("out") / now.strftime("%Y%m%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = _sanitize_filename(page_title) if page_title else f"lecture_{now.strftime('%H%M%S')}"
    output_path = _unique_path(out_dir / f"{filename}.{fmt.value}")

    info_rows: list[tuple[str, str]] = []
    if page_title:
        info_rows.append(("ページ", _GRN(page_title)))
    info_rows.append(("エンジン", _CYN("page（専用 Chrome / ページ内キャプチャ）")))
    info_rows.append(("出力", _CYN(str(output_path))))
    _print_session_info(info_rows)

    writer = OutputWriter(output_path, fmt, args.model, args.language,
                          title=page_title, page_url=page_url,
                          backend=getattr(transcriber, "backend_desc", None))
    capture = PageAudioCapture(args.segment_duration, args.overlap)
    capture.start()
    session.set_chunk_sink(capture.feed_b64)
    cap_res = session.start_capture()
    logger.info("page capture: %s", cap_res)
    if cap_res == "no_video":
        writer.finalize()
        print(_YLW("  動画要素が見つかりませんでした"))
        return _entry(page_title, page_url, output_path, [], "動画要素なし", False, -1.0)
    m_sr = re.search(r"sr=(\d+)", cap_res or "")
    if m_sr and int(m_sr.group(1)) != 16000:
        # AudioContext が 16kHz を拒否して既定レートで動いている。Python 側は 16kHz 前提なので
        # 速度違いの音声になり文字起こしが壊れる。偽成功にせず失敗で止める。
        writer.finalize()
        logger.warning("page capture sample rate mismatch: %s", cap_res)
        print(_YLW(f"  キャプチャのサンプルレートが 16kHz ではありません ({cap_res})"))
        return _entry(page_title, page_url, output_path, [], f"キャプチャのサンプルレート不一致 ({cap_res})", False, -1.0)

    print(f"\n{_BGRN('▶  文字起こし開始')}  {_DIM('Ctrl+C で全停止')}\n")
    _page_play_with_retry(session)

    probe = _SessionProbe(session)
    monitor = PlaybackMonitor(url, "page", retrigger=lambda: _page_play_with_retry(session), probe=probe)
    texts: list[str] = []
    stop_reason = ""
    last_save = time.monotonic()
    loop_started = time.monotonic()   # elapsed の基準（ログイン待ち時間を watchdog に含めない）
    poll_timeout = getattr(args, "page_poll_timeout", None) or max(5.0, args.segment_duration + PAGE_POLL_EXTRA)
    stopping = False
    try:
        while True:
            if stop_event.is_set() and not stopping:
                stopping = True
                capture.stop()
            segment = capture.next_segment(timeout=poll_timeout)
            if segment is None:
                if stopping or stop_event.is_set():
                    break
                # 音声チャンクが届かない（ページが止まった等）: 無音セグメント相当として判定だけ進める
                logger.debug("page: no audio chunk for %.0fs — polling playback state", poll_timeout)
                elapsed = time.monotonic() - loop_started
                should_stop, reason = monitor.poll(silent=True, elapsed=elapsed)
                if should_stop:
                    stop_reason = reason
                    print(f"\n{_BGRN('視聴完了')} ({reason}) — 保存中...")
                    logger.info("stopping: %s", reason)
                    stopping = True
                    try:
                        session.stop_capture()
                    except Exception as e:  # noqa: BLE001
                        logger.debug("stop_capture: %s", e)
                    capture.stop()
                continue
            silent = is_silent(segment.data)
            if args.verbose:
                print(f"[セグメント {segment.segment_id}] {segment.start_time:.1f}秒〜"
                      + (" (無音スキップ)" if silent else ""))
            if not silent:
                results = transcriber.transcribe(segment.data, time_offset=segment.start_time)
                for r in results:
                    prefix = f"{_DIM(f'[{_fmt_ts(r.start)}]')} " if args.timestamps else ""
                    print(f"{prefix}{r.text}")
                    writer.append(r)
                    texts.append(r.text)
            if stopping:
                continue   # 停止決定後は残りセグメントを流し切るだけ
            elapsed = segment.start_time + len(segment.data) / 16000.0
            should_stop, reason = monitor.poll(silent=silent, elapsed=elapsed)
            if args.save_interval > 0 and time.monotonic() - last_save >= args.save_interval:
                session.click_save()
                last_save = time.monotonic()
            if should_stop:
                stop_reason = reason
                print(f"\n{_BGRN('視聴完了')} ({reason}) — 保存中...")
                logger.info("stopping: %s", reason)
                stopping = True
                try:
                    session.stop_capture()
                except Exception as e:  # noqa: BLE001
                    logger.debug("stop_capture: %s", e)
                capture.stop()   # 残りバッファを最終セグメントとして流し、終端を入れる
    finally:
        session.set_chunk_sink(None)
        capture.stop()
        writer.finalize()

    if stop_reason and monitor.played and not stop_event.is_set():
        session.click_save()
        time.sleep(1)
        session.click_exit()
    elif stop_reason and not monitor.played:
        logger.warning("video never played — skipping save/exit clicks")

    if not stop_event.is_set():
        output_path = _prompt_rename(output_path)
    entry = _entry(page_title, page_url, output_path, texts, stop_reason, monitor.played, monitor.total)
    entry["audio_seconds_received"] = round(capture.seconds_received, 1)
    if monitor.played and monitor.total > 0 and capture.seconds_received < monitor.total * 0.5:
        entry["qa"]["warnings"].append(
            f"届いた音声が動画長の半分未満: {capture.seconds_received:.0f}s / {monitor.total:.0f}s")
        if entry["qa"]["status"] == "ok":
            entry["qa"]["status"] = "warn"
    _title = _BGRN(page_title) if page_title else _DIM(url)
    print(f"\n  {_BGRN('✔')}  {_title}  {_BGRN('done!')}")
    print(f"  {_DIM('URL')}   {_DIM(page_url)}")
    print(f"  {_DIM('保存')}  {_CYN(str(output_path))}")
    print(format_qa_line(entry))
    if idx + 1 < total:
        print(f"  {_DIM(f'[{idx+2}/{total}] 次の URL へ...')}")
    return entry


COURSE_PAGE_TEXT_JS = (
    "(() => { const m = document.querySelector('#region-main') || document.querySelector('main') || document.body;"
    " return (document.title || '') + String.fromCharCode(10) + (m ? m.innerText : ''); })()"
)


def _dump_course_page(session: "BrowserSession", out_dir: Path) -> Path | None:
    """最後の『活動から抜ける』でコースページに居れば本文を course-page.txt に保存する。

    スキルが課題の有無を判定する材料。専用ブラウザは実行後に閉じるので、ここで取っておく。
    """
    try:
        url = session.url()
        if "course/view.php" not in url:
            logger.info("course page not reached (url=%s); skip dump", url)
            return None
        text = session.evaluate(COURSE_PAGE_TEXT_JS) or ""
        path = out_dir / "course-page.txt"
        path.write_text(f"# URL: {url}\n{text}", encoding="utf-8")
        logger.info("course page dumped: %s", path)
        return path
    except Exception as e:  # noqa: BLE001
        logger.warning("course page dump failed: %s", e)
        return None


def _run_batch(
    urls: list[str],
    args: argparse.Namespace,
    device_index: int | None = None,
    device_name: str = "",
    routing_changed: bool = False,
    session: "BrowserSession | None" = None,
) -> int:
    """複数 URL を順に処理する。session があれば page エンジン、無ければ BlackHole エンジン。"""
    print(f"\n{_BOLD(f'{len(urls)} 件の講義を順次処理します')}")
    print("  Whisperモデルを読み込み中...")

    from transcriber import Transcriber as _Transcriber
    transcriber = _Transcriber(
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

    entries: list[dict] = []
    # 出力ディレクトリは URL 処理と同じ規則で先に決める（1 本目が失敗して output=None でも書ける）
    out_dir = Path(args.output) if getattr(args, "output", None) else Path("out") / datetime.now().strftime("%Y%m%d")
    summary_path = Path(args.json_summary) if getattr(args, "json_summary", None) else out_dir / "run-summary.json"

    def _flush_summary(finished: bool) -> None:
        if not entries:
            return
        try:
            write_run_summary(summary_path, entries, finished)
        except OSError as e:
            logger.warning("run-summary の書き出しに失敗: %s", e)

    completed = False   # 全 URL を回り切ったときだけ True（例外・中断では False のまま）
    try:
        for i, url in enumerate(urls):
            if stop_event.is_set():
                break
            if session is not None:
                entry = _process_one_url_page(url, i, len(urls), transcriber, args, session, stop_event)
            else:
                entry = _process_one_url(url, i, len(urls), transcriber, args,
                                         device_index, device_name, current, stop_event)
            if entry is not None:
                entries.append(entry)
                _flush_summary(finished=False)   # クラッシュ・中断に備えて URL ごとに書く
            if not stop_event.is_set() and i < len(urls) - 1:
                print(f"\n  次の URL まで {_DIM('5秒待機...')}")
                time.sleep(5)
        completed = not stop_event.is_set()
        if session is not None and entries:
            out_dir.mkdir(parents=True, exist_ok=True)
            _dump_course_page(session, out_dir)
    finally:
        if hasattr(transcriber, "close"):
            transcriber.close()
        if routing_changed:
            restore_audio_output(restore_to=args.restore_to)
        _flush_summary(finished=completed)

    failed = [e for e in entries if e["qa"]["status"] in ("failed", "interrupted")]
    warned = [e for e in entries if e["qa"]["status"] == "warn"]
    if not stop_event.is_set():
        if SYSTEM == "Darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{len(urls)}件の講義を文字起こし完了" with title "moodle-transcriber"'],
                capture_output=True,
            )
        print(f"\n{_BGRN('全講義の処理が完了しました')} ({len(urls)}件)")
    if entries:
        print(f"\n{_BOLD('結果サマリー')}  ok {len(entries) - len(failed) - len(warned)} / "
              f"warn {len(warned)} / failed {len(failed)}")
        for e in entries:
            print(format_qa_line(e))
        print(f"  {_DIM('サマリー')}  {_CYN(str(summary_path))}")
    if len(entries) < len(urls):
        missing = len(urls) - len(entries)
        print(_YLW(f"  未処理の URL が {missing} 件あります"))
        logger.warning("batch: %d of %d urls not processed", missing, len(urls))

    # 1 本でも failed（再生できず・空・幻覚のみ・中断）があれば rc=2。後段が再実行を判断する
    return 2 if failed or len(entries) < len(urls) else 0


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

    # --no-transcribe: 文字起こしなしの自動再生のみ
    if args.no_transcribe:
        targets = urls if urls else ([args.moodle_url] if args.moodle_url else [])
        if not targets:
            print("エラー: --no-transcribe には --moodle-url / --urls / --url-file のいずれかが必要です",
                  file=sys.stderr)
            return 1
        if not args.keep_active:
            args.keep_active = "chrome"
        return _run_keep_active_batch(targets, args)

    # ── page エンジン（既定）: 専用 Chrome + ページ内キャプチャ。ユーザーの Chrome には触らない ──
    if args.engine == "page":
        if not urls and args.moodle_url and args.moodle_url.startswith("http"):
            urls = [args.moodle_url]
        if not urls:
            print("エラー: page エンジンは --urls / --url-file（フル URL）が必要です。"
                  "従来方式を使う場合は --engine blackhole を指定してください", file=sys.stderr)
            return 1
        from browser import BrowserSession, DEFAULT_PROFILE_DIR
        profile = Path(args.profile_dir).expanduser() if args.profile_dir else DEFAULT_PROFILE_DIR
        print(f"  専用 Chrome を起動中... {_DIM(str(profile))}")
        session = BrowserSession(profile_dir=profile, headless=args.headless)
        try:
            session.start()
        except RuntimeError as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1
        try:
            return _run_batch(urls, args, session=session)
        finally:
            session.close()

    # 音声ルーティング自動切替（macOS + SwitchAudioSource がある場合）
    routing_changed = False
    if not args.no_auto_routing:
        routing_changed = setup_audio_for_capture()
    for app in warn_other_audio_apps():
        print(_YLW(f"  警告: {app} が再生中です。録音は全システム音声を拾うため文字起こしに混入します。一時停止してください"))

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

    # ─── 以下、シングル URL モード(既存の動作) ─────────────────────────────

    # ユーザーが開いている Moodle タブをパターンで探し、見つかればタブ id で追跡する
    # （アクティブタブ依存だと、実行中のタブ切り替えで別ページを誤って掴む）。
    url_pattern = args.moodle_url or ""
    target: TabTarget = url_pattern
    if args.moodle_url and args.keep_active:
        tab_id = get_tab_id_by_pattern(args.moodle_url, args.keep_active)
        if tab_id is not None:
            actual_url = get_tab_url(tab_id, args.keep_active) or args.moodle_url
        else:
            actual_url = get_active_tab_url(args.keep_active) or args.moodle_url
        url_pattern = _url_to_pattern(actual_url)
        target = tab_id if tab_id is not None else url_pattern
        logger.info("single target resolved: tab_id=%s pattern=%r (actual_url: %r)",
                    tab_id, url_pattern, actual_url)

    # ページ情報取得(Chrome タブの DOM から読む、サーバーリクエストなし)
    page_title, page_url = None, None
    if args.moodle_url and args.keep_active:
        page_title, page_url = get_moodle_page_info(target, args.keep_active)

    fmt = OutputFormat(args.format)
    now = datetime.now()
    out_dir = Path(args.output) if args.output else Path("out") / now.strftime("%Y%m%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = _sanitize_filename(page_title) if page_title else f"lecture_{now.strftime('%H%M%S')}"
    output_path = _unique_path(out_dir / f"{filename}.{fmt.value}")

    # セッション情報を整形表示
    info_rows: list[tuple[str, str]] = []
    if page_title:
        info_rows.append(("ページ", _GRN(page_title)))
    info_rows.append(("デバイス", _CYN(f"[{device_index}] {device_name}")))
    info_rows.append(("出力", _CYN(str(output_path))))
    info_rows.append(("モデル", _BLU(f"{args.model} | {args.language}")))
    _print_session_info(info_rows)

    print(_YLW("  音量 0 でも BlackHole 録音は継続します"))
    print("  Whisperモデルを読み込み中...")

    from transcriber import Transcriber as _Transcriber
    transcriber = _Transcriber(
        model_size=args.model,
        language=args.language,
        cpu_threads=args.cpu_threads,
    )
    writer = OutputWriter(output_path, fmt, args.model, args.language,
                          title=page_title, page_url=page_url,
                          backend=getattr(transcriber, "backend_desc", None))
    capture = create_capture(device_index, args.segment_duration, args.overlap)

    keep_alive: WindowKeepAlive | None = None
    if args.keep_active:
        keep_alive = WindowKeepAlive(
            args.keep_active, args.keep_interval,
            target=target,
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

    print(f"\n{_BGRN('▶  録音開始')}  {_DIM('Ctrl+C で停止')}\n")
    capture.start()
    if keep_alive:
        keep_alive.start()
    if args.moodle_url and args.keep_active:
        _trigger_with_retry(target, args.keep_active)

    monitor: PlaybackMonitor | None = None
    if args.moodle_url and args.keep_active:
        monitor = PlaybackMonitor(target, args.keep_active,
                                  retrigger=lambda: _trigger_with_retry(target, args.keep_active))
    try:
        for segment in capture.segments():
            silent = is_silent(segment.data)
            if args.verbose:
                print(f"[セグメント {segment.segment_id}] {segment.start_time:.1f}秒〜"
                      + (" (無音スキップ)" if silent else ""))
            if not silent:
                results = transcriber.transcribe(segment.data, time_offset=segment.start_time)
                for r in results:
                    prefix = f"{_DIM(f'[{_fmt_ts(r.start)}]')} " if args.timestamps else ""
                    print(f"{prefix}{r.text}")
                    writer.append(r)
            if monitor is not None:
                elapsed = segment.start_time + len(segment.data) / 16000.0
                should_stop, reason = monitor.poll(silent=silent, elapsed=elapsed)
                if should_stop:
                    print(f"\n{_BGRN('視聴完了')} ({reason}) — 保存中...")
                    logger.info("single url stopping: %s", reason)
                    if keep_alive:
                        keep_alive.mark_ended()
                        keep_alive.stop()
                    if monitor.played:
                        click_save_button(target, args.keep_active)
                        time.sleep(1)
                        click_exit_activity_button(target, args.keep_active)
                    else:
                        logger.warning("video never played — skipping save/exit clicks")
                    capture.stop()
                    break
    finally:
        if keep_alive:
            keep_alive.stop()
        capture.stop()   # 冪等。例外経路でもストリームを確実に閉じる
        writer.finalize()
        if hasattr(transcriber, "close"):
            transcriber.close()
        if routing_changed:
            restore_audio_output(restore_to=args.restore_to)
        if not shutdown_requested:
            output_path = _prompt_rename(output_path)
        _title = _BGRN(page_title) if page_title else ""
        print(f"\n  {_BGRN('✔')}  {(_title + '  ') if _title else ''}{_BGRN('done!')}")
        if page_url:
            print(f"  {_DIM('URL')}   {_DIM(page_url)}")
        print(f"  {_DIM('保存')}  {_CYN(str(output_path))}")

    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
