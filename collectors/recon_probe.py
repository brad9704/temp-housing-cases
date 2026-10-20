"""recon 단계 조사 도구. 언론사 프로파일 작성에 필요한 증거를 모은다.

모든 요청은 `PoliteClient.fetch()`를 거친다. 원문은 저장하지 않고 요약만 출력한다.

    uv run python -m collectors.recon_probe robots https://www.example.com [--path /news/...]
    uv run python -m collectors.recon_probe fetch URL [--links PATTERN]
    uv run python -m collectors.recon_probe sitemap URL [--limit 50] [--grep 2017]
    uv run python -m collectors.recon_probe extract URLS_FILE [--min-chars 300]
    uv run python -m collectors.recon_probe wayback URL_PREFIX --from 20171115 --to 20181115 [--limit 50]
    uv run python -m collectors.recon_probe validate registry/outlet_profiles/*.json

출력은 JSON 한 덩어리. 403/429가 연속되면 `stop_domain: true`가 붙는다. 그때는 해당 도메인 조사를 멈춘다.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urljoin, urlsplit

import trafilatura
from pydantic import BaseModel, Field, field_validator

from collectors.http import FetchFailed, PoliteClient, RobotsDisallowed

BLOCK_STATUSES = (401, 403, 429)


# ---- 프로파일 스키마 (CLAUDE.md "언론사 프로파일 스키마") --------------------------
class OutletProfile(BaseModel):
    outlet_id: str = Field(pattern=r"^[a-z]{2}_[a-z0-9_]+$")
    country: str = Field(pattern=r"^[A-Z]{2}$")
    name: str = Field(min_length=1)
    domain: str = Field(min_length=3)
    languages: list[str] = Field(min_length=1)
    outlet_type: Literal["wire", "national_daily", "regional_daily", "broadcaster", "online"]
    access_path: Literal["api", "licensed_db", "sitemap", "archive_page", "wayback", "none"]
    access_notes: str
    robots_allows_articles: bool | None
    paywall: Literal["none", "metered", "hard"]
    js_rendering_required: bool
    archive_start_year: int | None
    est_articles_per_day: float | None
    url_pattern: str
    url_has_date: bool
    url_has_section: bool
    section_taxonomy: list[str]
    date_format_examples: list[str]
    byline_wire_markers: list[str]
    extraction_sample_n: int
    extraction_success_rate: float | None = Field(default=None, ge=0, le=1)
    needs_custom_parser: bool
    correction_handling: str
    evidence_urls: list[str] = Field(min_length=1)
    profiled_at: str

    # 레지스트리 운용 필드 (스키마 확장, 선택)
    role: Literal["core_daily", "core_broadcaster", "regional_probe", "candidate_rejected"] | None = None
    baseline_event_coverage: str | None = None

    @field_validator("evidence_urls")
    @classmethod
    def _urls(cls, v: list[str]) -> list[str]:
        bad = [u for u in v if not u.startswith(("http://", "https://"))]
        if bad:
            raise ValueError(f"evidence_urls에 URL이 아닌 값: {bad}")
        return v

    @field_validator("profiled_at")
    @classmethod
    def _date(cls, v: str) -> str:
        datetime.fromisoformat(v)
        return v


# ---- helpers ------------------------------------------------------------------
_client: PoliteClient | None = None
_block_streak: dict[str, int] = {}


def client() -> PoliteClient:
    global _client
    if _client is None:
        _client = PoliteClient()
    return _client


def _get(url: str) -> dict:
    """fetch 1건. 예외를 결과 dict로 바꾸고 차단 연속 횟수를 센다."""
    host = urlsplit(url).netloc
    try:
        r = client().fetch(url)
    except RobotsDisallowed:
        return {"url": url, "error": "robots_disallowed"}
    except FetchFailed as e:
        status = 429 if " 429 " in f" {e} " else None
        out = {"url": url, "error": f"fetch_failed: {e}"}
        if status:
            _block_streak[host] = _block_streak.get(host, 0) + 1
            out["stop_domain"] = _block_streak[host] >= 2
        return out
    if r.status in BLOCK_STATUSES:
        _block_streak[host] = _block_streak.get(host, 0) + 1
    else:
        _block_streak[host] = 0
    out = {
        "url": url,
        "final_url": r.final_url,
        "status": r.status,
        "bytes": len(r.content),
        "content_type": r.headers.get("content-type") or r.headers.get("Content-Type"),
        "attempts": r.attempts,
        "_content": r.content,
    }
    if _block_streak.get(host, 0) >= 2:
        out["stop_domain"] = True
    return out


def _strip(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _extract(content: bytes, url: str) -> dict:
    doc = trafilatura.bare_extraction(content, url=url, with_metadata=True, favor_precision=True)
    if doc is None:
        return {"ok": False}
    d = doc.as_dict() if hasattr(doc, "as_dict") else dict(doc)
    text = d.get("text") or ""
    return {
        "title": d.get("title"),
        "date": d.get("date"),
        "author": d.get("author"),
        "sitename": d.get("sitename"),
        "categories": d.get("categories"),
        "text_chars": len(text),
        "text_head": text[:400],
        "text_tail": text[-200:] if len(text) > 400 else "",
    }


def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ---- commands -----------------------------------------------------------------
def cmd_robots(a) -> None:
    c = client()
    base = a.url if urlsplit(a.url).scheme else f"https://{a.url}"
    mode = c.robots_status(base)
    text = c.robots_text(base) or ""
    st = c._state(c._origin(base))
    res = {
        "origin": c._origin(base),
        "mode": mode,
        "crawl_delay_for_bot": st.robots.crawl_delay(c.cfg.bot_token) if st.robots else None,
        "sitemaps": list(st.robots.sitemaps) if st.robots else [],
        "can_fetch": {p: c.allowed(urljoin(base, p)) for p in (a.path or ["/"])},
        "robots_text": text[:4000],
    }
    _out(res)


def cmd_fetch(a) -> None:
    r = _get(a.url)
    if "_content" in r:
        if a.extract:
            r["extract"] = _extract(r["_content"], r["final_url"])
        html = r["_content"].decode("utf-8", errors="replace")
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        r["html_title"] = m.group(1).strip()[:200] if m else None
        r["meta"] = dict(
            re.findall(r'<meta[^>]+(?:property|name)=["\']((?:og:|article:)[^"\']+)["\'][^>]+content=["\']([^"\']*)', html)[:30]
        )
        r["noscript_or_spa_hint"] = bool(re.search(r'id=["\'](?:__next|root|app)["\']', html)) and len(html) < 20000
        if a.links:
            pat = re.compile(a.links)
            links = sorted({urljoin(r["final_url"], h) for h in re.findall(r'href=["\']([^"\']+)', html)})
            r["links"] = [u for u in links if pat.search(u)][: a.limit]
    _out(_strip(r))


def _sitemap_bytes(r: dict) -> bytes:
    b = r["_content"]
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    return b


def cmd_sitemap(a) -> None:
    r = _get(a.url)
    if "_content" not in r or r.get("status") != 200:
        _out(_strip(r))
        return
    try:
        root = ET.fromstring(_sitemap_bytes(r))
    except ET.ParseError as e:
        r["parse_error"] = str(e)
        _out(_strip(r))
        return
    tag = root.tag.split("}")[-1]
    entries = []
    for el in root:
        loc = lastmod = pub = None
        for ch in el.iter():
            t = ch.tag.split("}")[-1]
            if t == "loc" and loc is None:
                loc = (ch.text or "").strip()
            elif t == "lastmod":
                lastmod = (ch.text or "").strip()
            elif t == "publication_date":
                pub = (ch.text or "").strip()
        if loc:
            entries.append({"loc": loc, "lastmod": lastmod, "publication_date": pub})
    total = len(entries)
    if a.grep:
        g = re.compile(a.grep)
        entries = [e for e in entries if g.search(json.dumps(e))]
    r["kind"] = tag  # sitemapindex | urlset
    r["total_entries"] = total
    r["matched_entries"] = len(entries)
    r["first"] = entries[: a.limit]
    if len(entries) > a.limit:
        r["last"] = entries[-min(5, len(entries) - a.limit):]
    _out(_strip(r))


def cmd_extract(a) -> None:
    urls = [u.strip() for u in Path(a.urls_file).read_text().splitlines() if u.strip() and not u.startswith("#")]
    rows, ok = [], 0
    for u in urls:
        r = _get(u)
        row = {"url": u, "status": r.get("status"), "error": r.get("error")}
        if "_content" in r and r.get("status") == 200:
            e = _extract(r["_content"], r["final_url"])
            row.update({k: e.get(k) for k in ("title", "date", "text_chars")})
            row["success"] = bool(e.get("text_chars", 0) >= a.min_chars and e.get("date"))
            ok += row["success"]
        else:
            row["success"] = False
        rows.append(row)
        if r.get("stop_domain"):
            row["stop_domain"] = True
            break
    _out({
        "n": len(rows),
        "success": ok,
        "success_rate": round(ok / len(rows), 3) if rows else None,
        "criteria": f"status 200, trafilatura text >= {a.min_chars} chars, date extracted",
        "rows": rows,
    })


def cmd_wayback(a) -> None:
    q = {
        "url": a.url_prefix,
        "matchType": a.match,
        "from": getattr(a, "from"),
        "to": a.to,
        "output": "json",
        "filter": "statuscode:200",
        "collapse": "urlkey",
        "limit": a.limit,
        "fl": "timestamp,original,mimetype,statuscode",
    }
    url = "https://web.archive.org/cdx/search/cdx?" + urlencode({k: v for k, v in q.items() if v})
    r = _get(url)
    if "_content" in r and r.get("status") == 200:
        try:
            data = json.loads(r["_content"] or b"[]")
        except json.JSONDecodeError:
            data = []
        rows = [dict(zip(data[0], row)) for row in data[1:]] if data else []
        for row in rows:
            row["replay_raw"] = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
        r["n"] = len(rows)
        r["rows"] = rows
    _out(_strip(r))


def cmd_validate(a) -> None:
    bad = 0
    for p in a.files:
        try:
            prof = OutletProfile.model_validate_json(Path(p).read_text())
            if Path(p).stem != prof.outlet_id:
                raise ValueError(f"파일명({Path(p).stem})과 outlet_id({prof.outlet_id}) 불일치")
            print(f"OK   {p}")
        except Exception as e:
            bad += 1
            print(f"FAIL {p}: {e}")
    sys.exit(1 if bad else 0)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="recon_probe")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("robots")
    p.add_argument("url")
    p.add_argument("--path", action="append", help="can_fetch 확인할 경로 (여러 번 가능)")
    p.set_defaults(fn=cmd_robots)

    p = sub.add_parser("fetch")
    p.add_argument("url")
    p.add_argument("--no-extract", dest="extract", action="store_false")
    p.add_argument("--links", help="이 정규식에 맞는 링크 목록 출력")
    p.add_argument("--limit", type=int, default=60)
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("sitemap")
    p.add_argument("url")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--grep")
    p.set_defaults(fn=cmd_sitemap)

    p = sub.add_parser("extract")
    p.add_argument("urls_file")
    p.add_argument("--min-chars", type=int, default=300)
    p.set_defaults(fn=cmd_extract)

    p = sub.add_parser("wayback")
    p.add_argument("url_prefix")
    p.add_argument("--from", required=False)
    p.add_argument("--to", required=False)
    p.add_argument("--match", default="prefix", choices=["exact", "prefix", "host", "domain"])
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_wayback)

    p = sub.add_parser("validate")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_validate)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
