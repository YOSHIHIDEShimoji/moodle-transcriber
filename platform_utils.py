"""プラットフォーム固有の操作: 音声ルーティング自動切替, Moodleウィンドウキープアライブ"""

import logging
import platform
import shutil
import subprocess
import threading
import time

_logger = logging.getLogger("moodle-transcriber")

SYSTEM = platform.system()

# ─── macOS 音声ルーティング ───────────────────────────────────────────────────

_original_output: str | None = None

_VISIBILITY_JS = (
    "(function(d){"
    "try{Object.defineProperty(d,'visibilityState',{get:()=>'visible',configurable:true});}catch(e){}"
    "try{Object.defineProperty(d,'hidden',{get:()=>false,configurable:true});}catch(e){}"
    "try{d.dispatchEvent(new Event('visibilitychange'));}catch(e){}"
    "})(document);"
    "try{for(var i=0;i<frames.length;i++){"
    "try{(function(d){"
    "Object.defineProperty(d,'visibilityState',{get:()=>'visible',configurable:true});"
    "Object.defineProperty(d,'hidden',{get:()=>false,configurable:true});"
    "d.dispatchEvent(new Event('visibilitychange'));"
    "})(frames[i].document);}catch(e){}"
    "}}catch(e){}"
)

_SAVE_BTN_JS = (
    "(function(){"
    "var btn=document.querySelector('#save > input[type=button]');"
    "if(btn){btn.click();return 'clicked';}"
    "return 'not_found';"
    "})()"
)

_EXIT_ACTIVITY_JS = (
    "(function(){"
    "var a=[...document.querySelectorAll('#region-main a')]"
    ".find(function(el){return el.textContent.trim().includes('活動から抜ける');});"
    "if(a){a.click();return 'clicked';}"
    "return 'not_found';"
    "})()"
)

_GET_PCT_JS = (
    "(function(){"
    "var b=document.body.innerText;"
    "var i=b.indexOf('視聴状況');"
    "if(i<0)return '-1';"
    "var s=b.substring(i,i+20);"
    "var n=s.match(/(\\d+)%/);"
    "return n?n[1]:'-1';"
    "})()"
)

_GET_PLAY_BTN_POS_JS = (
    "(function(){"
    "var cH=window.outerHeight-window.innerHeight;"
    "var sels=['.vjs-big-play-button','.vjs-play-control','.big-play-button','video'];"
    "function findPos(doc,ox,oy){"
    "for(var s=0;s<sels.length;s++){"
    "try{var b=doc.querySelector(sels[s]);"
    "if(b){var r=b.getBoundingClientRect();return [ox+r.left+r.width/2,oy+r.top+r.height/2];}}"
    "catch(e){}}"
    "return null;}"
    "var p=findPos(document,0,0);"
    "if(p)return Math.round(window.screenX+p[0])+','+Math.round(window.screenY+cH+p[1]);"
    "var ifs=document.querySelectorAll('iframe');"
    "for(var i=0;i<ifs.length;i++){"
    "try{"
    "var ir=ifs[i].getBoundingClientRect();"
    "var fd=ifs[i].contentDocument||ifs[i].contentWindow.document;"
    "p=findPos(fd,ir.left,ir.top);"
    "if(p)return Math.round(window.screenX+p[0])+','+Math.round(window.screenY+cH+p[1]);"
    "if(ir.width>0&&ir.height>0)return Math.round(window.screenX+ir.left+ir.width/2)+','+Math.round(window.screenY+cH+ir.top+ir.height/2);"
    "}catch(e){}}"
    "return Math.round(window.screenX+window.innerWidth/2)+','+Math.round(window.screenY+cH+window.innerHeight/2);"
    "})()"
)

_GET_VIDEO_TIME_JS = (
    "(function(){"
    "var v=document.querySelector('video');"
    "if(!v){for(var i=0;i<frames.length;i++){try{v=frames[i].document.querySelector('video');if(v)break;}catch(e){}}}"
    "if(v){return v.currentTime+'|'+v.duration;}"
    "return '-1|-1';"
    "})()"
)

_VIDEO_ENDED_JS = (
    "(function(){"
    "var v=document.querySelector('video');"
    "if(!v){for(var i=0;i<frames.length;i++){try{v=frames[i].document.querySelector('video');if(v)break;}catch(e){}}}"
    "if(v){return v.ended?'true':'false';}"
    "return 'unknown';"
    "})()"
)


def _find_multi_output_device() -> str | None:
    """SwitchAudioSource -a でデバイス一覧を取得し、Multi-Output Device 名を返す。"""
    r = subprocess.run(["SwitchAudioSource", "-a", "-t", "output"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        name = line.strip()
        if "Multi-Output" in name or "複数出力" in name:
            return name
    return None


def setup_audio_for_capture() -> bool:
    """
    録音開始時にシステム音声出力を Multi-Output Device に自動切替する。
    スピーカー音量はキーボードで自由に調整可能（0にしてもBlackHoleへの信号は維持される）。
    戻り値: 切り替え成功したか
    """
    if SYSTEM != "Darwin":
        return False
    if not shutil.which("SwitchAudioSource"):
        print("ヒント: brew install switchaudio-osx で自動音声切替が有効になります")
        return False

    global _original_output
    _original_output = subprocess.run(
        ["SwitchAudioSource", "-c"], capture_output=True, text=True
    ).stdout.strip()

    target = _find_multi_output_device()
    if not target:
        print("警告: Multi-Output Device が見つかりません（Audio MIDI Setup で作成済みか確認してください）")
        return False

    r = subprocess.run(["SwitchAudioSource", "-s", target], capture_output=True)
    if r.returncode == 0:
        print(f"音声出力 → '{target}' に切り替えました（元: {_original_output}）")
        return True
    print(f"警告: '{target}' への切り替え失敗")
    return False


def get_moodle_page_info(url_pattern: str, browser: str = "chrome") -> tuple[str | None, str | None]:
    """
    Chrome の既存タブから講義タイトルと URL を取得。
    AppleScript の `title of t` プロパティを使う → JS 実行・フレームコンテキスト問題なし。
    Moodle の title 形式 "講義名 | コース名 | サイト名" から先頭部分を抽出する。
    Returns: (title, page_url)
    """
    if SYSTEM != "Darwin":
        return None, None
    app = _MACOS_APPS.get(browser.lower(), browser)
    if browser.lower() not in ("chrome", "arc"):
        return None, None

    lines = [
        f'tell application "{app}"',
        "  repeat with w in windows",
        "    repeat with t in tabs of w",
        f'      if URL of t contains "{url_pattern}" then',
        "        set pageUrl to URL of t",
        "        set pageTitle to title of t",
        '        return pageUrl & "\\n" & pageTitle',
        "      end if",
        "    end repeat",
        "  end repeat",
        "end tell",
    ]
    result = subprocess.run(
        ["osascript", "-e", "\n".join(lines)],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None, None
    parts = result.stdout.strip().split("\n", 1)
    page_url = parts[0] if parts else None
    raw_title = parts[1].strip() if len(parts) > 1 else None
    if not raw_title or raw_title == "missing value":
        return None, page_url
    # "講義動画１ | 2026-@M1-IPE001 | ChibaUnivMoodle" → "講義動画１"
    title = raw_title.split("|")[0].strip()
    return (title or None), page_url


def restore_audio_output(restore_to: str | None = None) -> None:
    """
    録音終了時にシステム音声出力を復元する。
    restore_to が指定されていればそのデバイスへ。
    未指定かつ元デバイスが Multi-Output Device（音量キー非対応）の場合は
    スキップして警告を表示する。
    """
    if SYSTEM != "Darwin":
        return
    if not shutil.which("SwitchAudioSource"):
        return

    target = restore_to or _original_output
    if not target:
        return

    # Multi-Output Device 系に戻しても音量キーが使えないため警告
    is_multi = any(kw in target for kw in ("Multi-Output", "複数出力装置", "複数出力"))
    if is_multi and restore_to is None:
        print(
            f"注意: 元のデバイス '{target}' はMulti-Output Deviceのため音量キーが使えません。\n"
            f"      音量調整したい場合は --restore-to 'MacBook Proのスピーカー' を指定してください。"
        )

    subprocess.run(["SwitchAudioSource", "-s", target], capture_output=True)
    print(f"音声出力 → '{target}' に復元しました")


# ─── Moodle ウィンドウキープアライブ ─────────────────────────────────────────

_MACOS_APPS = {
    "safari": "Safari",
    "chrome": "Google Chrome",
    "firefox": "Firefox",
    "edge": "Microsoft Edge",
    "arc": "Arc",
}

_LINUX_CMDS = {
    "chrome": "google-chrome",
    "firefox": "firefox",
    "edge": "microsoft-edge",
}


def _run_js_in_tab(js: str, url_pattern: str, browser: str = "chrome") -> str:
    """指定 URL パターンのタブで JS を実行して結果文字列を返す。"""
    if SYSTEM != "Darwin":
        return ""
    app = _MACOS_APPS.get(browser.lower(), browser)
    lines = [
        f'tell application "{app}"',
        "  repeat with w in windows",
        "    repeat with t in tabs of w",
        f'      if URL of t contains "{url_pattern}" then',
        f'        return execute t javascript "{js}"',
        "      end if",
        "    end repeat",
        "  end repeat",
        "end tell",
    ]
    result = subprocess.run(
        ["osascript", "-e", "\n".join(lines)],
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout.strip()


def navigate_to_url(url: str, browser: str = "chrome") -> None:
    """Chrome のアクティブタブを指定 URL に遷移させる。"""
    if SYSTEM != "Darwin":
        return
    app = _MACOS_APPS.get(browser.lower(), browser)
    script = (
        f'tell application "{app}" to '
        f'set URL of active tab of front window to "{url}"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)


def get_active_tab_url(browser: str = "chrome") -> str | None:
    """フロントウィンドウのアクティブタブの URL を返す（リダイレクト後の実 URL）。"""
    if SYSTEM != "Darwin":
        return None
    app = _MACOS_APPS.get(browser.lower(), browser)
    script = f'tell application "{app}" to return URL of active tab of front window'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    if result.returncode == 0:
        return result.stdout.strip() or None
    return None


def trigger_video_play(url_pattern: str, browser: str = "chrome") -> bool:
    """
    Moodle タブの動画再生を開始する。成功時 True。
    macOS: AppleScript で実際のマウスクリックをシミュレートする（autoplay policy 回避）。
    非macOS: v.play() を直接呼ぶ（フォールバック）。
    """
    if SYSTEM != "Darwin":
        raw = _run_js_in_tab(
            "(function(){var v=document.querySelector('video');"
            "if(!v){for(var i=0;i<frames.length;i++){try{v=frames[i].document.querySelector('video');if(v)break;}catch(e){}}}"
            "if(v){v.play();return 'playing';}return 'no_video';})()",
            url_pattern, browser,
        )
        return raw == "playing"

    app = _MACOS_APPS.get(browser.lower(), browser)
    raw = _run_js_in_tab(_GET_PLAY_BTN_POS_JS, url_pattern, browser)
    _logger.debug("trigger_video_play raw=%r pattern=%r", raw, url_pattern)
    if not raw or "," not in raw:
        return False
    try:
        x, y = raw.strip().split(",")
        # Step1: OS レベルクリックでユーザージェスチャーを Chrome に登録する
        script = (
            f'tell application "{app}" to activate\n'
            f'delay 0.3\n'
            f'tell application "System Events" to click at {{{int(float(x))}, {int(float(y))}}}'
        )
        result = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
        if result.returncode != 0:
            return False
        # Step2: ユーザージェスチャー登録後に video.play() を JS で呼ぶ
        # クリック座標がボタンに当たらない場合でもこちらで再生を保証する
        time.sleep(0.5)
        _run_js_in_tab(
            "(function(){"
            "var v=document.querySelector('video');"
            "if(!v){for(var i=0;i<frames.length;i++){try{v=frames[i].document.querySelector('video');if(v)break;}catch(e){}}}"
            "if(v){try{v.play();}catch(e){}}"
            "})()",
            url_pattern, browser,
        )
        return True
    except Exception as e:
        _logger.debug("trigger_video_play exception: %s", e)
        return False


def get_viewing_percentage(url_pattern: str, browser: str = "chrome") -> int:
    """視聴状況 XX% を取得する。取得失敗時は -1 を返す。"""
    raw = _run_js_in_tab(_GET_PCT_JS, url_pattern, browser)
    try:
        return int(raw)
    except (ValueError, TypeError):
        return -1


def get_video_time(url_pattern: str, browser: str = "chrome") -> tuple[float, float]:
    """動画の (現在位置秒, 総尺秒) を返す。取得失敗時は (-1, -1)。"""
    raw = _run_js_in_tab(_GET_VIDEO_TIME_JS, url_pattern, browser)
    try:
        cur, total = raw.split("|")
        return float(cur), float(total)
    except (ValueError, AttributeError):
        return -1.0, -1.0


def click_save_button(url_pattern: str, browser: str = "chrome") -> bool:
    """「視聴状況を保存」ボタン (#save > input[type=button]) をクリックする。成功時 True。"""
    return _run_js_in_tab(_SAVE_BTN_JS, url_pattern, browser) == "clicked"


def click_exit_activity_button(url_pattern: str, browser: str = "chrome") -> bool:
    """「活動から抜ける」リンクをクリックする。成功時 True。"""
    return _run_js_in_tab(_EXIT_ACTIVITY_JS, url_pattern, browser) == "clicked"


def get_video_ended(url_pattern: str, browser: str = "chrome") -> bool:
    """動画の ended プロパティを返す。取得失敗時は False。"""
    raw = _run_js_in_tab(_VIDEO_ENDED_JS, url_pattern, browser)
    return raw == "true"


class WindowKeepAlive:
    """
    Moodleタブをアクティブに保つバックグラウンドスレッド。
    macOS Chrome: visibilityState を JS で上書きして一時停止を防ぐ（サーバーログなし）。
    url_pattern 指定時: 全タブをURL検索して対象タブに注入（アクティブタブ不問）。
    """

    def __init__(
        self,
        browser: str = "chrome",
        interval: float = 20.0,
        url_pattern: str | None = None,
        save_interval: float = 60.0,
    ):
        self._browser = browser.lower()
        self._interval = interval
        self._url_pattern = url_pattern
        self._save_interval = save_interval
        self._last_save: float = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        pat = f" (URL: {self._url_pattern})" if self._url_pattern else ""
        save_info = f", 保存:{self._save_interval:.0f}秒毎" if self._save_interval > 0 else ""
        print(f"ウィンドウキープアライブ: {self._browser}{pat} ({self._interval}秒ごと{save_info})")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        try:
            self._tick()          # 起動直後に即座に実行
        except Exception:
            pass
        while not self._stop.wait(self._interval):
            try:
                self._tick()
            except Exception:
                pass

    def _tick(self) -> None:
        if SYSTEM == "Darwin":
            self._tick_macos()
        elif SYSTEM == "Linux":
            self._tick_linux()
        elif SYSTEM == "Windows":
            self._tick_windows()

    def _tick_macos(self) -> None:
        app = _MACOS_APPS.get(self._browser, self._browser)
        if self._browser in ("chrome", "arc"):
            if self._url_pattern:
                # 全タブのURLを検索してMoodleタブに注入（アクティブタブ不問）
                lines = [
                    f'tell application "{app}"',
                    "  repeat with w in windows",
                    "    repeat with t in tabs of w",
                    f'      if URL of t contains "{self._url_pattern}" then',
                    f'        execute t javascript "{_VISIBILITY_JS}"',
                    "      end if",
                    "    end repeat",
                    "  end repeat",
                    "end tell",
                ]
                script = "\n".join(lines)
            else:
                script = (
                    f'tell application "{app}" to execute front window\'s '
                    f'active tab javascript "{_VISIBILITY_JS}"'
                )
        elif self._browser == "safari":
            script = (
                f'tell application "Safari" to do JavaScript '
                f'"{_VISIBILITY_JS}" in current tab of front window'
            )
        else:
            script = (
                f'tell application "System Events" to if exists process '
                f'"{app}" then set frontmost of process "{app}" to true'
            )
        subprocess.run(["osascript", "-e", script], capture_output=True)

        if self._url_pattern and self._save_interval > 0:
            now = time.monotonic()
            if now - self._last_save >= self._save_interval:
                click_save_button(self._url_pattern, self._browser)
                self._last_save = now

    def _tick_linux(self) -> None:
        if shutil.which("xdotool"):
            cmd = _LINUX_CMDS.get(self._browser, self._browser)
            subprocess.run(
                ["xdotool", "search", "--name", cmd, "windowactivate"],
                capture_output=True,
            )

    def _tick_windows(self) -> None:
        import ctypes
        cls_map = {
            "chrome": "Chrome_WidgetWin_1",
            "firefox": "MozillaWindowClass",
            "edge": "Chrome_WidgetWin_1",
        }
        cls = cls_map.get(self._browser)
        if cls:
            hwnd = ctypes.windll.user32.FindWindowW(cls, None)
            if hwnd:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
