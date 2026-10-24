"""専用ブラウザ制御層（Playwright / Chrome DevTools Protocol）。

ユーザーの Chrome には一切触らない。system の Google Chrome 本体を **別プロファイル**
（既定 ~/.moodle-transcriber/chrome-profile）で別プロセスとして起動し、そのウィンドウだけを操作する。

- `--autoplay-policy=no-user-gesture-required` で起動するため OS レベルのクリックが不要
  （AppleScript / System Events / CGEventPost を使わない）。
- 音声は BlackHole ではなく **ページ内** で取り出す: video 要素を Web Audio の
  MediaElementSource に繋ぎ、ScriptProcessor で PCM を取り出して Playwright の binding 経由で
  Python に渡す。destination には繋がないのでスピーカーからは鳴らない。
  他アプリ（Spotify 等）の音は物理的に混ざらない。
- iframe は Playwright の frame として直接評価するので、別オリジンの SCORM 教材にも届く。

Playwright の同期 API はスレッド間で共有できないため、ブラウザ操作は専用スレッドで行い、
他スレッドからはコマンドキュー経由で呼ぶ（`BrowserSession` の各メソッド）。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_logger = logging.getLogger("moodle-transcriber")

DEFAULT_PROFILE_DIR = Path.home() / ".moodle-transcriber" / "chrome-profile"

CHROME_ARGS = [
    "--autoplay-policy=no-user-gesture-required",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-features=MediaSessionService",
    "--no-first-run",
    "--no-default-browser-check",
]

# ─── ページ内 JS ─────────────────────────────────────────────────────────────
# Playwright 経由なのでダブルクォート・バックスラッシュの制約（AppleScript 用）は無い。

# video 要素を Web Audio に繋いで PCM を Python へ送る。
# - 16kHz でサンプリング（Whisper の入力レート）。
# - ステレオをそのまま送る（逆相ステレオは Python 側 downmix_to_mono で処理）。
# - destination には繋がない（無音）。ScriptProcessor は出力先が無いと止まる実装が
#   あるため、gain=0 を経由して destination に繋ぐ。
# %d には AudioContext のサンプルレートが入る。再生速度 r 倍のときは 16000*r にする:
# preservesPitch=false の r 倍速はただの早回し（波形を時間方向に 1/r に縮めたもの）なので、
# 16000*r Hz で取ったサンプルを 16000 Hz として扱えば元の速度・元のピッチに戻る。
CAPTURE_JS_TEMPLATE = r"""
(() => {
  const v = document.querySelector('video');
  if (!v) return 'no_video';
  if (window.__mtCap) return 'already';
  let ctx;
  try { ctx = new AudioContext({sampleRate: %d}); } catch (e) { ctx = new AudioContext(); }
  const src = ctx.createMediaElementSource(v);
  const proc = ctx.createScriptProcessor(4096, 2, 2);
  src.connect(proc);
  const mute = ctx.createGain(); mute.gain.value = 0;
  proc.connect(mute); mute.connect(ctx.destination);
  let n = 0;
  proc.onaudioprocess = (e) => {
    const L = e.inputBuffer.getChannelData(0);
    const R = e.inputBuffer.numberOfChannels > 1 ? e.inputBuffer.getChannelData(1) : L;
    const out = new Int16Array(L.length * 2);
    for (let i = 0; i < L.length; i++) {
      out[2 * i] = Math.max(-1, Math.min(1, L[i])) * 32767;
      out[2 * i + 1] = Math.max(-1, Math.min(1, R[i])) * 32767;
    }
    const bytes = new Uint8Array(out.buffer);
    let s = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    try { window.mt_chunk(btoa(s)); n++; } catch (e) {}
  };
  window.__mtCap = {ctx, proc, src, get count() { return n; }};
  ctx.resume();
  return 'capturing sr=' + ctx.sampleRate + ' state=' + ctx.state;
})()
"""
CAPTURE_JS = CAPTURE_JS_TEMPLATE % 16000

# 再生速度を設定する。r != 1 のとき preservesPitch=false（ピッチ補正なしの早回し）にする。
# プレーヤーが rate を 1 に戻すことがあるので、毎 poll で呼び直す。
SET_RATE_JS_TEMPLATE = r"""
(() => {
  const v = document.querySelector('video');
  if (!v) return -1;
  const r = %s;
  try { v.defaultPlaybackRate = r; } catch (e) {}
  try { v.playbackRate = r; } catch (e) {}
  try { v.preservesPitch = (r === 1); } catch (e) {}
  try { v.mozPreservesPitch = (r === 1); } catch (e) {}
  return v.playbackRate;
})()
"""

STOP_CAPTURE_JS = r"""
(() => {
  const c = window.__mtCap;
  if (!c) return 'none';
  try { c.proc.onaudioprocess = null; c.proc.disconnect(); } catch (e) {}
  try { c.ctx.close(); } catch (e) {}
  window.__mtCap = null;
  return 'stopped';
})()
"""

# visibilityState を常に visible に見せ、プレーヤーがタブ非表示で止まるのを防ぐ。
# 全フレームの init script として登録する（iframe の再読み込みにも効く）。
SHIELD_INIT_JS = r"""
(() => {
  try { Object.defineProperty(document, 'visibilityState', {get: () => 'visible', configurable: true}); } catch (e) {}
  try { Object.defineProperty(document, 'hidden', {get: () => false, configurable: true}); } catch (e) {}
  try {
    const oa = document.addEventListener.bind(document);
    document.addEventListener = function (t, l, o) {
      if (t === 'visibilitychange' || t === 'webkitvisibilitychange' || t === 'freeze') return;
      return oa(t, l, o);
    };
  } catch (e) {}
  try {
    const ow = window.addEventListener.bind(window);
    window.addEventListener = function (t, l, o) {
      if (t === 'blur' || t === 'pagehide') return;
      return ow(t, l, o);
    };
  } catch (e) {}
})()
"""

HAS_VIDEO_JS = "!!document.querySelector('video')"

VIDEO_STATE_JS = r"""
(() => {
  const v = document.querySelector('video');
  if (!v) return null;
  const c = window.__mtCap;
  return {
    paused: v.paused, ended: v.ended,
    currentTime: v.currentTime, duration: isFinite(v.duration) ? v.duration : -1,
    readyState: v.readyState, muted: v.muted, playbackRate: v.playbackRate,
    captureCount: c ? c.count : -1, ctxState: c ? c.ctx.state : '',
  };
})()
"""

PLAY_JS = r"""
(async () => {
  const v = document.querySelector('video');
  if (!v) return 'no_video';
  try { if (window.__mtCap) await window.__mtCap.ctx.resume(); } catch (e) {}
  try { await v.play(); return 'playing'; } catch (e) { return 'ERR ' + e.name + ': ' + e.message; }
})()
"""

# Moodle の player.php 外側ページ: 「視聴状況 XX%」
PCT_JS = r"""
(() => {
  const b = document.body ? document.body.innerText : '';
  const i = b.indexOf('視聴状況');
  if (i >= 0) { const m = b.substring(i, i + 20).match(/(\d+)%/); if (m) return parseInt(m[1], 10); }
  return -1;
})()
"""

SAVE_BTN_JS = r"""
(() => {
  const btn = document.querySelector('#save > input[type=button]');
  if (btn) { btn.click(); return 'clicked'; }
  return 'not_found';
})()
"""

EXIT_ACTIVITY_JS = r"""
(() => {
  const a = [...document.querySelectorAll('#region-main a')].find(el => el.textContent.trim().includes('活動から抜ける'));
  if (a) { a.click(); return 'clicked'; }
  return 'not_found';
})()
"""


@dataclass
class VideoState:
    found: bool
    paused: bool = True
    ended: bool = False
    current_time: float = -1.0
    duration: float = -1.0
    capture_count: int = -1
    ctx_state: str = ""
    playback_rate: float = 1.0

    @property
    def playing(self) -> bool:
        return self.found and not self.paused and not self.ended


@dataclass
class _Cmd:
    fn: Callable[[], Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class BrowserSession:
    """専用 Chrome を 1 プロセス起動し、専用スレッドから操作する。

    使い方:
        s = BrowserSession(); s.start()
        s.set_chunk_sink(capture.feed_b64)
        s.goto(url); s.wait_for_player(); s.start_capture(); s.play()
        ... s.state() を定期的に見る ...
        s.stop_capture(); s.click_save(); s.click_exit(); s.close()
    """

    def __init__(
        self,
        profile_dir: Path | str = DEFAULT_PROFILE_DIR,
        headless: bool = False,
        window: tuple[int, int, int, int] = (1200, 40, 900, 620),   # x, y, w, h
        channel: str = "chrome",
    ):
        self._profile_dir = Path(profile_dir).expanduser()
        self._headless = headless
        self._window = window
        self._channel = channel
        self._q: queue.Queue[_Cmd] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._start_error: BaseException | None = None
        self._sink: Callable[[str], None] | None = None
        self._sink_lock = threading.Lock()
        # Playwright オブジェクト（専用スレッドのみが触る）
        self._pw = None
        self._ctx = None
        self._page = None
        self._video_frame = None

    # ── ライフサイクル ───────────────────────────────────────────────────
    @property
    def headless(self) -> bool:
        return self._headless

    def start(self, timeout: float = 60.0) -> None:
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._start_error = None
        self._thread = threading.Thread(target=self._run, name="browser", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("専用ブラウザの起動がタイムアウトしました")
        if self._start_error is not None:
            raise RuntimeError(f"専用ブラウザの起動に失敗: {self._start_error}") from self._start_error

    def relaunch(self, headless: bool, timeout: float = 60.0) -> None:
        """同じプロファイルで headless を切り替えて起動し直す（Cookie は残る）。

        既定運用: 普段はヘッドレス、ログインが必要なときだけウィンドウ付きで起こし、
        ログイン後に再びヘッドレスへ戻す。chunk sink は維持する。
        """
        sink = self._sink
        self.close()
        self._headless = headless
        self.start(timeout=timeout)
        self.set_chunk_sink(sink)

    def close(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=30)
        self._thread = None

    def set_chunk_sink(self, sink: Callable[[str], None] | None) -> None:
        """ページから届く base64 PCM（int16 ステレオ 16kHz）の受け口を差し替える。"""
        with self._sink_lock:
            self._sink = sink

    # ── 専用スレッド本体 ─────────────────────────────────────────────────
    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            self._start_error = e
            self._ready.set()
            return
        try:
            self._profile_dir.mkdir(parents=True, exist_ok=True)
            x, y, w, h = self._window
            self._pw = sync_playwright().start()
            self._ctx = self._pw.chromium.launch_persistent_context(
                str(self._profile_dir),
                channel=self._channel,
                headless=self._headless,
                args=CHROME_ARGS + [f"--window-size={w},{h}", f"--window-position={x},{y}"],
                viewport={"width": w, "height": h - 90},
                ignore_default_args=["--enable-automation"],
            )
            self._ctx.add_init_script(SHIELD_INIT_JS)
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            self._page.expose_binding("mt_chunk", self._on_chunk)
            self._ready.set()
        except BaseException as e:  # noqa: BLE001
            self._start_error = e
            self._ready.set()
            self._teardown()
            return

        while not self._stop.is_set():
            try:
                cmd = self._q.get(timeout=0.05)
            except queue.Empty:
                # Playwright の呼び出し中にしか binding が配送されないため、定期的にポンプする
                try:
                    self._page.wait_for_timeout(100)
                except Exception:  # noqa: BLE001
                    pass
                continue
            try:
                cmd.result = cmd.fn()
            except BaseException as e:  # noqa: BLE001
                cmd.error = e
            finally:
                cmd.done.set()
        self._teardown()

    def _teardown(self) -> None:
        for closer in (lambda: self._ctx and self._ctx.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        self._ctx = None
        self._pw = None
        self._page = None

    def _on_chunk(self, source: dict, b64: str) -> None:
        with self._sink_lock:
            sink = self._sink
        if sink is not None:
            try:
                sink(b64)
            except Exception as e:  # noqa: BLE001
                _logger.debug("chunk sink error: %s", e)

    def _call(self, fn: Callable[[], Any], timeout: float = 60.0) -> Any:
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError("専用ブラウザが起動していません")
        cmd = _Cmd(fn)
        self._q.put(cmd)
        if not cmd.done.wait(timeout):
            raise TimeoutError("ブラウザ操作がタイムアウトしました")
        if cmd.error is not None:
            raise cmd.error
        return cmd.result

    # ── フレーム探索（専用スレッド内で呼ぶ） ─────────────────────────────
    def _find_video_frame(self):
        # 直前のフレームがまだ有効ならそれを使う
        f = self._video_frame
        if f is not None:
            try:
                if not f.is_detached() and f.evaluate(HAS_VIDEO_JS):
                    return f
            except Exception:  # noqa: BLE001
                pass
        self._video_frame = None
        for fr in self._page.frames:
            try:
                if fr.evaluate(HAS_VIDEO_JS):
                    self._video_frame = fr
                    return fr
            except Exception:  # noqa: BLE001
                continue
        return None

    # ── 公開 API（任意のスレッドから） ───────────────────────────────────
    def goto(self, url: str, timeout: float = 60.0) -> str:
        def _fn():
            self._video_frame = None
            self._page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            return self._page.url
        return self._call(_fn, timeout=timeout + 10)

    def url(self) -> str:
        return self._call(lambda: self._page.url, timeout=10)

    def title(self) -> str:
        return self._call(lambda: self._page.title(), timeout=10)

    def bring_to_front(self) -> None:
        self._call(lambda: self._page.bring_to_front(), timeout=10)

    def pump(self, ms: int = 200) -> None:
        """何もせず ms だけ待つ（binding の配送を進める）。"""
        self._call(lambda: self._page.wait_for_timeout(ms), timeout=ms / 1000 + 10)

    def has_video(self) -> bool:
        return self._call(lambda: self._find_video_frame() is not None, timeout=20)

    def start_capture(self, sample_rate: int = 16000) -> str:
        def _fn():
            f = self._find_video_frame()
            if f is None:
                return "no_video"
            return f.evaluate(CAPTURE_JS_TEMPLATE % int(sample_rate))
        return self._call(_fn, timeout=20)

    def set_playback_rate(self, rate: float) -> float:
        """video.playbackRate を設定し、実際の値を返す（video が無ければ -1）。"""
        def _fn():
            f = self._find_video_frame()
            if f is None:
                return -1.0
            return float(f.evaluate(SET_RATE_JS_TEMPLATE % repr(float(rate))))
        return self._call(_fn, timeout=20)

    def stop_capture(self) -> str:
        def _fn():
            f = self._find_video_frame()
            return f.evaluate(STOP_CAPTURE_JS) if f is not None else "none"
        return self._call(_fn, timeout=20)

    def play(self) -> str:
        def _fn():
            f = self._find_video_frame()
            return f.evaluate(PLAY_JS) if f is not None else "no_video"
        return self._call(_fn, timeout=20)

    def pause(self) -> None:
        def _fn():
            f = self._find_video_frame()
            if f is not None:
                f.evaluate("(() => { const v = document.querySelector('video'); if (v) v.pause(); })()")
        self._call(_fn, timeout=20)

    def state(self) -> VideoState:
        def _fn():
            f = self._find_video_frame()
            if f is None:
                return VideoState(found=False)
            d = f.evaluate(VIDEO_STATE_JS)
            if not d:
                return VideoState(found=False)
            return VideoState(
                found=True, paused=bool(d["paused"]), ended=bool(d["ended"]),
                current_time=float(d["currentTime"]), duration=float(d["duration"]),
                capture_count=int(d["captureCount"]), ctx_state=str(d["ctxState"]),
                playback_rate=float(d.get("playbackRate", 1.0) or 1.0),
            )
        return self._call(_fn, timeout=20)

    def viewing_percentage(self) -> int:
        """『視聴状況 XX%』をメインフレーム→他フレームの順で探す（SCORM 構成により位置が違う）。"""
        def _fn():
            for fr in [self._page.main_frame] + [f for f in self._page.frames if f is not self._page.main_frame]:
                try:
                    v = int(fr.evaluate(PCT_JS))
                except Exception:  # noqa: BLE001
                    continue
                if v >= 0:
                    return v
            return -1
        return self._call(_fn, timeout=20)

    def evaluate(self, js: str) -> Any:
        """メインフレームで JS を評価する（検証・デバッグ用）。"""
        return self._call(lambda: self._page.main_frame.evaluate(js), timeout=20)

    def click_save(self) -> bool:
        return self._call(lambda: self._page.main_frame.evaluate(SAVE_BTN_JS) == "clicked", timeout=20)

    def click_exit(self) -> bool:
        return self._call(lambda: self._page.main_frame.evaluate(EXIT_ACTIVITY_JS) == "clicked", timeout=20)

    # ── 高レベル: プレーヤー到達待ち（ログイン待ちを含む） ─────────────
    # パス区切り付きで判定する（"sso" ⊂ lesson、"auth" ⊂ authors の過剰一致を避ける）
    LOGIN_PATH_MARKERS = ("/login/", "/login.", "/auth/", "/saml", "/sso", "/idp/", "shibboleth", "/oauth")
    LOGIN_HOST_PREFIXES = ("idp.", "sso.", "auth.", "login.", "shib.")

    @classmethod
    def looks_like_login(cls, url: str) -> bool:
        """ログイン／SSO 系のページか。Moodle 自身の /login/ と外部 IdP（idp.*/sso.* や /saml,/sso パス）を拾う。"""
        from urllib.parse import urlparse
        try:
            pr = urlparse((url or "").lower())
        except ValueError:
            return False
        host = pr.netloc.split("@")[-1].split(":")[0]
        if any(host.startswith(pfx) for pfx in cls.LOGIN_HOST_PREFIXES):
            return True
        path = pr.path or "/"
        return any(m in path for m in cls.LOGIN_PATH_MARKERS)

    def wait_for_player(
        self,
        contains: str = "player.php",
        timeout: float = 60.0,
        login_timeout: float = 600.0,
        on_login_required: Callable[[str], None] | None = None,
        should_abort: Callable[[], bool] | None = None,
        poll_ms: int = 500,
    ) -> str | None:
        """URL に contains が現れるまで待つ。途中でログイン画面に落ちたら本人の操作を待つ。

        - ログイン画面を一度でも見たら、以後は（どのページに居ても）login_timeout で諦める。
          以前は「ログイン後に /my/ 等へ着地」で期限判定が消え永久ループしていた。
        - should_abort()（Ctrl+C の stop_event 等）が True になったら即 None。
        Returns: 到達した URL。タイムアウト／中断なら None。
        """
        start = time.monotonic()
        deadline = start + timeout
        login_deadline: float | None = None
        notified = False
        while True:
            if should_abort is not None and should_abort():
                return None
            url = self.url()
            if contains in url:
                return url
            if self.looks_like_login(url):
                if login_deadline is None:
                    login_deadline = time.monotonic() + login_timeout
                    try:
                        self.bring_to_front()
                    except Exception:  # noqa: BLE001
                        pass
                if not notified and on_login_required is not None:
                    notified = True
                    on_login_required(url)
            now = time.monotonic()
            if login_deadline is not None:
                if now > login_deadline:
                    return None
            elif now > deadline:
                return None
            self.pump(poll_ms)
