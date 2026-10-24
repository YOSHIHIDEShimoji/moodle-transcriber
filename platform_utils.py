"""プラットフォーム固有の操作: 音声ルーティング自動切替, Moodleウィンドウキープアライブ"""

import logging
import platform
import shutil
import subprocess
import threading
import time

_logger = logging.getLogger("moodle-transcriber")

SYSTEM = platform.system()

# タブの指定方法: int なら Chrome/Arc のタブ id、str なら URL 部分一致パターン。
# タブ id の方がユーザーのタブ操作に影響されないため優先する。
TabTarget = int | str

# ─── macOS 音声ルーティング ───────────────────────────────────────────────────

_original_output: str | None = None

# 注意: ここに埋め込む JS は AppleScript の "..." 内に展開されるため、
# ダブルクォートとバックスラッシュを使ってはならない。
# （AppleScript は未知のエスケープ \d 等を構文エラーにする。過去に
#  正規表現の \d が原因で get_viewing_percentage が常時失敗していた）

_VISIBILITY_JS = (
    # 各同一オリジンフレーム（深さ4まで）に対し:
    #  1) visibilityState/hidden を visible/false に上書き
    #  2) addEventListener('visibilitychange'|...) を一度だけ無効化
    #  3) video が見つかれば、終了済みでない場合のみ再生を維持する
    #     - v.ended を一度でも観測したらフレームの window に __mtEnded を立て、
    #       以後は play() も visibilitychange の dispatch もしない
    #       （SCORM プレーヤーが ended をリセットして巻き戻しても再生し直さない）
    #     - currentTime が 0 付近の動画は再生しない（最初からの再ループ防止）
    "(function(){"
    "function shield(d){"
    "try{Object.defineProperty(d,'visibilityState',{get:function(){return 'visible';},configurable:true});}catch(e){}"
    "try{Object.defineProperty(d,'hidden',{get:function(){return false;},configurable:true});}catch(e){}"
    "try{if(!d.__mtBlocked){d.__mtBlocked=true;"
    "var oa=d.addEventListener;"
    "d.addEventListener=function(t,l,o){"
    "if(t==='visibilitychange'||t==='webkitvisibilitychange'||t==='freeze'||t==='blur')return;"
    "return oa.call(this,t,l,o);};}}catch(e){}"
    "}"
    "function walk(w,d){if(d>4)return;"
    "try{shield(w.document);}catch(e){}"
    "try{for(var i=0;i<w.frames.length;i++){walk(w.frames[i],d+1);}}catch(e){}"
    "}"
    "function findv(w,d){if(d>4)return null;"
    "try{var v=w.document.querySelector('video');if(v)return v;}catch(e){}"
    "try{for(var i=0;i<w.frames.length;i++){var r=findv(w.frames[i],d+1);if(r)return r;}}catch(e){}"
    "return null;}"
    "walk(window,0);"
    "var v=findv(window,0);"
    "if(v){var W=v.ownerDocument.defaultView||window;"
    "if(v.ended){W.__mtEnded=true;}"
    "if(!W.__mtEnded){"
    "try{v.ownerDocument.dispatchEvent(new Event('visibilitychange'));}catch(e){}"
    "try{if(v.paused&&!v.ended&&v.currentTime>0.5){v.play().catch(function(){});}}catch(e){}"
    "}}"
    "})();"
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
    # 1) Moodle player.php の outer page に「視聴状況 XX%」テキストがあればそれを返す
    # 2) なければ SCORM iframe を再帰探索して video.currentTime/duration から算出
    #    （正規表現に \d を使うと AppleScript が構文エラーになるため [0-9] を使う）
    "(function(){"
    "var b=document.body.innerText;"
    "var i=b.indexOf('視聴状況');"
    "if(i>=0){var s=b.substring(i,i+20);var n=s.match(/([0-9]+)%/);if(n)return n[1];}"
    "function _f(w,d){if(d>4)return null;"
    "try{var v=w.document.querySelector('video');if(v&&v.duration>0)return v;}catch(e){}"
    "try{for(var j=0;j<w.frames.length;j++){var r=_f(w.frames[j],d+1);if(r)return r;}}catch(e){}"
    "return null;}"
    "var v=_f(window,0);"
    "if(v)return Math.round(v.currentTime/v.duration*100);"
    "return '-1';"
    "})()"
)

_GET_PLAY_BTN_POS_JS = (
    # SCORM iframe 内の <video> を再生させるには、クリック先が iframe の内側で
    # ある必要がある（Chrome の autoplay policy: ユーザーアクティベーションは
    # フレーム単位で伝播するため）。最大面積の iframe の中心座標を返すことで、
    # 確実に iframe 内にクリックを当てる。iframe が無いページでは再生ボタン or
    # ウィンドウ中心にフォールバックする。
    "(function(){"
    "var cH=window.outerHeight-window.innerHeight;"
    "var ifs=document.querySelectorAll('iframe');"
    "var best=null,bestArea=0;"
    "for(var i=0;i<ifs.length;i++){"
    "try{var r=ifs[i].getBoundingClientRect();"
    "var a=r.width*r.height;"
    "if(a>bestArea&&r.width>100&&r.height>100){bestArea=a;best=r;}}"
    "catch(e){}}"
    "if(best){"
    "return Math.round(window.screenX+best.left+best.width/2)+','+"
    "Math.round(window.screenY+cH+best.top+best.height/2);}"
    "var sels=['.vjs-big-play-button','.vjs-play-control','.big-play-button','video'];"
    "for(var s=0;s<sels.length;s++){"
    "try{var b=document.querySelector(sels[s]);"
    "if(b){var br=b.getBoundingClientRect();"
    "return Math.round(window.screenX+br.left+br.width/2)+','+"
    "Math.round(window.screenY+cH+br.top+br.height/2);}}"
    "catch(e){}}"
    "return Math.round(window.screenX+window.innerWidth/2)+','+"
    "Math.round(window.screenY+cH+window.innerHeight/2);"
    "})()"
)

_PLAY_VIDEO_JS = (
    # iframe を再帰探索（深さ上限 4）して video.play() を呼ぶ
    "(function(){"
    "function _f(w,d){if(d>4)return null;"
    "try{var v=w.document.querySelector('video');if(v)return v;}catch(e){}"
    "try{for(var i=0;i<w.frames.length;i++){var r=_f(w.frames[i],d+1);if(r)return r;}}catch(e){}"
    "return null;}"
    "var v=_f(window,0);"
    "if(v){try{v.play();return 'playing';}catch(e){}}"
    "return 'no_video';"
    "})()"
)

_GET_VIDEO_TIME_JS = (
    # iframe を再帰探索（深さ上限 4）して video 要素を取得する
    "(function(){"
    "function _f(w,d){if(d>4)return null;"
    "try{var v=w.document.querySelector('video');if(v)return v;}catch(e){}"
    "try{for(var i=0;i<w.frames.length;i++){var r=_f(w.frames[i],d+1);if(r)return r;}}catch(e){}"
    "return null;}"
    "var v=_f(window,0);"
    "if(v)return v.currentTime+'|'+v.duration;"
    "return '-1|-1';"
    "})()"
)

_VIDEO_ENDED_JS = (
    # iframe を再帰探索（深さ上限 4）して video.ended を返す
    "(function(){"
    "function _f(w,d){if(d>4)return null;"
    "try{var v=w.document.querySelector('video');if(v)return v;}catch(e){}"
    "try{for(var i=0;i<w.frames.length;i++){var r=_f(w.frames[i],d+1);if(r)return r;}}catch(e){}"
    "return null;}"
    "var v=_f(window,0);"
    "if(v)return v.ended?'true':'false';"
    "return 'unknown';"
    "})()"
)


# ─── 他アプリの音声再生チェック ────────────────────────────────────────────

_AUDIO_APPS = ("Spotify", "Music")


def warn_other_audio_apps() -> list[str]:
    """再生中の音楽アプリ名を返す（macOS 以外・未起動なら空）。

    録音は Multi-Output Device 経由の全システム音声なので、Spotify 等の音は
    そのまま文字起こしに混入する（2026-10-16 の実機テストで英語の歌詞が混入）。
    """
    if SYSTEM != "Darwin":
        return []
    playing: list[str] = []
    for app in _AUDIO_APPS:
        r = _osascript(
            f'if application "{app}" is running then\n'
            f'  tell application "{app}" to return (player state as string)\n'
            f'else\n  return "not running"\nend if',
            timeout=5,
        )
        if r is not None and r.returncode == 0 and r.stdout.strip() == "playing":
            playing.append(app)
    return playing


# ─── osascript 実行の共通ラッパー ─────────────────────────────────────────────

def _osascript(script: str, timeout: float = 10.0) -> subprocess.CompletedProcess | None:
    """osascript を実行する。タイムアウト・OSエラーは None を返す（例外を上げない）。

    Chrome が応答しない等で TimeoutExpired が発生しても、呼び出し側の
    録音ループを殺さないことが最優先。
    """
    try:
        return subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _logger.warning("osascript timeout (%.0fs): %s...", timeout, script[:80])
        return None
    except OSError as e:
        _logger.warning("osascript failed: %s", e)
        return None


def _find_multi_output_device() -> str | None:
    """SwitchAudioSource -a でデバイス一覧を取得し、Multi-Output Device 名を返す。"""
    try:
        r = subprocess.run(
            ["SwitchAudioSource", "-a", "-t", "output"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
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
    try:
        _original_output = subprocess.run(
            ["SwitchAudioSource", "-c"], capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        _original_output = None

    target = _find_multi_output_device()
    if not target:
        print("警告: Multi-Output Device が見つかりません（Audio MIDI Setup で作成済みか確認してください）")
        return False

    try:
        r = subprocess.run(["SwitchAudioSource", "-s", target], capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        r = None
    if r is not None and r.returncode == 0:
        print(f"音声出力 → '{target}' に切り替えました（元: {_original_output}）")
        return True
    print(f"警告: '{target}' への切り替え失敗")
    return False


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

    try:
        subprocess.run(["SwitchAudioSource", "-s", target], capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return
    print(f"音声出力 → '{target}' に復元しました")


# ─── ブラウザタブ操作 ─────────────────────────────────────────────────────────

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


def _tab_condition(target: TabTarget) -> str:
    """タブ検索条件の AppleScript 行を返す。int=タブid完全一致 / str=URL部分一致。

    タブ id は必ず文字列として比較する。Chrome (155 で実測) の `id of tab` は
    text で返るため、`id of t is 2144314444` のような整数リテラル比較は
    一度も一致しない（さらに 2^29 超の整数リテラルは AppleScript 内で real になる）。
    この 1 行が数値比較だった間、JS 注入・タイトル取得・保存/離脱クリックが
    すべて無言失敗し「ブラインドモード」になっていた（docs/fix-plan-tab-id.md）。
    """
    if isinstance(target, int):
        return f'      if (id of t as string) is "{target}" then'
    return f'      if URL of t contains "{target}" then'


# タブ未発見を JS の空結果と区別するための番兵。
_NO_TAB_SENTINEL = "__NO_TAB__"
_no_tab_warned: set[TabTarget] = set()


def _warn_no_tab(target: TabTarget) -> None:
    """対象タブが見つからなかったことを target ごとに 1 回だけ警告する。"""
    if target in _no_tab_warned:
        return
    _no_tab_warned.add(target)
    _logger.warning("tab not found: %r (JS 注入・終了検出が機能しません)", target)


def _run_js_in_tab(js: str, target: TabTarget, browser: str = "chrome") -> str:
    """指定タブ（id または URL パターン）で JS を実行して結果文字列を返す。失敗時は ""。"""
    if SYSTEM != "Darwin":
        return ""
    app = _MACOS_APPS.get(browser.lower(), browser)
    lines = [
        f'tell application "{app}"',
        "  repeat with w in windows",
        "    repeat with t in tabs of w",
        _tab_condition(target),
        f'        return execute t javascript "{js}"',
        "      end if",
        "    end repeat",
        "  end repeat",
        f'  return "{_NO_TAB_SENTINEL}"',
        "end tell",
    ]
    result = _osascript("\n".join(lines), timeout=10)
    if result is None or result.returncode != 0:
        return ""
    out = result.stdout.strip()
    if out == _NO_TAB_SENTINEL:
        _warn_no_tab(target)
        return ""
    return out


def navigate_to_url(url: str, browser: str = "chrome") -> None:
    """Chrome のアクティブタブを指定 URL に遷移させる。

    後続の System Events click が Chrome に届くよう、URL 変更前に activate する。
    """
    if SYSTEM != "Darwin":
        return
    app = _MACOS_APPS.get(browser.lower(), browser)
    script = (
        f'tell application "{app}"\n'
        f'  activate\n'
        f'  set URL of active tab of front window to "{url}"\n'
        f'end tell'
    )
    _osascript(script, timeout=10)


def get_active_tab_url(browser: str = "chrome") -> str | None:
    """フロントウィンドウのアクティブタブの URL を返す。"""
    if SYSTEM != "Darwin":
        return None
    app = _MACOS_APPS.get(browser.lower(), browser)
    script = f'tell application "{app}" to return URL of active tab of front window'
    result = _osascript(script, timeout=10)
    if result is not None and result.returncode == 0:
        return result.stdout.strip() or None
    return None


def get_active_tab_id(browser: str = "chrome") -> int | None:
    """フロントウィンドウのアクティブタブの id を返す（Chrome/Arc）。

    navigate_to_url 直後に呼べば「今遷移させたタブ」を掴める。以後の操作は
    タブ id で行うことで、ユーザーがタブを切り替えても誤爆しない。
    """
    if SYSTEM != "Darwin":
        return None
    app = _MACOS_APPS.get(browser.lower(), browser)
    script = f'tell application "{app}" to return id of active tab of front window'
    result = _osascript(script, timeout=10)
    if result is None or result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def get_tab_id_by_pattern(url_pattern: str, browser: str = "chrome") -> int | None:
    """URL パターンに一致する最初のタブの id を返す（全ウィンドウ検索）。"""
    if SYSTEM != "Darwin":
        return None
    app = _MACOS_APPS.get(browser.lower(), browser)
    lines = [
        f'tell application "{app}"',
        "  repeat with w in windows",
        "    repeat with t in tabs of w",
        f'      if URL of t contains "{url_pattern}" then',
        "        return id of t",
        "      end if",
        "    end repeat",
        "  end repeat",
        "end tell",
    ]
    result = _osascript("\n".join(lines), timeout=10)
    if result is None or result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def get_tab_url(target: TabTarget | None, browser: str = "chrome") -> str | None:
    """指定タブの URL を返す。target=None ならアクティブタブ。"""
    if SYSTEM != "Darwin":
        return None
    if target is None:
        return get_active_tab_url(browser)
    app = _MACOS_APPS.get(browser.lower(), browser)
    lines = [
        f'tell application "{app}"',
        "  repeat with w in windows",
        "    repeat with t in tabs of w",
        _tab_condition(target),
        "        return URL of t",
        "      end if",
        "    end repeat",
        "  end repeat",
        "end tell",
    ]
    result = _osascript("\n".join(lines), timeout=10)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def wait_for_page_ready(
    target: TabTarget | None,
    browser: str = "chrome",
    contains: str = "player.php",
    timeout: float = 25.0,
    min_wait: float = 5.0,
    settle: float = 2.0,
) -> str | None:
    """navigate 後、リダイレクト完了を待って実 URL を返す。

    固定 sleep の代わりにタブ URL をポーリングする:
    - URL に contains（SCORM プレーヤー）が現れたら settle 秒待って確定
    - 現れなくても min_wait 経過後に URL が 3 秒間安定していれば確定
      （view.php → player.php リダイレクトが完了する前に掴まないための min_wait）
    """
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    last: str | None = None
    stable = 0
    while time.monotonic() < deadline:
        url = get_tab_url(target, browser)
        if url and contains in url:
            time.sleep(settle)  # SCORM iframe の読み込み待ち
            return get_tab_url(target, browser) or url
        if url is not None and url == last:
            stable += 1
            if stable >= 6 and time.monotonic() - start >= min_wait:
                return url
        else:
            stable = 0
            last = url
        time.sleep(0.5)
    return last


def get_moodle_page_info(target: TabTarget, browser: str = "chrome") -> tuple[str | None, str | None]:
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
        _tab_condition(target),
        "        set pageUrl to URL of t",
        "        set pageTitle to title of t",
        '        return pageUrl & "\\n" & pageTitle',
        "      end if",
        "    end repeat",
        "  end repeat",
        "end tell",
    ]
    result = _osascript("\n".join(lines), timeout=10)
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return None, None
    parts = result.stdout.strip().split("\n", 1)
    page_url = parts[0] if parts else None
    raw_title = parts[1].strip() if len(parts) > 1 else None
    if not raw_title or raw_title == "missing value":
        return None, page_url
    # "講義動画１ | 2026-@M1-IPE001 | ChibaUnivMoodle" → "講義動画１"
    title = raw_title.split("|")[0].strip()
    return (title or None), page_url


_VIDEO_PLAYING_JS = (
    # iframe を再帰探索して video の再生状態を返す: 'playing' / 'paused' / 'no_video'
    "(function(){"
    "function _f(w,d){if(d>4)return null;"
    "try{var v=w.document.querySelector('video');if(v)return v;}catch(e){}"
    "try{for(var i=0;i<w.frames.length;i++){var r=_f(w.frames[i],d+1);if(r)return r;}}catch(e){}"
    "return null;}"
    "var v=_f(window,0);"
    "if(!v)return 'no_video';"
    "return (v.paused||v.ended)?'paused':'playing';"
    "})()"
)


def get_video_playing(target: TabTarget, browser: str = "chrome") -> bool | None:
    """video が実際に再生中か。True/False、video に到達できなければ None。"""
    raw = _run_js_in_tab(_VIDEO_PLAYING_JS, target, browser)
    if raw == "playing":
        return True
    if raw == "paused":
        return False
    return None


# CoreGraphics でマウスクリックを投稿する JXA。
# System Events の `click at {x, y}` は macOS 26 (Darwin 25) で座標を無視し、
# 常に同じ固定点に着弾することを実測した（2026-10-16・docs/fix-plan-tab-id.md）。
# CGEventPost は指定座標に正確に届き、Chrome は trusted なユーザー操作として
# 扱う（autoplay policy の解除に必要）。追加依存なし（osascript -l JavaScript）。
_CG_CLICK_JXA = """ObjC.import('CoreGraphics');
var p = $.CGPointMake(%d, %d);
var mv = $.CGEventCreateMouseEvent(null, $.kCGEventMouseMoved, p, $.kCGMouseButtonLeft);
var dn = $.CGEventCreateMouseEvent(null, $.kCGEventLeftMouseDown, p, $.kCGMouseButtonLeft);
var up = $.CGEventCreateMouseEvent(null, $.kCGEventLeftMouseUp, p, $.kCGMouseButtonLeft);
$.CGEventPost($.kCGHIDEventTap, mv); delay(0.05);
$.CGEventPost($.kCGHIDEventTap, dn); delay(0.05);
$.CGEventPost($.kCGHIDEventTap, up);
'ok';"""


def _cg_click(x: int, y: int) -> bool:
    """画面座標 (x, y) に CoreGraphics の左クリックを投稿する。成功時 True。"""
    try:
        r = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", _CG_CLICK_JXA % (x, y)],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        _logger.warning("cg click failed: %s", e)
        return False
    if r.returncode != 0 or "ok" not in r.stdout:
        _logger.warning("cg click failed rc=%s: %s", r.returncode, r.stderr.strip()[:120])
        return False
    return True


def _wait_frontmost(app: str, timeout: float = 2.0) -> bool:
    """app が最前面になるまで待つ（activate 直後のクリック取りこぼし防止）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = _osascript(
            'tell application "System Events" to return name of first application process whose frontmost is true',
            timeout=5,
        )
        if r is not None and r.returncode == 0 and r.stdout.strip() == app:
            return True
        time.sleep(0.1)
    return False


def _wait_playing(target: TabTarget, browser: str, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if get_video_playing(target, browser) is True:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)


def trigger_video_play(target: TabTarget, browser: str = "chrome") -> bool:
    """
    Moodle タブの動画再生を開始する。**実際に再生が始まったことを確認して** True。
    macOS: CoreGraphics で実クリック → video.play() → paused=false を実測。
    非macOS: v.play() を直接呼ぶ（フォールバック）。

    以前は「クリック API が 0 を返した」だけで True を返しており、
    クリックが動画に当たらず autoplay policy で play() が拒否されても
    成功扱いになっていた（録音だけ走って空ファイルができる）。
    """
    if SYSTEM != "Darwin":
        raw = _run_js_in_tab(_PLAY_VIDEO_JS, target, browser)
        return raw == "playing"

    app = _MACOS_APPS.get(browser.lower(), browser)
    raw = _run_js_in_tab(_GET_PLAY_BTN_POS_JS, target, browser)
    _logger.debug("trigger_video_play raw=%r target=%r", raw, target)
    if not raw or "," not in raw:
        return False
    try:
        x, y = (int(float(v)) for v in raw.strip().split(","))
        # Step1: 最前面にしてから OS レベルクリック（ユーザージェスチャー登録）
        _osascript(f'tell application "{app}" to activate', timeout=10)
        if not _wait_frontmost(app):
            _logger.debug("trigger_video_play: %s not frontmost after activate", app)
        if not _cg_click(x, y):
            # フォールバック: 旧来の System Events クリック
            r = _osascript(f'tell application "System Events" to click at {{{x}, {y}}}', timeout=10)
            if r is None or r.returncode != 0:
                return False
        time.sleep(0.5)
        if _wait_playing(target, browser, timeout=1.0):
            return True
        # Step2: クリックで再生が始まらなければ play() を呼ぶ
        res = _run_js_in_tab(_PLAY_VIDEO_JS, target, browser)
        _logger.debug("trigger_video_play play()=%r", res)
        ok = _wait_playing(target, browser, timeout=2.0)
        if not ok:
            _logger.warning("trigger_video_play: click+play() did not start playback (state=%s)",
                            get_video_playing(target, browser))
        return ok
    except Exception as e:
        _logger.debug("trigger_video_play exception: %s", e)
        return False


def get_viewing_percentage(target: TabTarget, browser: str = "chrome") -> int:
    """視聴状況 XX% を取得する。取得失敗時は -1 を返す。"""
    raw = _run_js_in_tab(_GET_PCT_JS, target, browser)
    try:
        return int(raw)
    except (ValueError, TypeError):
        return -1


def get_video_time(target: TabTarget, browser: str = "chrome") -> tuple[float, float]:
    """動画の (現在位置秒, 総尺秒) を返す。取得失敗時は (-1, -1)。"""
    raw = _run_js_in_tab(_GET_VIDEO_TIME_JS, target, browser)
    try:
        cur, total = raw.split("|")
        return float(cur), float(total)
    except (ValueError, AttributeError):
        return -1.0, -1.0


def click_save_button(target: TabTarget, browser: str = "chrome") -> bool:
    """「視聴状況を保存」ボタン (#save > input[type=button]) をクリックする。成功時 True。"""
    return _run_js_in_tab(_SAVE_BTN_JS, target, browser) == "clicked"


def click_exit_activity_button(target: TabTarget, browser: str = "chrome") -> bool:
    """「活動から抜ける」リンクをクリックする。成功時 True。"""
    return _run_js_in_tab(_EXIT_ACTIVITY_JS, target, browser) == "clicked"


def get_video_ended(target: TabTarget, browser: str = "chrome") -> bool | None:
    """動画の ended プロパティを返す。

    True: 終了を確認 / False: 未終了を確認 / None: 判定不能
    （video 要素に到達できない・JS 実行失敗）。
    「判定不能」を False と区別することで、呼び出し側がフォールバック
    終了判定（currentTime 停滞・無音継続など）に切り替えられる。
    """
    raw = _run_js_in_tab(_VIDEO_ENDED_JS, target, browser)
    if raw == "true":
        return True
    if raw == "false":
        return False
    return None


class WindowKeepAlive:
    """
    Moodleタブをアクティブに保つバックグラウンドスレッド。
    macOS Chrome: visibilityState を JS で上書きして一時停止を防ぐ（サーバーログなし）。
    target 指定時: タブ id / URL パターンで対象タブに注入（アクティブタブ不問）。

    動画終了を一度でも観測したら以後の注入を止める（再ループ防止）。
    JS 側にも __mtEnded フラグがあり、ended 判定が Python から取れない
    構成でも二重にガードされる。
    """

    def __init__(
        self,
        browser: str = "chrome",
        interval: float = 20.0,
        target: TabTarget | None = None,
        save_interval: float = 60.0,
        url_pattern: str | None = None,  # 旧引数名（互換用）
    ):
        self._browser = browser.lower()
        self._interval = interval
        resolved: TabTarget | None = target if target is not None else url_pattern
        if isinstance(resolved, str) and not resolved:
            resolved = None  # 空パターンは「全タブ一致」ではなくアクティブタブ注入にする
        self._target = resolved
        self._save_interval = save_interval
        self._last_save: float = 0.0
        self._ended = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        _logger.debug("keep-alive started target=%r", self._target)
        pat = f" (タブ: {self._target})" if self._target is not None else ""
        save_info = f", 保存:{self._save_interval:.0f}秒毎" if self._save_interval > 0 else ""
        print(f"ウィンドウキープアライブ: {self._browser}{pat} ({self._interval}秒ごと{save_info})")

    def stop(self) -> None:
        """停止して tick の完了を待つ。

        join しないと、メイン側が「保存 → 活動から抜ける」を実行した直後に
        残った tick が play() を注入して動画を再開させる競合が起きる。
        """
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=15)

    def mark_ended(self) -> None:
        """動画終了をメイン側から通知する（以後 play/visibility 注入をしない）。"""
        self._ended = True

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
        if self._stop.is_set():
            return
        if SYSTEM == "Darwin":
            self._tick_macos()
        elif SYSTEM == "Linux":
            self._tick_linux()
        elif SYSTEM == "Windows":
            self._tick_windows()

    def _tick_macos(self) -> None:
        # 動画が終了済みなら注入をスキップ（再生ループ防止）。
        # get_video_ended は「判定不能 (None)」を返すことがあるが、その場合は
        # 注入を続ける（タブ切替で止まるのを防ぐのが本来の役目のため）。
        # None のケースの再ループは JS 側の __mtEnded ガードと、メインループの
        # フォールバック終了判定 → stop() が防ぐ。
        if self._ended:
            return
        if self._target is not None:
            if get_video_ended(self._target, self._browser) is True:
                self._ended = True
                return
        if self._stop.is_set():
            return

        app = _MACOS_APPS.get(self._browser, self._browser)
        if self._browser in ("chrome", "arc"):
            if self._target is not None:
                lines = [
                    f'tell application "{app}"',
                    "  repeat with w in windows",
                    "    repeat with t in tabs of w",
                    _tab_condition(self._target),
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
        _osascript(script, timeout=10)

        if self._target is not None and self._save_interval > 0 and not self._stop.is_set():
            now = time.monotonic()
            if now - self._last_save >= self._save_interval:
                click_save_button(self._target, self._browser)
                self._last_save = now

    def _tick_linux(self) -> None:
        if shutil.which("xdotool"):
            cmd = _LINUX_CMDS.get(self._browser, self._browser)
            try:
                subprocess.run(
                    ["xdotool", "search", "--name", cmd, "windowactivate"],
                    capture_output=True, timeout=10,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass

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
