"""文字起こし結果の自己診断と実行サマリー（スキル／人が機械的に検品するための出力）。

背景: 既往の障害 3 件はすべて「成功と報告するが実際は失敗」型だった
（空ファイル・幻覚 1 行・再生せず録音のみ）。終了時に最低限の指標を出し、
run-summary.json に書いておけば、後段（Claude スキル）が目視なしで判定できる。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

# Whisper が無音・音楽・雑音に対して出しがちな定型句
HALLUCINATION_PHRASES = (
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございます",
    "チャンネル登録",
    "最後までご視聴",
    "お疲れ様でした",
    "おつかれさまでした",
    "字幕",
)

# 日本語の講義は概ね 150〜300 字/分。これを大きく下回れば取りこぼしを疑う
MIN_CHARS_PER_MIN = 40.0
# 同一文（正規化後）の出現比率がこれを超えたら幻覚の連発を疑う
MAX_REPEATED_RATIO = 0.30
# 定型句だけの行がこれを超えたら失敗扱い
MAX_HALLUCINATION_RATIO = 0.50
# 定型句を含む行を「幻覚」と数える最大長（長い文の中に含まれる場合は本物の発話の可能性）
HALLUCINATION_MAX_LEN = 30

_WS_RE = re.compile(r"[\s。、．，.,!！?？「」『』・…]+")


def _norm(text: str) -> str:
    return _WS_RE.sub("", text)


def qa_metrics(
    texts: list[str],
    video_seconds: float,
    played: bool,
    stop_reason: str = "",
) -> dict:
    """文字起こし本文（行のリスト）から品質指標と判定を返す。

    status: "ok" / "warn"（人の確認推奨） / "failed"（やり直し推奨）
    """
    norm = [_norm(t) for t in texts if t and _norm(t)]
    lines = len(norm)
    chars = sum(len(t) for t in norm)
    minutes = video_seconds / 60.0 if video_seconds and video_seconds > 0 else 0.0
    cpm = chars / minutes if minutes > 0 else 0.0

    counts = Counter(norm)
    repeated = sum(c for c in counts.values() if c >= 2)
    repeated_ratio = repeated / lines if lines else 0.0

    halluc = sum(1 for t in norm
                 if any(p in t for p in HALLUCINATION_PHRASES) and len(t) <= HALLUCINATION_MAX_LEN)
    halluc_ratio = halluc / lines if lines else 0.0

    # failed 条件と warn 条件を独立に集め、最後に重い方を採用する（failed を格下げしない）
    failed: list[str] = []
    warn: list[str] = []

    if not played:
        if video_seconds is not None and video_seconds <= 0 and lines > 0:
            # 再生状態が一切取れない（ブラインド）が本文はある: 判定不能として warn に留める
            warn.append("再生状態を確認できなかった（ブラインドモード）。本文を目視で確認")
        else:
            failed.append("動画が再生されなかった（再生トリガー失敗）")
    if lines == 0:
        failed.append("文字起こしが 1 行も出ていない")
    elif halluc_ratio > MAX_HALLUCINATION_RATIO:
        failed.append(f"定型句（幻覚）の比率が高い: {halluc_ratio:.0%}")
    else:
        if minutes > 0 and cpm < MIN_CHARS_PER_MIN:
            warn.append(f"動画長に対して文字数が少なすぎる: {cpm:.0f} 字/分（目安 {MIN_CHARS_PER_MIN:.0f} 以上）")
        if repeated_ratio > MAX_REPEATED_RATIO:
            warn.append(f"同一文の連発: {repeated_ratio:.0%}（幻覚・逆相音声を疑う）")
    if "中止" in stop_reason or "watchdog" in stop_reason or "失敗" in stop_reason:
        warn.append(f"異常終了理由: {stop_reason}")

    warnings = failed + warn
    status = "failed" if failed else ("warn" if warn else "ok")

    return {
        "status": status,
        "lines": lines,
        "chars": chars,
        "video_seconds": round(float(video_seconds or 0.0), 1),
        "chars_per_min": round(cpm, 1),
        "repeated_ratio": round(repeated_ratio, 3),
        "hallucination_ratio": round(halluc_ratio, 3),
        "warnings": warnings,
    }


def write_run_summary(path: Path, entries: list[dict], finished: bool) -> None:
    """run-summary.json を書く（URL ごとに追記呼び出しされる。常に全量を書き直す）。"""
    statuses = [e.get("qa", {}).get("status", "unknown") for e in entries]
    doc = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "finished": finished,
        "total": len(entries),
        "ok": statuses.count("ok"),
        "warn": statuses.count("warn"),
        "failed": statuses.count("failed") + statuses.count("interrupted"),
        "entries": entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def format_qa_line(entry: dict) -> str:
    q = entry.get("qa", {})
    mark = {"ok": "✔", "warn": "⚠", "failed": "✗", "interrupted": "■"}.get(q.get("status"), "?")
    base = (f"  {mark} {entry.get('title') or entry.get('url')}  "
            f"{q.get('lines', 0)} 行 / {q.get('chars_per_min', 0):.0f} 字/分")
    if q.get("warnings"):
        base += "\n" + "\n".join(f"      - {w}" for w in q["warnings"])
    return base
