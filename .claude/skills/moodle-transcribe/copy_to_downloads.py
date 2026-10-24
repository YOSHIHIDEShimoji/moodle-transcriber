"""この実行の成果物だけを ~/Downloads/moodle-transcriber-YYYYMMDD/ にコピーする。

対象: run-summary.json の各 entry の output、同ディレクトリの 講義まとめ.md / 課題.md（存在すれば）、
run-summary.json 自身。削除はしない（上書きコピーのみ）。

python .claude/skills/moodle-transcribe/copy_to_downloads.py out/YYYYMMDD/run-summary.json [--dest DIR]
"""

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("summary")
    p.add_argument("--dest", default=None)
    a = p.parse_args()

    summary = Path(a.summary)
    d = json.loads(summary.read_text(encoding="utf-8"))
    src_dir = summary.parent
    dest = Path(a.dest) if a.dest else Path.home() / "Downloads" / f"moodle-transcriber-{datetime.now():%Y%m%d}"
    dest.mkdir(parents=True, exist_ok=True)

    files = [Path(e["output"]) for e in d["entries"] if e.get("output")]
    skipped = [e.get("url") for e in d["entries"] if not e.get("output")]
    files += [src_dir / n for n in ("講義まとめ.md", "課題.md")]
    files.append(summary)

    copied, missing = [], []
    for f in files:
        if f.exists():
            shutil.copy2(f, dest / f.name)
            copied.append(f.name)
        elif f.name not in ("講義まとめ.md", "課題.md"):
            missing.append(str(f))
    print(f"copied {len(copied)} files → {dest}")
    for u in skipped:
        print(f"  (出力なし・スキップ) {u}")
    for n in copied:
        print(f"  {n}")
    if missing:
        print("missing (not copied):")
        for m in missing:
            print(f"  {m}")
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
