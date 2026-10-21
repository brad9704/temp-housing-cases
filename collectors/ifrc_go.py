"""IFRC GO API 수집기 (구호기관 문서 트랙).

    uv run python -m collectors.ifrc_go list-documents [--page-size 200]
    uv run python -m collectors.ifrc_go summary

list-documents: 긴급호소 문서 메타데이터(appeal_document) 전체를 받아
    {RAW_DIR}/ifrc_go/appeal_documents.jsonl 로 저장한다. PDF 본문은 받지 않는다.
    재난 유형 이름표(disaster_type)도 함께 저장한다.
모든 요청은 PoliteClient.fetch()를 거친다.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from collectors.http import PoliteClient

API = "https://goadmin.ifrc.org/api/v2"


def _out_dir() -> Path:
    d = Path(os.environ.get("RAW_DIR", "data/raw")) / "ifrc_go"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _code_state() -> dict:
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout.strip())
    return {"code_commit": head, "code_dirty": dirty}


def _get_json(c: PoliteClient, url: str):
    r = c.fetch(url)
    if r.status != 200:
        raise RuntimeError(f"{r.status} {url}")
    return json.loads(r.content)


def cmd_list_documents(a) -> None:
    c = PoliteClient()
    out = _out_dir()
    meta = {"fetched_at": datetime.now(timezone.utc).isoformat(), "source": API, **_code_state()}

    types = _get_json(c, f"{API}/disaster_type/?limit=200")
    (out / "disaster_types.json").write_text(json.dumps(types, ensure_ascii=False))

    path = out / "appeal_documents.jsonl"
    tmp = path.with_suffix(".jsonl.part")
    n = 0
    url = f"{API}/appeal_document/?limit={a.page_size}&offset=0"
    with tmp.open("w") as fh:
        while url:
            d = _get_json(c, url)
            for rec in d["results"]:
                fh.write(json.dumps({**rec, "_meta": meta}, ensure_ascii=False) + "\n")
                n += 1
            url = d.get("next")
            print(f"{n}/{d.get('count')}", flush=True)
    tmp.replace(path)
    print(f"saved {n} -> {path}")


def cmd_summary(a) -> None:
    out = _out_dir()
    types = json.loads((out / "disaster_types.json").read_text())
    tname = {t["id"]: t["name"] for t in types.get("results", types)}
    recs = [json.loads(l) for l in (out / "appeal_documents.jsonl").read_text().splitlines()]
    doc_type = Counter(r.get("type") for r in recs)
    dtype = Counter(tname.get(((r.get("appeal") or {}).get("event") or {}).get("dtype"), "?") for r in recs)
    year = Counter((r.get("created_at") or "????")[:4] for r in recs)
    iso = Counter(r.get("iso") for r in recs)
    appeals = {(r.get("appeal") or {}).get("code") for r in recs}
    res = {
        "documents": len(recs),
        "appeals": len(appeals),
        "countries": len(iso),
        "doc_type": doc_type.most_common(),
        "disaster_type": dtype.most_common(),
        "year": sorted(year.items()),
        "top_countries": iso.most_common(25),
        "pdf_share": round(sum(1 for r in recs if str(r.get("document_url") or "").lower().endswith(".pdf")) / len(recs), 3),
    }
    print(json.dumps(res, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(prog="ifrc_go")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list-documents")
    p.add_argument("--page-size", type=int, default=200)
    p.set_defaults(fn=cmd_list_documents)
    p = sub.add_parser("summary")
    p.set_defaults(fn=cmd_summary)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
