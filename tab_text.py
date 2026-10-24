"""Chrome の指定タブの本文テキストを標準出力に出す（読み取り専用）。

用途: スキルがコースページ（course/view.php）から課題の有無を読むため。
例: python tab_text.py --pattern course/view.php
    python tab_text.py --tab-id 2144333200 --max 8000
"""

from __future__ import annotations

import argparse
import sys

from platform_utils import _run_js_in_tab, get_tab_id_by_pattern, get_tab_url

# AppleScript の "..." に展開されるため、ダブルクォートとバックスラッシュは使えない
_TEXT_JS = (
    "(function(){var m=document.querySelector('#region-main')||document.querySelector('main')||document.body;"
    "return (document.title||'')+String.fromCharCode(10)+(m?m.innerText:'');})()"
)


def _dump_with_page_engine(a) -> int:
    """専用プロファイルの Chrome（ユーザーの Chrome ではない）で URL を開いて本文を出す。"""
    if not a.url:
        print("--engine page には --url が必要", file=sys.stderr)
        return 1
    from pathlib import Path
    from browser import BrowserSession, DEFAULT_PROFILE_DIR
    from main import COURSE_PAGE_TEXT_JS
    profile = Path(a.profile_dir).expanduser() if a.profile_dir else DEFAULT_PROFILE_DIR
    s = BrowserSession(profile_dir=profile, headless=False)
    try:
        s.start()
        s.goto(a.url)
        from urllib.parse import urlparse
        marker = urlparse(a.url).path.rsplit("/", 1)[-1] or a.url
        reached = s.wait_for_player(
            contains=marker, timeout=30.0, login_timeout=600.0,
            on_login_required=lambda u: print("ログイン画面です。専用ブラウザでログインしてください（最大 10 分待ちます）", file=sys.stderr),
        )
        if reached is None:
            print(f"ページに到達できませんでした: {s.url()}", file=sys.stderr)
            return 2
        s.pump(1500)
        text = s.evaluate(COURSE_PAGE_TEXT_JS) or ""
        print(f"# URL: {s.url()}")
        print(text[: a.max])
        return 0 if text else 2
    finally:
        s.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pattern", help="URL 部分一致でタブを探す（例: course/view.php）")
    p.add_argument("--tab-id", type=int, help="タブ id を直接指定")
    p.add_argument("--browser", default="chrome")
    p.add_argument("--max", type=int, default=12000, help="最大文字数")
    p.add_argument("--engine", choices=["user", "page"], default="user",
                   help="user: 本人の Chrome のタブを読む（既定） / page: 専用プロファイルの Chrome で --url を開いて読む")
    p.add_argument("--url", help="--engine page のとき開く URL（例: コースページ）")
    p.add_argument("--profile-dir", default=None)
    a = p.parse_args()
    if a.engine == "page":
        return _dump_with_page_engine(a)
    if a.tab_id is None and not a.pattern:
        p.error("--pattern か --tab-id のどちらかが必要")
    target = a.tab_id if a.tab_id is not None else get_tab_id_by_pattern(a.pattern, a.browser)
    if target is None:
        print(f"タブが見つかりません: {a.pattern!r}", file=sys.stderr)
        return 1
    url = get_tab_url(target, a.browser)
    text = _run_js_in_tab(_TEXT_JS, target, a.browser)
    if not text:
        print("本文を取得できませんでした（JS 実行権限・タブ状態を確認）", file=sys.stderr)
        return 2
    print(f"# URL: {url}")
    print(text[: a.max])
    return 0


if __name__ == "__main__":
    sys.exit(main())
