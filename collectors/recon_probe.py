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
import csv
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
        "robots_error": st.robots_error or None,
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


def _parse_sitemap(b: bytes) -> tuple[str, list[dict]]:
    root = ET.fromstring(b)
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
    return tag, entries


def cmd_sitemap(a) -> None:
    r = _get(a.url)
    if "_content" not in r or r.get("status") != 200:
        _out(_strip(r))
        return
    try:
        tag, entries = _parse_sitemap(_sitemap_bytes(r))
    except ET.ParseError as e:
        r["parse_error"] = str(e)
        _out(_strip(r))
        return
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


# ---- screen: 언론사 1곳 경량 선별 (요청 상한 있음) ---------------------------------
# 명시적 TDM·AI 이용 유보: 특정 봇이 아니라 이용 목적 자체를 금지하는 문구
_TDM_EXPLICIT_RE = re.compile(
    r"text\s*(and|&)\s*data[- ]?mining|\bTDM\b|data[- ]?min(e|ing)|§\s*4[24]\s*[bh]|\bDSM\b|Nutzungsvorbehalt|"
    r"reservation of rights|reserves? the right[^.\n]{0,80}(mining|AI|machine)|"
    r"not (be )?(used|permitted)[^\n]{0,120}(LLM|large language|machine learning|artificial intelligence|\bAI\b)|"
    r"(AI|artificial intelligence)[- ]?(training|use)[^\n]{0,80}(not permitted|prohibited|forbidden|no autorizad|interdit|vietat|verboten)|"
    r"no autorizad|no scraping|systematic extraction|scrap(e|ing)[^\n]{0,80}(prohibited|not permitted|forbidden|prohibido|interdit|vietato|dilarang)",
    re.I)
_AI_BOT_RE = re.compile(r"^\s*user-agent:\s*(GPTBot|ChatGPT-User|OAI-SearchBot|ClaudeBot|Claude-Web|anthropic-ai|Claude-User|Claude-SearchBot|"
                        r"Google-Extended|CCBot|cohere-ai|PerplexityBot|Bytespider|Applebot-Extended|meta-externalagent|MistralAI|Amazonbot)",
                        re.I | re.M)
_TDM_RE = _TDM_EXPLICIT_RE
_CHALLENGE_RE = re.compile(r"cf-chl|challenge-platform|Just a moment|Attention Required|captcha|px-captcha|_Incapsula_|datadome", re.I)
_PAYWALL_RE = re.compile(r"paywall|piano\.io|tinypass|poool|subscriber[- ]only|premium[- ]content|metered|会員限定|유료\s*회원|abbonati|suscríbete|abonnez-vous|abone\s*ol", re.I)
_YEAR_RE = re.compile(r"(?<!\d)(199\d|20[0-3]\d)(?!\d)")


def parse_content_signals(robots_text: str) -> dict[str, str]:
    """`*` 그룹(또는 그룹 밖)의 Content-Signal 값. 여러 줄이면 뒤 값이 덮어쓴다."""
    out: dict[str, str] = {}
    agents: list[str] = []
    in_rules = False
    for raw in robots_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(v.lower())
            continue
        in_rules = True
        if k == "content-signal" and (not agents or "*" in agents):
            for part in v.split(","):
                if "=" in part:
                    sk, sv = (x.strip().lower() for x in part.split("=", 1))
                    out[sk] = sv
    return out


def _years(e: dict) -> list[int]:
    ys = [int(y) for y in _YEAR_RE.findall(e["loc"])]
    for k in ("lastmod", "publication_date"):
        if e.get(k):
            m = _YEAR_RE.match(e[k])
            if m:
                ys.append(int(m.group(1)))
    return ys


def _trailing_num(loc: str) -> int:
    m = re.search(r"(\d+)(?:\.xml)?(?:\.gz)?$", loc)
    return int(m.group(1)) if m else -1


def _looks_like_article(loc: str) -> bool:
    """홈·섹션·태그·이미지가 아닌 기사 URL로 보이는가 (휴리스틱)."""
    p = urlsplit(loc)
    path = p.path.rstrip("/")
    if re.search(r"\.(jpg|jpeg|png|gif|xml|gz|pdf)$|/(tag|tags|category|categories|author|authors|video|videos|section|topic|topics)(/|$)", path, re.I):
        return False
    segs = [x for x in path.split("/") if x]
    if not segs and not p.query:
        return False
    return bool(re.search(r"\d{3,}", loc)) or len(segs) >= 2 or len(segs[-1] if segs else "") > 25


def cmd_screen(a) -> None:
    base = a.url if urlsplit(a.url).scheme else f"https://{a.url}"
    c = client()
    budget = {"n": 0}
    row: dict = {
        "domain": urlsplit(base).netloc, "country": a.country, "name": a.name,
        "outlet_type": a.outlet_type, "languages": a.lang, "screened_at": datetime.now().date().isoformat(),
        "evidence_urls": [], "notes": [],
    }

    def get(url):
        if budget["n"] >= a.max_requests:
            row["notes"].append("request budget exhausted")
            return None
        budget["n"] += 1
        r = _get(url)
        row["evidence_urls"].append(url)
        if r.get("stop_domain"):
            row["stop_domain"] = True
        return r

    def finish(verdict):
        if row.get("stop_domain") and not verdict.startswith("blocked"):
            row["notes"].append(f"stop_domain after partial screen (would be {verdict})")
            verdict = "blocked_http"
        row["verdict"] = verdict
        row["requests"] = budget["n"]
        line = json.dumps(row, ensure_ascii=False)
        if a.out:
            with open(a.out, "a") as fh:
                fh.write(line + "\n")
        print(line)

    # robots
    mode = c.robots_status(base)
    st = c._state(c._origin(base))
    budget["n"] += 1
    row["evidence_urls"].append(f"{c._origin(base)}/robots.txt")
    row["robots_mode"] = mode
    row["robots_error"] = st.robots_error or None
    text = st.robots_raw or ""
    m = _TDM_EXPLICIT_RE.search(text)
    row["tdm_reservation"] = bool(m)  # 명시적 유보만
    row["tdm_snippet"] = text[max(0, m.start() - 80): m.end() + 80].strip() if m else None
    row["ai_bots_named"] = sorted({x.lower() for x in _AI_BOT_RE.findall(text)})
    # Cloudflare Content Signals (https://contentsignals.org). 기록만 한다.
    # 2026-10-21 결정: 학습 목적 수집이 아니므로 ai-train·ai-input 신호는 제외 사유로 쓰지 않는다
    row["content_signals"] = parse_content_signals(text) or None
    row["robots_allows_home"] = c.allowed(base + "/")
    if mode == "deny_all":
        return finish("blocked_robots_unreachable" if st.robots_error else "blocked_robots")
    cd = st.robots.crawl_delay(c.cfg.bot_token) if st.robots else None
    row["crawl_delay"] = float(cd) if cd else None
    if cd and float(cd) > a.max_crawl_delay:
        # 기다리지 않는다. 수집 시에는 이 값을 따라야 하므로 사실상 수집 불가로 기록
        return finish("crawl_delay_excessive")
    if not row["robots_allows_home"]:
        row["notes"].append("robots disallows / for our bot")

    # homepage
    home = get(base + "/")
    if home is None or "_content" not in home:
        row["home_status"] = None
        row["notes"].append(f"home: {home and home.get('error')}")
    else:
        html = home["_content"][:400000].decode("utf-8", errors="replace")
        row["home_status"] = home["status"]
        row["home_final_url"] = home["final_url"]
        row["challenge"] = bool(_CHALLENGE_RE.search(html)) and home["status"] in (403, 429, 503, 200) and len(html) < 60000
        row["paywall_hint"] = (_PAYWALL_RE.search(html) or [None])[0]
        if home["status"] in BLOCK_STATUSES + (402, 406) or (row["challenge"] and home["status"] != 200):
            return finish("blocked_http")
    if row.get("stop_domain"):
        return finish("blocked_http")

    # sitemaps
    cands = list(st.robots.sitemaps) if st.robots else []
    row["robots_sitemaps"] = cands[:10]
    if not cands:
        cands = [base + "/sitemap.xml", base + "/sitemap_index.xml"]
    children, urls, kinds = [], [], []
    for sm in cands[:3]:
        r = get(sm)
        if not r or r.get("status") != 200 or "_content" not in r:
            continue
        try:
            kind, ents = _parse_sitemap(_sitemap_bytes(r))
        except Exception:
            continue
        kinds.append(kind)
        (children if kind == "sitemapindex" else urls).extend(ents)
        if len(children) > 50:
            break
    row["sitemap_found"] = bool(children or urls)
    row["sitemap_children"] = len(children)
    this_year = datetime.now().year
    allys = [y for e in children + urls for y in _years(e) if 1995 <= y <= this_year]
    row["sitemap_year_min"] = min(allys) if allys else None
    row["sitemap_year_max"] = max(allys) if allys else None

    # 표본 기사: 최신 1건 + 가장 오래된 하위 사이트맵 1건
    def pick_from(child):
        r = get(child["loc"])
        if not r or r.get("status") != 200 or "_content" not in r:
            return None
        try:
            kind, ents = _parse_sitemap(_sitemap_bytes(r))
        except Exception:
            return None
        if kind == "sitemapindex" and ents:
            return pick_from(ents[0])
        ents = [e for e in ents if _looks_like_article(e["loc"])]
        return ents[0]["loc"] if ents else None

    def sample(url, key):
        if not url:
            return
        r = get(url)
        if r and r.get("status") == 200 and "_content" in r:
            e = _extract(r["_content"], r["final_url"])
            row[key] = {"url": url, "date": e.get("date"), "text_chars": e.get("text_chars"), "title": (e.get("title") or "")[:120]}
        else:
            row[key] = {"url": url, "status": r and r.get("status"), "error": r and r.get("error")}

    newest_url = oldest_url = None
    if urls:
        arts = [e for e in urls if _looks_like_article(e["loc"])]
        newest_url = arts[0]["loc"] if arts else None
    if children:
        dated = [(min(_years(ch)) if _years(ch) else None, _trailing_num(ch["loc"]), ch) for ch in children]
        if any(d[0] for d in dated):
            dated = [d for d in dated if d[0]]
            oldest = min(dated, key=lambda d: (d[0], d[1]))[2]
            newest = max(dated, key=lambda d: (d[0], d[1]))[2]
        else:
            oldest = min(dated, key=lambda d: d[1])[2]
            newest = max(dated, key=lambda d: d[1])[2]
        newest_url = newest_url or pick_from(newest)
        if oldest is not newest:
            oldest_url = pick_from(oldest)
    sample(newest_url, "sample_newest")
    sample(oldest_url, "sample_oldest")

    od = (row.get("sample_oldest") or {}).get("date") or ""
    row["archive_year_verified"] = int(od[:4]) if od[:4].isdigit() else None
    nw = row.get("sample_newest") or {}
    row["extract_ok_newest"] = bool((nw.get("text_chars") or 0) >= 300 and nw.get("date"))

    if row["tdm_reservation"]:
        return finish("tdm_reserved")
    if not row["sitemap_found"]:
        return finish("no_sitemap")
    # 깊이는 사이트맵 연도 기준. 표본 기사 날짜가 사이트맵 최소 연도와 2년 이내면 검증된 것으로 표시
    sm_min, ver = row["sitemap_year_min"], row["archive_year_verified"]
    row["depth_verified"] = bool(sm_min and ver and abs(ver - sm_min) <= 2)
    depth = sm_min or ver
    if depth is None:
        return finish("sitemap_undated")
    return finish("sitemap_deep" if depth <= 2015 else "sitemap_mid" if depth <= 2020 else "sitemap_recent_only")


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


def cmd_screen_batch(a) -> None:
    """후보 CSV(domain,country,name,outlet_type,languages)를 도메인 간 병렬로 선별.

    같은 도메인은 PoliteClient가 직렬화하므로 워커 수와 무관하게 도메인당 동시 요청은 1건이다.
    이미 --out에 있는 도메인은 건너뛴다(--force로 재선별).
    """
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    rows = list(csv.DictReader(open(a.candidates)))
    done = set()
    if Path(a.out).exists() and not a.force:
        done = {json.loads(l)["domain"] for l in Path(a.out).read_text().split("\n") if l.strip()}
    todo, seen = [], set()
    for r in rows:
        d = urlsplit(r["domain"] if "//" in r["domain"] else "https://" + r["domain"]).netloc
        if d in done or d in seen:
            continue
        seen.add(d)
        todo.append((d, r))
    print(f"{len(todo)} to screen ({len(done)} already in {a.out})", file=sys.stderr)

    def run(item):
        d, r = item
        cmd = [sys.executable, "-m", "collectors.recon_probe", "screen", f"https://{d}",
               "--country", r["country"], "--name", r["name"], "--outlet-type", r["outlet_type"],
               "--lang", r.get("languages") or "", "--max-requests", str(a.max_requests), "--out", a.out]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout)
        if p.returncode != 0:
            err = {"domain": d, "country": r["country"], "name": r["name"], "outlet_type": r["outlet_type"],
                   "languages": r.get("languages") or "", "screened_at": datetime.now().date().isoformat(),
                   "verdict": "screen_error", "notes": [p.stderr.strip().splitlines()[-1][:300] if p.stderr.strip() else "error"]}
            with open(a.out, "a") as fh:
                fh.write(json.dumps(err, ensure_ascii=False) + "\n")
        return d, p.returncode

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, (d, rc) in enumerate(ex.map(run, todo), 1):
            if i % 20 == 0 or rc:
                print(f"[{i}/{len(todo)}] {d} rc={rc}", file=sys.stderr)


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

    p = sub.add_parser("screen", help="언론사 1곳 경량 선별 (요청 상한 --max-requests)")
    p.add_argument("url")
    p.add_argument("--country", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--outlet-type", required=True,
                   choices=["wire", "national_daily", "regional_daily", "broadcaster", "online"])
    p.add_argument("--lang", required=True, help="쉼표 구분 ISO 639-1")
    p.add_argument("--max-requests", type=int, default=10)
    p.add_argument("--max-crawl-delay", type=float, default=60.0)
    p.add_argument("--out", help="결과 JSONL을 이 파일에 덧붙임")
    p.set_defaults(fn=cmd_screen)

    p = sub.add_parser("screen-batch", help="후보 CSV 일괄 선별 (도메인 간 병렬)")
    p.add_argument("candidates")
    p.add_argument("--out", required=True)
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--max-requests", type=int, default=10)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_screen_batch)

    p = sub.add_parser("validate")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_validate)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
