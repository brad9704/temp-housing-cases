"""언론사 사이트맵 목록화 (기사 본문은 받지 않는다).

    uv run python -m collectors.news_sitemap list <outlet_id|all> [--dry-run]
    uv run python -m collectors.news_sitemap summary [<outlet_id>]

사이트맵 색인부터 따라 내려가며, 하위 사이트맵의 기간(파일명 날짜 또는 색인 lastmod)이
기준 기간과 겹치는 것만 연다. 각 기사 항목에서 URL·날짜·제목(news:title이 있으면)을 뽑아
기간 안의 것만 {RAW_DIR}/news/<outlet_id>/urls.jsonl 로 저장한다.
설정: collectors/news_sitemap_plans.yaml. 모든 요청은 PoliteClient.fetch()를 거친다.
"""

from __future__ import annotations

import argparse
import calendar
import fcntl
import gzip
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

from collectors.http import FetchFailed, PoliteClient, RobotsDisallowed

PLANS = Path(__file__).with_name("news_sitemap_plans.yaml")
MAX_DEPTH = 4


def _raw(outlet: str) -> Path:
    d = Path(os.environ.get("RAW_DIR", "data/raw")) / "news" / outlet
    d.mkdir(parents=True, exist_ok=True)
    return d


def _code_state() -> dict:
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True).stdout.strip())
    return {"code_commit": head, "code_dirty": dirty}


# ---- dates -------------------------------------------------------------------------------
def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def span_from_url(url: str) -> tuple[date, date] | None:
    """하위 사이트맵 파일명에서 덮는 기간을 읽는다. 못 읽으면 None."""
    name = unquote(urlsplit(url).path) + "?" + (urlsplit(url).query or "")
    tail = name.rsplit("/", 3)
    s = "/".join(tail[-3:])
    m = re.search(r"(19|20)(\d{2})(0[1-9]|1[0-2])-((19|20)\d{2})(0[1-9]|1[0-2])", s)  # 201711-201712
    if m:
        y1, m1 = int(m.group(1) + m.group(2)), int(m.group(3))
        y2, m2 = int(m.group(4)), int(m.group(6))
        return date(y1, m1, 1), _month_end(y2, m2)
    m = re.search(r"((?:19|20)\d{2})[-_/]?(0[1-9]|1[0-2])[-_/]?(0[1-9]|[12]\d|3[01])(?!\d)", s)  # YYYYMMDD / YYYY-MM-DD
    if m:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return d, d
    m = re.search(r"((?:19|20)\d{2})[-_/.](1[0-2]|0?[1-9])(?!\d)", s)  # YYYY-MM, YYYY-M, YYYY/MM, .YYYY-
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return date(y, mo, 1), _month_end(y, mo)
    m = re.search(r"((?:19|20)\d{2})(0[1-9]|1[0-2])(?!\d)", s)  # YYYYMM
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return date(y, mo, 1), _month_end(y, mo)
    m = re.search(r"[_/-]((?:19|20)\d{2})(?:\.xml|/|$)", s)  # sitemap3_2010.xml, /2023/
    if m:
        y = int(m.group(1))
        return date(y, 1, 1), date(y, 12, 31)
    return None


def _parse_dt(s: str | None) -> date | None:
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s.strip())
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def date_from_article_url(url: str) -> date | None:
    m = re.search(r"((?:19|20)\d{2})[/-]?(0[1-9]|1[0-2])[/-]?(0[1-9]|[12]\d|3[01])(?!\d)", urlsplit(url).path)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, min(d.day, 28))


# ---- sitemap parsing ---------------------------------------------------------------------
def parse(b: bytes) -> tuple[str, list[dict]]:
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    root = ET.fromstring(b)
    kind = root.tag.split("}")[-1]
    out = []
    for el in root:
        e = {"loc": None, "lastmod": None, "pub": None, "title": None}
        for ch in el.iter():
            t = ch.tag.split("}")[-1]
            txt = (ch.text or "").strip()
            if t == "loc" and e["loc"] is None:
                e["loc"] = txt
            elif t == "lastmod":
                e["lastmod"] = txt
            elif t == "publication_date":
                e["pub"] = txt
            elif t == "title" and e["title"] is None:
                e["title"] = txt
        if e["loc"]:
            out.append(e)
    return kind, out


_BLOCK = re.compile(rb"<(url|sitemap)>(.*?)</\1>", re.S)


def _tag(block: bytes, name: str) -> str | None:
    m = re.search(rb"<(?:\w+:)?" + name.encode() + rb">\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</(?:\w+:)?" + name.encode() + rb">", block, re.S)
    return m.group(1).decode("utf-8", "replace").strip() if m else None


def parse_lenient(b: bytes) -> tuple[str, list[dict]]:
    """XML이 깨진 사이트맵용(예: 이미지 캡션에 이스케이프 안 된 HTML). <url>/<sitemap> 블록에서 필요한 태그만 뽑는다."""
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    out, kind = [], "urlset"
    for m in _BLOCK.finditer(b):
        if m.group(1) == b"sitemap":
            kind = "sitemapindex"
        loc = _tag(m.group(2), "loc")
        if loc:
            out.append({"loc": loc, "lastmod": _tag(m.group(2), "lastmod"),
                        "pub": _tag(m.group(2), "publication_date"), "title": _tag(m.group(2), "title")})
    return kind, out


class Lister:
    def __init__(self, outlet: str, plan: dict, window: tuple[date, date]):
        self.outlet, self.plan = outlet, plan
        self.start, self.end = window
        self.client = PoliteClient()
        self.requests = 0
        self.errors: list[str] = []
        self.inc = re.compile(plan["include"]) if plan.get("include") else None
        self.exc = re.compile(plan["exclude"]) if plan.get("exclude") else None

    def _get(self, url: str) -> bytes | None:
        if self.plan.get("https_only") and url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        self.requests += 1
        try:
            r = self.client.fetch(url)
        except (FetchFailed, RobotsDisallowed) as e:
            self.errors.append(f"{type(e).__name__}: {url}")
            return None
        if r.status != 200:
            self.errors.append(f"HTTP {r.status}: {url}")
            return None
        return r.content

    def _child_in_window(self, child: dict) -> bool:
        if self.inc and not self.inc.search(child["loc"]):
            return False
        if self.exc and self.exc.search(child["loc"]):
            return False
        end = self.end
        if self.plan.get("lastmod_keyed"):
            end = _add_months(self.end, self.plan.get("margin_months", 2))
        if self.plan.get("child_dates") == "lastmod":
            return True  # 순번 사이트맵은 아래 walk에서 lastmod 순서로 자른다
        if self.plan.get("child_dates") == "numbered":
            m = re.search(r"(\d+)\.xml", child["loc"])
            lo, hi = self.plan["numbered_range"]
            return bool(m) and lo <= int(m.group(1)) <= hi
        span = span_from_url(child["loc"])
        if span is None:
            return True  # 날짜 없는 중간 색인(섹션 색인 등)은 내려가 본다
        return span[1] >= self.start and span[0] <= end

    def walk(self, dry_run: bool = False):
        stack = [(self.plan["index"], 0)]
        seen = set()
        opened = 0
        while stack:
            url, depth = stack.pop()
            if url in seen or depth > MAX_DEPTH:
                continue
            seen.add(url)
            b = self._get(url)
            if b is None:
                continue
            try:
                kind, ents = parse(b)
            except ET.ParseError as e:
                kind, ents = parse_lenient(b)
                self.errors.append(f"ParseError {e}: {url} (lenient parse salvaged {len(ents)} entries)")
                if not ents:
                    continue
            if kind == "sitemapindex":
                kids = [c for c in ents if self._child_in_window(c)]
                if self.plan.get("child_dates") == "lastmod":
                    kids = self._lastmod_slice(ents)
                if dry_run and depth == 0:
                    yield {"_dry": [k["loc"] for k in kids]}
                    return
                stack.extend((k["loc"], depth + 1) for k in reversed(kids))
            else:
                opened += 1
                for e in ents:
                    yield e

    def _lastmod_slice(self, ents: list[dict]) -> list[dict]:
        """순번 사이트맵: lastmod(=마지막 글 날짜)가 기간 시작 이후인 첫 파일부터, 기간 끝을 넘긴 첫 파일까지."""
        kids = [c for c in ents if (not self.inc or self.inc.search(c["loc"]))]
        kids.sort(key=lambda c: c.get("lastmod") or "")
        out = []
        for c in kids:
            lm = _parse_dt(c.get("lastmod"))
            if lm is None or lm < self.start:
                continue
            out.append(c)
            if lm > self.end:
                break
        return out


def cmd_list(a) -> None:
    cfg = yaml.safe_load(PLANS.read_text())
    targets = list(cfg["outlets"]) if a.outlet == "all" else [a.outlet]
    for oid in targets:
        plan = cfg["outlets"][oid]
        w = [date.fromisoformat(str(x)) for x in cfg["windows"][plan["country"]]]
        lister = Lister(oid, plan, (w[0], w[1]))
        if a.dry_run:
            for e in lister.walk(dry_run=True):
                kids = e["_dry"]
                print(f"{oid}: {len(kids)} child sitemaps selected at top level; first {kids[:2]} last {kids[-2:]}")
            continue
        meta = {"outlet_id": oid, "window": [str(w[0]), str(w[1])], **_code_state(),
                "listed_at": datetime.now(timezone.utc).isoformat()}
        out = _raw(oid) / "urls.jsonl"
        tmp = out.with_suffix(".part")
        # 같은 언론사 목록화를 두 프로세스가 동시에 돌리면 같은 .part를 함께 쓴다(2026-10-21 발생). 잠금으로 막는다
        lock = (_raw(oid) / "list.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"{oid}: 다른 프로세스가 목록화 중이라 건너뜁니다", flush=True)
            continue
        kept = dropped = undated = 0
        seen = set()
        with tmp.open("w") as fh:
            for e in lister.walk():
                if e["loc"] in seen:
                    continue
                seen.add(e["loc"])
                d, src = _parse_dt(e["pub"]), "publication_date"
                if d is None:
                    d, src = date_from_article_url(e["loc"]), "url"
                if d is None:
                    d, src = _parse_dt(e["lastmod"]), "lastmod"
                if d is None:
                    undated += 1
                elif not (w[0] <= d <= w[1]):
                    dropped += 1
                    continue
                kept += 1
                fh.write(json.dumps({"url": e["loc"], "date": d.isoformat() if d else None, "date_source": src if d else None,
                                     "title": e["title"], "lastmod": e["lastmod"], "_meta": meta}, ensure_ascii=False) + "\n")
        tmp.replace(out)
        rep = {"outlet": oid, "kept": kept, "outside_window": dropped, "undated_kept": undated,
               "sitemap_requests": lister.requests, "errors": len(lister.errors), "error_samples": lister.errors[:3]}
        (_raw(oid) / "list_report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1))
        print(json.dumps(rep, ensure_ascii=False), flush=True)
        lock.close()


def cmd_summary(a) -> None:
    base = Path(os.environ.get("RAW_DIR", "data/raw")) / "news"
    for d in sorted(base.iterdir()) if base.exists() else []:
        if a.outlet and d.name != a.outlet:
            continue
        p = d / "urls.jsonl"
        if not p.exists():
            continue
        rows = [json.loads(l) for l in p.read_text().split("\n") if l.strip()]
        titles = sum(1 for r in rows if r.get("title"))
        years = Counter((r.get("date") or "????")[:4] for r in rows)
        print(f"{d.name:20} urls {len(rows):>8}  with title {titles:>8}  years {dict(sorted(years.items()))}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="news_sitemap")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("outlet")
    p.add_argument("--dry-run", action="store_true", help="최상위 색인에서 고른 하위 사이트맵만 보여 준다 (요청 1~2회)")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("summary")
    p.add_argument("outlet", nargs="?")
    p.set_defaults(fn=cmd_summary)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
