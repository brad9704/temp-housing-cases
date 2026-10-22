"""뉴스 원본 HTML → 본문 텍스트(범용 추출) + 정규식 필터 결과.

    uv run python -m pipeline.news_text extract {outlet_id|all}
    uv run python -m pipeline.news_text report [--sample-tag TAG]

extract: html_index.jsonl의 성공 기록마다 trafilatura로 제목·날짜·본문을 뽑고 v0 필터를 돌려
    {DERIVED_DIR}/news_text/<outlet>.jsonl 에 쓴다. 이미 처리한 URL은 건너뛴다.
    추출 성공 기준은 recon과 같다: 본문 300자 이상 + 날짜 추출.
report: 언론사별 추출 성공률, 필터 통과율(tier별)을 출력한다.
원문 텍스트는 내부 분석용이다. 재배포하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import trafilatura
import zstandard

from pipeline.filter import Filter

MIN_CHARS = 300


def _news_dir() -> Path:
    return Path(os.environ.get("RAW_DIR", "data/raw")) / "news"


def _out_dir() -> Path:
    d = Path(os.environ.get("DERIVED_DIR", "data/derived")) / "news_text"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().split("\n") if l.strip()] if path.exists() else []


def _code_state() -> dict:
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True).stdout.strip())
    return {"code_commit": head, "code_dirty": dirty}


def _lang(oid: str) -> str:
    return json.loads(Path(f"registry/outlet_profiles/{oid}.json").read_text())["languages"][0]


def extract_one(html: bytes, url: str) -> dict:
    doc = trafilatura.bare_extraction(html, url=url, with_metadata=True, favor_precision=True)
    if doc is None:
        return {"title": None, "pub_date": None, "text": ""}
    d = doc.as_dict() if hasattr(doc, "as_dict") else dict(doc)
    return {"title": d.get("title"), "pub_date": d.get("date"), "text": d.get("text") or ""}


def cmd_extract(a) -> None:
    targets = sorted(p.parent.name for p in _news_dir().glob("*/html_index.jsonl")) if a.outlet == "all" else [a.outlet]
    flt, dctx, code = Filter(), zstandard.ZstdDecompressor(), _code_state()
    for oid in targets:
        out = _out_dir() / f"{oid}.jsonl"
        done = {x["url"] for x in _jsonl(out)}
        lang = _lang(oid)
        n = 0
        with out.open("a") as fh:
            for rec in _jsonl(_news_dir() / oid / "html_index.jsonl"):
                if not rec.get("ok") or rec["url"] in done:
                    continue
                done.add(rec["url"])
                e = extract_one(dctx.decompress(Path(rec["path"]).read_bytes()), rec["final_url"])
                f = flt.match(e["text"], lang) if e["text"] else {"pass": False, "tier": None, "hits": [], "dict_version": flt.version}
                fh.write(json.dumps({
                    "outlet_id": oid, "url": rec["url"], "list_date": rec.get("date"), "sample": rec.get("sample"),
                    "lang": lang, **e, "text_chars": len(e["text"]),
                    "extract_ok": len(e["text"]) >= MIN_CHARS and bool(e["pub_date"]),
                    "filter": {k: f[k] for k in ("pass", "tier", "dict_version")},
                    "filter_hits": [{k: h[k] for k in ("tier", "pattern", "text")} for h in f["hits"][:20]],
                    "extractor": f"trafilatura {trafilatura.__version__} favor_precision",
                    "processed_at": datetime.now(timezone.utc).isoformat(), **code,
                }, ensure_ascii=False) + "\n")
                n += 1
        print(f"{oid}: {n} extracted", flush=True)


def cmd_report(a) -> None:
    rows = []
    for p in sorted(_out_dir().glob("*.jsonl")):
        recs = [r for r in _jsonl(p) if not a.sample_tag or r.get("sample") == a.sample_tag]
        if not recs:
            continue
        n = len(recs)
        tiers = Counter(r["filter"]["tier"] for r in recs if r["filter"]["pass"])
        rows.append({
            "outlet": p.stem, "n": n,
            "extract_ok": round(sum(r["extract_ok"] for r in recs) / n, 3),
            "short_text": sum(r["text_chars"] < MIN_CHARS for r in recs),
            "no_date": sum(not r["pub_date"] for r in recs),
            "median_chars": sorted(r["text_chars"] for r in recs)[n // 2],
            "filter_pass": sum(tiers.values()), "tier1": tiers.get("tier1", 0), "tier2": tiers.get("tier2", 0),
        })
    print(json.dumps(rows, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(prog="news_text")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("extract")
    p.add_argument("outlet")
    p.set_defaults(fn=cmd_extract)
    p = sub.add_parser("report")
    p.add_argument("--sample-tag")
    p.set_defaults(fn=cmd_report)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
