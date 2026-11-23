"""文字起こしの機械的な校正（スキルの検品ステップで使う）。

対象は TXT 形式の本文行（`[hh:mm:ss] 本文`）だけ。ヘッダ（# 行）・空行・それ以外の行は一切触らない。
本文行が 1 行も無いファイル（course-page.txt、SRT/VTT など）は丸ごとスキップする。

直すのは「判断がいらない」ものだけ:
1. 定型句だけの行（Whisper の幻覚。例「ご視聴ありがとうございました」）を削除。
   正規化（空白・句読点除去）後に定型句と**完全一致**する行だけ。定型句を含む本物の発話は残す。
2. 境界重複: 残っている本文行のうち隣り合う 2 行で、一方が他方の**先頭部分**（正規化後 8 文字以上）なら
   短い方を削除（セグメントのオーバーラップで同じ発話が 2 回出たもの）。包含や非隣接では発火しない。
3. 用語辞書（JSON: {"誤": "正", ...}）による置換。元の本文に対して 1 パスで置換位置を決める。
   先に始まる語から確定し、同じ位置から始まる語同士では長い語を優先する。置換結果に別の誤語が現れても
   二重に置換しない。辞書は「どの誤語もどの正にも含まれない」ことを要求し、さらに各行で
   「2 回適用した結果 = 1 回適用した結果」を確かめる（再適用で増殖・往復しないこと）。
4. 辞書の "__delete__" に列挙した行（本文の完全一致）を削除。

元ファイルは <dir>/raw/ に退避（既にあれば上書きしない＝最初の原本を保つ）し、
変更は 校正ログ.md に「ファイル / 行 / 前 → 後」で残す。行番号は適用直前のファイル基準。

使い方:
  python fix_transcript.py --glossary out/20261024/glossary.json out/20261024/*.txt
  python fix_transcript.py --dry-run --glossary g.json a.txt   # 変更内容だけ表示（ファイル・ログは書かない）
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from qa import HALLUCINATION_PHRASES

_BODY_RE = re.compile(r"^(\[\d\d:\d\d:\d\d\] )(.*?)(\r?\n)?$", re.S)
_PUNCT_RE = re.compile(r"[\s。、．，.,!！?？「」『』・…]+")
MIN_PREFIX_LEN = 8
_HALLUC_NORM = {_PUNCT_RE.sub("", p) for p in HALLUCINATION_PHRASES}


class GlossaryError(ValueError):
    pass


def _norm(text: str) -> str:
    return _PUNCT_RE.sub("", text)


def load_glossary(raw: dict) -> tuple[dict[str, str], set[str]]:
    """辞書 JSON を検証して (置換辞書, 削除行集合) を返す。不正なら GlossaryError（黙って捨てない）。"""
    if not isinstance(raw, dict):
        raise GlossaryError("辞書はオブジェクト（{\"誤\": \"正\"}）で書く")
    raw = dict(raw)
    delete = raw.pop("__delete__", [])
    if not isinstance(delete, list) or not all(isinstance(x, str) and x.strip() for x in delete):
        raise GlossaryError("__delete__ は空でない文字列のリストで書く")
    glossary: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(v, str):
            raise GlossaryError(f"値が文字列ではない: {k!r}")
        if not k.strip():
            raise GlossaryError("空・空白だけの誤語は書けない")
        if k == v:
            continue
        glossary[k] = v
    # どの誤語も、どの項目の正にも含まれてはいけない（「千葉大」→「千葉大学」の増殖や、入れ替え辞書の往復を防ぐ）
    for k in glossary:
        for k2, v2 in glossary.items():
            if k in v2:
                raise GlossaryError(f"誤 {k!r} が正 {v2!r}（{k2!r} の置換先）に含まれるため再適用で変化する。前後を含む語句で書く")
    return glossary, {x.strip() for x in delete}


def _replace_once(text: str, pattern: re.Pattern | None, glossary: dict[str, str]) -> str:
    if pattern is None:
        return text
    return pattern.sub(lambda m: glossary[m.group(0)], text)


def fix_lines(lines: list[str], glossary: dict[str, str], delete: set[str]) -> tuple[list[str], list[dict]]:
    """行リストを校正して (新しい行リスト, 変更ログ) を返す。変更ログは 1 行につき最大 1 件・行番号順。"""
    parsed: dict[int, tuple[str, str, str]] = {}
    for i, line in enumerate(lines):
        m = _BODY_RE.match(line)
        if m:
            parsed[i] = (m.group(1), m.group(2), m.group(3) or "")
    body = sorted(parsed)

    removed: dict[int, str] = {}   # 行 index → ルール名
    # 1) 幻覚行 / 4) 指定行（どちらか 1 つだけ記録）
    for i in body:
        text = parsed[i][1]
        n = _norm(text)
        if n and n in _HALLUC_NORM:
            removed[i] = "幻覚行削除"
        elif text.strip() in delete:
            removed[i] = "指定行削除"

    # 2) 境界重複: 残っている本文行を順に見て、直前に残っている行とだけ比較する
    prev: int | None = None
    for i in body:
        if i in removed:
            continue
        if prev is not None:
            a, b = _norm(parsed[prev][1]), _norm(parsed[i][1])
            if len(a) >= MIN_PREFIX_LEN and len(b) >= MIN_PREFIX_LEN:
                if len(b) > len(a) and b.startswith(a):
                    removed[prev] = "境界重複削除"     # 短い先頭断片（前）を消し、長い方を残す
                elif len(a) > len(b) and a.startswith(b):
                    removed[i] = "境界重複削除"        # 後ろの短い断片を消す。prev は長い行のまま
                    continue
        prev = i

    # 3) 辞書置換（1 パス・長い語優先）
    pattern = (re.compile("|".join(re.escape(k) for k in sorted(glossary, key=lambda k: (-len(k), k))))
               if glossary else None)
    out: list[str] = []
    changes: list[dict] = []
    for i, line in enumerate(lines):
        if i not in parsed:
            out.append(line)
            continue
        ts, text, nl = parsed[i]
        if i in removed:
            changes.append({"line": i + 1, "rule": removed[i], "before": text, "after": ""})
            continue
        new = _replace_once(text, pattern, glossary)
        if _replace_once(new, pattern, glossary) != new:
            # 置換結果と前後の文字にまたがって別の誤語が一致する（例: 寒卓→干拓 と 拓地→X）。2 回目で変わる
            raise GlossaryError(f"再適用で結果が変わる置換がある（{i + 1} 行目: {text!r} → {new!r}）。辞書を見直す")
        if new != text:
            changes.append({"line": i + 1, "rule": "用語置換", "before": text, "after": new})
        out.append(f"{ts}{new}{nl}")
    return out, changes


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+")
    p.add_argument("--glossary", help="用語辞書 JSON（{\"誤\": \"正\"}。\"__delete__\": [行...] で行削除）")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--log", default=None, help="校正ログの出力先（既定: 最初のファイルと同じディレクトリの 校正ログ.md）")
    a = p.parse_args(argv)

    glossary: dict[str, str] = {}
    delete: set[str] = set()
    if a.glossary:
        try:
            glossary, delete = load_glossary(json.loads(Path(a.glossary).read_text(encoding="utf-8")))
        except (GlossaryError, json.JSONDecodeError) as e:
            print(f"辞書エラー: {e}", file=sys.stderr)
            return 2

    files = [Path(f) for f in a.files]
    log_path = Path(a.log) if a.log else files[0].parent / "校正ログ.md"

    # 先に全ファイルを校正してみる（書き込みはしない）。辞書の問題で途中から失敗して半端に書き換わるのを防ぐ
    plans: list[tuple[Path, list[str], list[dict]]] = []
    skipped: list[str] = []
    for f in files:
        lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
        if not any(_BODY_RE.match(l) for l in lines):
            skipped.append(f.name)          # course-page.txt / SRT / VTT など。後続のファイルは続けて処理する
            continue
        try:
            new, changes = fix_lines(lines, glossary, delete)
        except GlossaryError as e:
            print(f"辞書エラー（{f.name}）: {e}", file=sys.stderr)
            return 2
        plans.append((f, new, changes))

    total = sum(len(c) for _, _, c in plans)
    header = f"\n## {datetime.now():%Y-%m-%d %H:%M} 校正（{'dry-run' if a.dry_run else '適用'}）\n"
    if total:
        print(header)
    wrote_header = False
    for f, new, changes in plans:
        if not changes:
            continue
        block = [f"\n### {f.name}（{len(changes)} 件）\n"]
        for c in changes:
            arrow = f"→ {c['after']}" if c["after"] else "→ （削除）"
            block.append(f"- L{c['line']} [{c['rule']}] {c['before']} {arrow}")
        text = "\n".join(block) + "\n"
        print(text)
        if a.dry_run:
            continue
        raw_dir = f.parent / "raw"
        raw_dir.mkdir(exist_ok=True)
        raw = raw_dir / f.name
        if not raw.exists():                # 最初の原本だけを保つ（再適用で上書きしない）
            shutil.copy2(f, raw)
        f.write_text("".join(new), encoding="utf-8")
        # ファイルごとにログを追記する（途中で失敗しても、書き換えたファイルの記録は残る）
        is_new = not log_path.exists() or log_path.stat().st_size == 0
        with log_path.open("a", encoding="utf-8") as fh:
            if is_new:
                fh.write("# 校正ログ\n\n元ファイルは raw/ に退避（最初の原本）。行番号は各回の適用直前のファイル基準。\n"
                         "ルール: 幻覚行削除 / 境界重複削除 / 用語置換 / 指定行削除\n")
            if not wrote_header:
                fh.write(header)
                wrote_header = True
            fh.write(text)
    if total and not a.dry_run:
        print(f"校正ログ: {log_path}")
    for name in skipped:
        print(f"スキップ（[hh:mm:ss] 形式の本文行なし）: {name}")
    print(f"変更 {total} 件 / {len(plans)} ファイル（スキップ {len(skipped)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
