"""2 倍速の確認用に先に回した 1 本のサマリーを、本体の run-summary.json に統合する。

2 倍速の安全手順では、最初の 1 本を `--json-summary <dir>/first-run-summary.json` で同じ出力ディレクトリに回し、
残りを通常どおり（run-summary.json）回す。両方そろったらこれで 1 つにまとめる（検品・コピーが 1 ファイルで済む）。

python .claude/skills/moodle-transcribe/merge_summaries.py out/YYYYMMDD/first-run-summary.json out/YYYYMMDD/run-summary.json

- first の entries を先頭に置き、index を振り直し、件数（total/ok/warn/failed）と finished を再計算して run-summary.json に書く。
- 同じ URL が両方にある場合（2 倍が不可で等速で全部回し直したとき）は run-summary 側を残し、first 側は捨てる。
- first のファイルは消さない（統合後に `trash -v` で片付ける）。
"""

import json
import sys
from datetime import datetime
from pathlib import Path


def merge(first: dict, main: dict) -> dict:
    main_urls = {e.get("url") for e in main["entries"]}
    entries = [e for e in first["entries"] if e.get("url") not in main_urls] + list(main["entries"])
    for i, e in enumerate(entries):
        e["index"] = i
    statuses = [e.get("qa", {}).get("status") for e in entries]
    out = dict(main)
    out.update(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        finished=bool(first.get("finished")) and bool(main.get("finished")),
        total=len(entries),
        ok=statuses.count("ok"),
        warn=statuses.count("warn"),
        failed=len(entries) - statuses.count("ok") - statuses.count("warn"),
        entries=entries,
    )
    return out


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    first_path, main_path = Path(sys.argv[1]), Path(sys.argv[2])
    first = json.loads(first_path.read_text(encoding="utf-8"))
    main_d = json.loads(main_path.read_text(encoding="utf-8"))
    merged = merge(first, main_d)
    main_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    dropped = len(first["entries"]) + len(main_d["entries"]) - merged["total"]
    print(f"統合: {merged['total']} 本（ok={merged['ok']} warn={merged['warn']} failed={merged['failed']}）→ {main_path}")
    if dropped:
        print(f"  first 側の {dropped} 本は等速の撮り直しが run-summary にあるので除外")
    print(f"片付け: trash -v {first_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
