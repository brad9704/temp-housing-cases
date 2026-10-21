"""구호기관 문서 본문 수집기.

    uv run python -m collectors.doc_fetch select {ifrc_go|shelter_cluster}
    uv run python -m collectors.doc_fetch fetch  {ifrc_go|shelter_cluster} [--limit N] [--workers 4]
    uv run python -m collectors.doc_fetch status {ifrc_go|shelter_cluster}

select: codebook/doc_prefilter_v0.yaml 규칙으로 받을 문서를 고른다. 걸러낸 문서 중 무작위 표본은
    `audit=True`로 함께 고른다(누락 감사용). 결과: {RAW_DIR}/<source>/selected.jsonl
fetch: 고른 문서를 받아 {RAW_DIR}/<source>/files/<id>.<ext>.zst 로 저장하고 files_index.jsonl에 기록한다.
    이미 성공한 문서는 건너뛴다. Shelter Cluster는 문서 페이지를 먼저 받아 파일 링크를 찾는다.
    도메인별 연속 403/429가 STOP_STREAK에 이르면 그 도메인은 멈추고 기록한다.
모든 요청은 PoliteClient.fetch()를 거친다. 병렬은 도메인 간에만 생긴다(같은 도메인은 클라이언트가 직렬화).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import yaml
import zstandard

from collectors.http import FetchFailed, PoliteClient, RobotsDisallowed

RULES = Path("codebook/doc_prefilter_v0.yaml")
STOP_STREAK = 3
FILE_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|odt)$", re.I)


def _raw(source: str) -> Path:
    d = Path(os.environ.get("RAW_DIR", "data/raw")) / source
    d.mkdir(parents=True, exist_ok=True)
    return d


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().split("\n") if l.strip()] if path.exists() else []


def _code_state() -> dict:
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True).stdout.strip())
    return {"code_commit": head, "code_dirty": dirty}


def _rules() -> dict:
    return yaml.safe_load(RULES.read_text())


# ---- select ---------------------------------------------------------------------------
def _select_ifrc(r: dict) -> list[dict]:
    raw = _raw("ifrc_go")
    types = {t["id"]: t["name"] for t in json.loads((raw / "disaster_types.json").read_text())["results"]}
    rule = r["ifrc_go"]
    out, seen = [], set()
    for d in _jsonl(raw / "appeal_documents.jsonl"):
        if d["id"] in seen:
            continue
        seen.add(d["id"])
        url = d.get("document_url") or d.get("document")
        if not url:
            continue
        dtype = types.get(((d.get("appeal") or {}).get("event") or {}).get("dtype"), "?")
        keep = dtype not in rule["exclude_disaster_types"] and d.get("type") in rule["include_doc_types"]
        out.append({"doc_id": f"ifrc-{d['id']}", "url": url, "keep": keep,
                    "meta": {"type": d.get("type"), "disaster_type": dtype, "iso": d.get("iso"),
                             "appeal": (d.get("appeal") or {}).get("code"), "created_at": d.get("created_at"),
                             "name": d.get("name")}})
    return out


def _select_shelter_cluster(r: dict) -> list[dict]:
    terms = r["shelter_cluster"]["exclude_slug_terms"]
    pat = re.compile(r"(^|-)(" + "|".join(map(re.escape, terms)) + r")(-|$)")
    out = []
    for d in _jsonl(_raw("shelter_cluster") / "documents.jsonl"):
        out.append({"doc_id": f"sc-{d['group']}--{d['slug']}"[:200], "url": d["url"],
                    "keep": not pat.search(d["slug"]),
                    "meta": {"group": d["group"], "slug": d["slug"], "lastmod": d.get("lastmod")}})
    return out


def cmd_select(a) -> None:
    r = _rules()
    rows = {"ifrc_go": _select_ifrc, "shelter_cluster": _select_shelter_cluster}[a.source](r)
    excluded = [x for x in rows if not x["keep"]]
    aud = r["audit_sample"]
    n_aud = min(len(excluded), max(aud["min_per_source"], round(len(excluded) * aud["share"])))
    audit_ids = {x["doc_id"] for x in random.Random(aud["seed"]).sample(excluded, n_aud)}
    sel = []
    for x in rows:
        if x["keep"] or x["doc_id"] in audit_ids:
            sel.append({**x, "audit": not x["keep"], "rules": str(RULES)})
    path = _raw(a.source) / "selected.jsonl"
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in sel))
    print(f"{a.source}: total {len(rows)}, kept {len(rows) - len(excluded)}, audit sample {n_aud} -> {path}")


# ---- fetch ----------------------------------------------------------------------------
class Fetcher:
    def __init__(self, source: str):
        self.source = source
        self.client = PoliteClient()
        self.dir = _raw(source)
        self.files = self.dir / "files"
        self.files.mkdir(exist_ok=True)
        self.index = self.dir / "files_index.jsonl"
        self.lock = threading.Lock()
        self.streak: Counter = Counter()
        self.stopped: set[str] = set()
        self.cctx = zstandard.ZstdCompressor(level=10)
        self.code = _code_state()

    def _log(self, rec: dict) -> None:
        with self.lock:
            with self.index.open("a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _get(self, url: str):
        host = urlsplit(url).netloc
        if host in self.stopped:
            return None, "domain_stopped"
        try:
            r = self.client.fetch(url)
        except RobotsDisallowed:
            return None, "robots_disallowed"
        except FetchFailed as e:
            if " 429 " in f" {e} ":
                self._streak(host)
            return None, f"fetch_failed: {str(e)[-200:]}"
        if r.status in (401, 403, 429):
            self._streak(host)
        else:
            with self.lock:
                self.streak[host] = 0
        return r, None

    def _streak(self, host: str) -> None:
        with self.lock:
            self.streak[host] += 1
            if self.streak[host] >= STOP_STREAK and host not in self.stopped:
                self.stopped.add(host)
                print(f"STOP domain {host}: {STOP_STREAK} consecutive 401/403/429", flush=True)

    def _store(self, doc_id: str, url: str, r) -> dict:
        ctype = (r.headers.get("content-type") or r.headers.get("Content-Type") or "").split(";")[0].strip()
        m = FILE_EXT.search(urlsplit(r.final_url).path)
        ext = (m.group(1).lower() if m else {"application/pdf": "pdf", "text/html": "html"}.get(ctype, "bin"))
        if r.content[:5] == b"%PDF-":  # 서버가 octet-stream으로 보내는 경우
            ext = "pdf"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", doc_id)
        path = self.files / f"{safe}__{hashlib.sha1(url.encode()).hexdigest()[:8]}.{ext}.zst"
        path.write_bytes(self.cctx.compress(r.content))
        return {"path": str(path), "bytes": len(r.content), "sha256": hashlib.sha256(r.content).hexdigest(),
                "content_type": ctype, "final_url": r.final_url, "status": r.status,
                "robots_exception": getattr(r, "robots_exception", False)}

    def _sc_file_links(self, page_html: bytes, base: str) -> list[str]:
        html = page_html.decode("utf-8", errors="replace")
        links = {urljoin(base, h) for h in re.findall(r'href=["\']([^"\']+)', html)}
        return sorted(u for u in links if FILE_EXT.search(unquote(urlsplit(u).path))
                      and ("sheltercluster" in u))

    def one(self, item: dict) -> dict:
        base = {"doc_id": item["doc_id"], "url": item["url"], "audit": item.get("audit", False),
                "fetched_at": datetime.now(timezone.utc).isoformat(), **self.code}
        r, err = self._get(item["url"])
        if err or r.status != 200:
            rec = {**base, "ok": False, "error": err or f"HTTP {r.status}"}
            self._log(rec)
            return rec
        stored = self._store(item["doc_id"], item["url"], r)
        files = []
        if self.source == "shelter_cluster" and stored["content_type"] == "text/html":
            for fu in self._sc_file_links(r.content, r.final_url)[:5]:
                fr, ferr = self._get(fu)
                if ferr or fr.status != 200:
                    files.append({"url": fu, "ok": False, "error": ferr or f"HTTP {fr.status}"})
                else:
                    files.append({"url": fu, "ok": True, **self._store(item["doc_id"], fu, fr)})
        rec = {**base, "ok": True, **stored, "files": files}
        self._log(rec)
        return rec


def cmd_fetch(a) -> None:
    free_gb = shutil.disk_usage(_raw(a.source)).free / 1e9
    min_gb = float(os.environ.get("MIN_FREE_GB", 100))
    if free_gb < min_gb:
        raise SystemExit(f"여유 공간 {free_gb:.0f}GB < MIN_FREE_GB {min_gb:.0f}GB: 수집 중단")
    f = Fetcher(a.source)
    done = {x["doc_id"] for x in _jsonl(f.index) if x.get("ok")}
    todo = [x for x in _jsonl(f.dir / "selected.jsonl") if x["doc_id"] not in done]
    if a.limit:
        todo = todo[: a.limit]
    print(f"{a.source}: {len(todo)} to fetch ({len(done)} already ok)", flush=True)
    ok = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, rec in enumerate(ex.map(f.one, todo), 1):
            ok += rec["ok"]
            if i % 50 == 0:
                print(f"[{i}/{len(todo)}] ok={ok} stopped={sorted(f.stopped)}", flush=True)
    print(f"done: {ok}/{len(todo)} ok; stopped domains: {sorted(f.stopped)}")


def cmd_status(a) -> None:
    idx = _jsonl(_raw(a.source) / "files_index.jsonl")
    sel = _jsonl(_raw(a.source) / "selected.jsonl")
    last = {}
    for x in idx:
        last[x["doc_id"]] = x
    c = Counter("ok" if x.get("ok") else (x.get("error") or "?")[:40] for x in last.values())
    print(json.dumps({"selected": len(sel), "attempted": len(last), "outcomes": c.most_common(10),
                      "bytes": sum(x.get("bytes", 0) + sum(f.get("bytes", 0) for f in x.get("files", []))
                                   for x in last.values() if x.get("ok"))}, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(prog="doc_fetch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("select", cmd_select), ("fetch", cmd_fetch), ("status", cmd_status)):
        p = sub.add_parser(name)
        p.add_argument("source", choices=["ifrc_go", "shelter_cluster"])
        if name == "fetch":
            p.add_argument("--limit", type=int)
            p.add_argument("--workers", type=int, default=4)
        p.set_defaults(fn=fn)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
