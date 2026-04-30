"""プラットフォーム固有の操作: 音声ルーティング自動切替, Moodleウィンドウキープアライブ"""

import platform
import shutil
import subprocess
import threading

SYSTEM = platform.system()

# ─── macOS 音声ルーティング ───────────────────────────────────────────────────

_original_output: str | None = None

_VISIBILITY_JS = (
    "try{"
    "Object.defineProperty(document,'visibilityState',{get:()=>'visible',configurable:true});"
    "Object.defineProperty(document,'hidden',{get:()=>false,configurable:true});"
    "document.dispatchEvent(new Event('visibilitychange'));"
    "}catch(e){}"
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
    ):
        self._browser = browser.lower()
        self._interval = interval
        self._url_pattern = url_pattern
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        pat = f" (URL: {self._url_pattern})" if self._url_pattern else ""
        print(f"ウィンドウキープアライブ: {self._browser}{pat} ({self._interval}秒ごと)")

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
