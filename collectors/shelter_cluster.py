"""Shelter Cluster 수집기 (구호기관 문서 트랙).

    uv run python -m collectors.shelter_cluster list-documents
    uv run python -m collectors.shelter_cluster summary

list-documents: 사이트맵 색인(robots 허용)을 읽어 문서 페이지 URL(`/<group>/documents/<slug>`)
    목록을 {RAW_DIR}/shelter_cluster/documents.jsonl 로 저장한다. 문서 페이지·파일 본문은 받지 않는다.
robots의 Content-Signal은 `ai-train=no`(학습 금지)뿐이라 본 연구에 해당하지 않는다 (2026-10-21 결정).
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from collectors.http import PoliteClient
from collectors.recon_probe import _parse_sitemap

INDEX = "https://www.sheltercluster.org/sitemap.xml"


def _out_dir() -> Path:
    d = Path(os.environ.get("RAW_DIR", "data/raw")) / "shelter_cluster"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _code_state() -> dict:
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True).stdout.strip())
    return {"code_commit": head, "code_dirty": dirty}


def _sitemap(c: PoliteClient, url: str) -> tuple[str, list[dict]]:
    r = c.fetch(url)
    if r.status != 200:
        raise RuntimeError(f"{r.status} {url}")
    b = r.content
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    return _parse_sitemap(b)


def cmd_list_documents(a) -> None:
    c = PoliteClient()
    meta = {"source": INDEX, "fetched_at": datetime.now(timezone.utc).isoformat(), **_code_state()}
    kind, pages = _sitemap(c, INDEX)
    pages = [p["loc"] for p in pages] if kind == "sitemapindex" else [INDEX]
    docs, other = [], Counter()
    for i, page in enumerate(pages, 1):
        _, ents = _sitemap(c, page)
        for e in ents:
            parts = [x for x in urlsplit(e["loc"]).path.split("/") if x]
            if len(parts) >= 3 and parts[-2] == "documents":
                docs.append({"url": e["loc"], "group": parts[0], "slug": parts[-1],
                             "lastmod": e.get("lastmod"), "_meta": meta})
            else:
                other[parts[0] if parts else "/"] += 1
        print(f"page {i}/{len(pages)}: documents so far {len(docs)}", flush=True)
    path = _out_dir() / "documents.jsonl"
    tmp = path.with_suffix(".part")
    tmp.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in docs))
    tmp.replace(path)
    print(f"saved {len(docs)} -> {path}; non-document entries by first segment: {other.most_common(8)}")


def cmd_summary(a) -> None:
    docs = [json.loads(l) for l in (_out_dir() / "documents.jsonl").read_text().split("\n") if l.strip()]
    groups = Counter(d["group"] for d in docs)
    year = Counter((d.get("lastmod") or "????")[:4] for d in docs)
    print(json.dumps({"documents": len(docs), "groups": len(groups),
                      "top_groups": groups.most_common(30), "lastmod_year": sorted(year.items())},
                     ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(prog="shelter_cluster")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-documents").set_defaults(fn=cmd_list_documents)
    sub.add_parser("summary").set_defaults(fn=cmd_summary)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
