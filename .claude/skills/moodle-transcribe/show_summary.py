"""run-summary.json を 1 行/URL で表示する（検品用）。

python .claude/skills/moodle-transcribe/show_summary.py out/YYYYMMDD/run-summary.json
"""

import json
import sys


def main() -> int:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    print(f"finished={d['finished']} total={d['total']} ok={d['ok']} warn={d['warn']} failed={d['failed']}")
    for e in d["entries"]:
        q = e["qa"]
        print(f"[{e['index'] + 1}] {q['status']:<11} {e.get('title') or '(no title)'}")
        print(f"      {e['output']}")
        print(f"      {q['lines']} 行 / {q['chars_per_min']:.0f} 字/分 / 動画 {q['video_seconds']:.0f} 秒 / "
              f"played={e['played']} / 終了: {e['stop_reason'] or '-'}")
        if "speed" in e:
            pct = e.get("viewing_pct", -1)
            print(f"      速度 {e['speed']:g}x / 保存後の視聴状況 {str(pct) + '%' if pct >= 0 else '不明'}")
        for w in q.get("warnings", []):
            print(f"      ⚠ {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
