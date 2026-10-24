"""moodle-transcribe スキルの事前確認（読み取りのみ）。終了コード 0 = 合格。

リポジトリ直下から実行する: python .claude/skills/moodle-transcribe/preflight.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from platform_utils import (  # noqa: E402
    _run_js_in_tab,
    get_moodle_page_info,
    get_tab_id_by_pattern,
    warn_other_audio_apps,
)

MOODLE_PATTERN = os.environ.get("MOODLE_PATTERN", "moodle.gs.chiba-u.jp")


def _check_page_engine() -> bool:
    """page エンジン（既定）: playwright と Google Chrome があればよい。ユーザーの Chrome・BlackHole は不要。"""
    ok = True
    try:
        import playwright  # noqa: F401
        from importlib.metadata import version
        print(f"playwright: {version('playwright')}")
    except Exception as e:  # noqa: BLE001
        print(f"playwright: 未導入 ({e}) → pip install -r requirements.txt")
        ok = False
    chrome = Path("/Applications/Google Chrome.app").exists()
    print(f"Google Chrome.app: {chrome}")
    if not chrome:
        ok = False
    from browser import DEFAULT_PROFILE_DIR
    prof = DEFAULT_PROFILE_DIR
    has_profile = (prof / "Default").exists()
    print(f"profile: {prof} ({'あり' if has_profile else '初回（ログインが必要になる）'})")
    return ok


def _check_blackhole_engine() -> bool:
    """blackhole エンジン（従来）: ユーザーの Chrome の Moodle タブ・BlackHole・複数出力装置が必要。"""
    ok = True
    tid = get_tab_id_by_pattern(MOODLE_PATTERN)
    info = get_moodle_page_info(tid) if tid is not None else (None, None)
    print(f"moodle tab: {tid} {info}")
    js_ok = bool(_run_js_in_tab("document.title", tid)) if tid is not None else False
    print(f"js ok: {js_ok}")
    if tid is None or not js_ok:
        ok = False
    title, url = info
    if url and ("/login/" in url or (title and "ログイン" in title)):
        print("  → Moodle がログイン画面。本人に Chrome でログインしてもらう（Claude はログインしない）")
        ok = False

    playing = warn_other_audio_apps()
    print(f"playing apps: {playing}")
    if playing:
        ok = False

    sas = shutil.which("SwitchAudioSource")
    print(f"SwitchAudioSource: {bool(sas)}")
    devices: list[str] = []
    if sas:
        r = subprocess.run([sas, "-a", "-t", "output"], capture_output=True, text=True)
        devices = [d for d in r.stdout.splitlines() if d.strip()]
    print(f"devices: {devices}")
    has_bh = any("BlackHole" in d for d in devices)
    has_multi = any(("Multi-Output" in d) or ("複数出力" in d) for d in devices)
    if not (has_bh and has_multi):
        print("  → BlackHole / 複数出力装置 が見つからない（USAGE.md の初期セットアップ）")
        ok = False

    return ok


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["page", "blackhole"], default="page")
    engine = ap.parse_args().engine
    print(f"engine: {engine}")
    ok = _check_page_engine() if engine == "page" else _check_blackhole_engine()

    w = Path(os.environ.get("WHISPER_CPP_DIR", str(Path.home() / "my-projects/whisper.cpp")))
    server = (w / "build/bin/whisper-server").exists()
    model = (w / "models/ggml-large-v3-turbo.bin").exists()
    print(f"whisper-server: {server}  model(large-v3-turbo): {model}  ({w})")
    if not (server and model):
        ok = False

    # このリポジトリの main.py だけを見る（他プロジェクトの main.py で NG にしない）
    r = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True)
    running = [l.strip() for l in r.stdout.splitlines()
               if "main.py" in l and (str(ROOT) in l or "moodle-transcriber" in l)
               and "preflight.py" not in l]
    print(f"running main.py: {running or 'none'}")
    if running:
        ok = False

    print("PREFLIGHT:", "OK" if ok else "NG")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
