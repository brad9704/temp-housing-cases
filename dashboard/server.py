"""수집 진행 대시보드 (로컬 네트워크용, 읽기 전용).

    uv run python -m dashboard.server [--host 0.0.0.0] [--port 8090]

GET /            대시보드 화면
GET /api/status  현재 상태 JSON (로컬 데이터에서 계산, CACHE_SEC 동안 캐시)
GET /static/...  정적 파일 (dashboard/static 밖은 내주지 않는다)

API 응답에는 집계값과 언론사·문서 메타데이터만 넣는다. 파일 경로, appname, .env 값, 원문은 넣지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import shutil
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pycountry
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
CACHE_SEC = 60

load_dotenv(ROOT / ".env")
RAW = (ROOT / os.environ.get("RAW_DIR", "data/raw")).resolve()
STATE = (ROOT / os.environ.get("CRAWL_STATE_DIR", "data/cache/crawl_state")).resolve()

USABLE = {"route_deep", "route_mid", "route_alt", "wayback_only"}
PRESS = {"national_daily", "online", "regional_daily"}
BCAST = {"broadcaster", "wire"}


def _jsonl(path: Path):
    if not path.exists():
        return
    with path.open() as fh:
        buf = ""
        for chunk in iter(lambda: fh.read(1 << 20), ""):
            buf += chunk
            *lines, buf = buf.split("\n")
            for line in lines:
                if line.strip():
                    yield json.loads(line)
        if buf.strip():
            yield json.loads(buf)


def _iso_numeric(code: str) -> str | None:
    c = None
    if len(code) == 2:
        c = pycountry.countries.get(alpha_2=code.upper())
    elif len(code) == 3:
        c = pycountry.countries.get(alpha_3=code.upper())
    return c.numeric if c else None


def _country_name(alpha2: str) -> str:
    c = pycountry.countries.get(alpha_2=alpha2.upper())
    return getattr(c, "common_name", None) or (c.name if c else alpha2)


# ---- news -------------------------------------------------------------------------------
def _news() -> dict:
    roles = {}
    oc = ROOT / "registry/outlets.csv"
    if oc.exists():
        for r in csv.DictReader(oc.open()):
            roles[r["domain"]] = r["role"]
    by_country: dict[str, list] = defaultdict(list)
    sc = ROOT / "registry/screening.csv"
    if sc.exists():
        for r in csv.DictReader(sc.open()):
            by_country[r["country"]].append({
                "name": r["name"], "domain": r["domain"], "type": r["outlet_type"],
                "class": r["collectability"], "verdict": r["verdict"],
                "year": int(r["archive_year"]) if r["archive_year"] else None,
                "role": roles.get(r["domain"], ""), "source": r["source"],
            })
    countries = {}
    for cc, outlets in by_country.items():
        def has(kinds, classes):
            return any(o["class"] in classes and o["type"] in kinds for o in outlets)
        past = "A" if has(PRESS, USABLE) and has(BCAST, USABLE) else "B" if any(o["class"] in USABLE for o in outlets) \
            else "C" if any(o["class"] == "route_recent" for o in outlets) else "D"
        recent_usable = USABLE | {"route_recent"}
        recent = "A" if has(PRESS, recent_usable) and has(BCAST, recent_usable) else \
            "B" if any(o["class"] in recent_usable for o in outlets) else "D"
        countries[cc] = {
            "name": _country_name(cc) if cc != "INT" else "International",
            "numeric": _iso_numeric(cc) if cc != "INT" else None,
            "tier_past": past, "tier_recent": recent,
            "pilot": any(o["source"] == "profile" for o in outlets),
            "outlets": sorted(outlets, key=lambda o: (o["role"] == "", o["class"] in ("blocked", "no_route"), o["name"])),
        }
    classes = Counter(o["class"] for v in by_country.values() for o in v)
    return {"countries": countries, "class_counts": classes, "outlets": sum(classes.values())}


# ---- humanitarian ------------------------------------------------------------------------
def _fetch_progress(source: str) -> dict:
    d = RAW / source
    selected = list(_jsonl(d / "selected.jsonl"))
    last = {}
    for rec in _jsonl(d / "files_index.jsonl"):
        last[rec["doc_id"]] = rec
    ok = [r for r in last.values() if r.get("ok")]
    files = sum(1 for r in ok for f in (r.get("files") or []) if f.get("ok"))
    byt = sum(r.get("bytes", 0) + sum(f.get("bytes", 0) for f in (r.get("files") or []) if f.get("ok")) for r in ok)
    errors = Counter((r.get("error") or "?").split(":")[0][:40] for r in last.values() if not r.get("ok"))
    times = [r.get("fetched_at") for r in last.values() if r.get("fetched_at")]
    return {
        "selected": len(selected), "audit_sample": sum(1 for s in selected if s.get("audit")),
        "attempted": len(last), "ok": len(ok), "failed": len(last) - len(ok), "attached_files": files,
        "bytes": byt, "errors": errors.most_common(5), "last_fetch": max(times) if times else None,
        "_selected": selected, "_ok_ids": {r["doc_id"] for r in ok},
    }


def _humanitarian() -> dict:
    per_country: dict[str, Counter] = defaultdict(Counter)

    # IFRC GO
    ifrc_meta = list(_jsonl(RAW / "ifrc_go/appeal_documents.jsonl"))
    for r in ifrc_meta:
        if r.get("iso"):
            per_country[r["iso"].upper()]["ifrc_docs"] += 1
    ifrc = _fetch_progress("ifrc_go")
    for s in ifrc["_selected"]:
        iso = ((s.get("meta") or {}).get("iso") or "").upper()
        if iso:
            per_country[iso]["ifrc_selected"] += 1
            if s["doc_id"] in ifrc["_ok_ids"]:
                per_country[iso]["ifrc_fetched"] += 1

    # ReliefWeb (월별 파일)
    rw_dir = RAW / "reliefweb/reports"
    months = sorted(p.stem for p in rw_dir.glob("*.jsonl")) if rw_dir.exists() else []
    rw_total, rw_formats = 0, Counter()
    for p in (rw_dir.glob("*.jsonl") if rw_dir.exists() else []):
        for r in _jsonl(p):
            rw_total += 1
            for f in r.get("format") or []:
                rw_formats[f.get("name")] += 1
            pc = (r.get("primary_country") or {}).get("iso3")
            if pc:
                c = pycountry.countries.get(alpha_3=pc.upper())
                if c:
                    per_country[c.alpha_2]["reliefweb_reports"] += 1
    today = date.today()
    first = date(1990, 1, 1)
    months_expected = (today.year - first.year) * 12 + today.month
    calls_file = STATE / f"reliefweb_calls_{today.isoformat()}.txt"
    calls_today = int(calls_file.read_text()) if calls_file.exists() else 0

    # Shelter Cluster
    sc_docs = list(_jsonl(RAW / "shelter_cluster/documents.jsonl"))
    sc = _fetch_progress("shelter_cluster")
    groups = Counter(d["group"] for d in sc_docs)

    def strip(p):
        return {k: v for k, v in p.items() if not k.startswith("_")}

    sources = [
        {"id": "ifrc_go", "name": "IFRC GO", "access": "Public API", "metadata": len(ifrc_meta),
         "metadata_unit": "appeal documents", **strip(ifrc)},
        {"id": "shelter_cluster", "name": "Global Shelter Cluster", "access": "Sitemap", "metadata": len(sc_docs),
         "metadata_unit": f"documents in {len(groups)} response groups", **strip(sc),
         "top_groups": groups.most_common(12)},
        {"id": "reliefweb", "name": "ReliefWeb", "access": "Approved API (robots exception)", "metadata": rw_total,
         "metadata_unit": "reports, Shelter and NFI theme", "months_done": len(months),
         "months_expected": months_expected, "last_month": months[-1] if months else None,
         "calls_today": calls_today, "daily_cap": 900, "formats": rw_formats.most_common(8),
         "stage": "metadata listing" if len(months) < months_expected else "metadata done; bodies next"},
        {"id": "shelter_projects", "name": "Shelter Projects", "access": "Manual download (site unreachable from server)",
         "metadata": len(list((RAW / "manual/shelter_projects").glob("*.pdf"))) if (RAW / "manual/shelter_projects").exists() else 0,
         "metadata_unit": "case-study books downloaded"},
    ]
    countries = {}
    for a2, c in per_country.items():
        num = _iso_numeric(a2)
        if num:
            countries[a2] = {"numeric": num, "name": _country_name(a2), **c,
                             "total": c["ifrc_docs"] + c["reliefweb_reports"]}
    return {"sources": sources, "countries": countries}


# ---- system ------------------------------------------------------------------------------
def _jobs() -> list[dict]:
    out = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            args = (p / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        a = [x.decode(errors="replace") for x in args if x]
        # uv run 래퍼 프로세스는 빼고 실제 파이썬 프로세스만 센다
        if a and Path(a[0]).name.startswith("python") and "-m" in a:
            i = a.index("-m")
            mod = a[i + 1] if i + 1 < len(a) else ""
            if mod.startswith("collectors.") and mod != "collectors.http":
                try:
                    started = (p / "stat").stat().st_mtime
                except OSError:
                    started = None
                out.append({"pid": int(p.name), "job": " ".join([mod.split(".", 1)[1], *a[i + 2:i + 4]]),
                            "since": datetime.fromtimestamp(started, timezone.utc).isoformat() if started else None})
    return sorted(out, key=lambda j: j["pid"])


def _system() -> dict:
    du = shutil.disk_usage(RAW if RAW.exists() else ROOT)
    size = 0
    if RAW.exists():
        for dirpath, _, files in os.walk(RAW):
            for f in files:
                try:
                    size += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    return {"disk_free_gb": round(du.free / 1e9, 1), "disk_total_gb": round(du.total / 1e9, 1),
            "raw_gb": round(size / 1e9, 3), "min_free_gb": float(os.environ.get("MIN_FREE_GB", 100)),
            "jobs": _jobs()}


# ---- server ------------------------------------------------------------------------------
_cache: dict = {"at": 0.0, "data": None}
_lock = threading.Lock()


def status() -> dict:
    with _lock:
        if _cache["data"] is None or time.time() - _cache["at"] > CACHE_SEC:
            t0 = time.time()
            data = {"generated_at": datetime.now(timezone.utc).isoformat(), "news": _news(),
                    "humanitarian": _humanitarian(), "system": _system()}
            data["build_sec"] = round(time.time() - t0, 2)
            _cache.update(at=time.time(), data=data)
        return _cache["data"]


class Handler(BaseHTTPRequestHandler):
    server_version = "HousingDashboard"

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            try:
                body = json.dumps(status(), ensure_ascii=False, default=list).encode()
                return self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                return self._send(500, json.dumps({"error": type(e).__name__}).encode(), "application/json")
        if path == "/":
            path = "/static/index.html"
        if path.startswith("/static/"):
            target = (STATIC / path[len("/static/"):]).resolve()
            if STATIC in target.parents and target.is_file():
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                    ctype += "; charset=utf-8"
                return self._send(200, target.read_bytes(), ctype)
        self._send(404, b"not found", "text/plain")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"dashboard on http://{a.host}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
