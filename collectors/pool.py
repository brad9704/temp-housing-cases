"""언론사 풀 집계: 상세 프로파일 + 경량 선별(screen) JSONL -> registry/screening.csv

    uv run python -m collectors.pool SCREEN_JSONL... [--out registry/screening.csv]

수집 가능성 등급 (collectability)
- route_deep    : 목록화 경로(사이트맵·목록 페이지) 확인, 2015년 이전 기사까지
- route_mid     : 목록화 경로 확인, 2016~2020년부터
- route_recent  : 사이트맵이 2021년 이후분만 (과거 경로 미확인)
- route_unknown : 접근은 되지만 사이트맵 없음·날짜 불명 (수동 확인 필요)
- wayback_only  : 상세 프로파일에서 Wayback으로만 목록화 가능 판정
- no_route      : 접근은 되지만 기준 기간 기사 목록화 경로 없음 (상세 프로파일 판정)
- blocked       : robots·HTTP 차단·연결 불가·TDM 유보·유료벽
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

_BLOCK_RE = re.compile(r"(robots[^.]{0,60}(disallow|block|den(y|ies)|only googlebot|prohibit))|disallow: /(\s|$|[).,;])|\b40[13]\b|\b429\b|cloudflare|bot[- ]challenge|TDM|data[- ]mining|stream reset|unreachable|certificate|会員限定|member wall|hard paywall", re.I)

COLUMNS = [
    "domain", "country", "name", "outlet_type", "languages", "source", "verdict", "collectability",
    "archive_year", "tdm_reservation", "paywall_hint", "extract_ok_newest", "screened_at", "evidence_url", "notes",
]

_SCREEN_CLASS = {
    "blocked_robots": "blocked", "blocked_robots_unreachable": "blocked", "blocked_http": "blocked",
    "tdm_reserved": "blocked", "crawl_delay_excessive": "blocked", "screen_error": "route_unknown", "no_sitemap": "route_unknown", "sitemap_undated": "route_unknown",
    "sitemap_deep": "route_deep", "sitemap_mid": "route_mid", "sitemap_recent_only": "route_recent",
}


def _depth_class(year: int | None) -> str:
    if year is None:
        return "route_unknown"
    return "route_deep" if year <= 2015 else "route_mid" if year <= 2020 else "route_recent"


def from_profile(d: dict) -> dict:
    ap = d["access_path"]
    if d.get("paywall") == "hard" or (ap == "none" and _BLOCK_RE.search(d.get("access_notes", "") + d.get("baseline_event_coverage", "") )):
        cls = "blocked"
    elif ap == "none":
        cls = "no_route"
    elif ap == "wayback":
        cls = "wayback_only"
    else:
        cls = _depth_class(d.get("archive_start_year"))
    return {
        "domain": d["domain"], "country": d["country"], "name": d["name"], "outlet_type": d["outlet_type"],
        "languages": ",".join(d["languages"]), "source": "profile", "verdict": f"{ap}/{d.get('role') or ''}",
        "collectability": cls, "archive_year": d.get("archive_start_year"), "tdm_reservation": "",
        "paywall_hint": d.get("paywall"), "extract_ok_newest": d.get("extraction_success_rate"),
        "screened_at": d["profiled_at"], "evidence_url": d["evidence_urls"][0], "notes": d.get("role") or "",
    }


def from_screen(r: dict) -> dict:
    # 초기 버전 screen 결과도 같은 기준으로 재판정: 사이트맵 연도 우선, 이상값 제외, stop_domain은 차단
    sm = r.get("sitemap_year_min")
    sm = sm if sm and 1995 <= sm else None
    year = sm or r.get("archive_year_verified")
    verdict = r["verdict"]
    if r.get("stop_domain") and not verdict.startswith("blocked"):
        verdict = "blocked_http"
    elif verdict.startswith("sitemap_") and verdict != "sitemap_undated":
        verdict = {"route_deep": "sitemap_deep", "route_mid": "sitemap_mid", "route_recent": "sitemap_recent_only",
                   "route_unknown": "sitemap_undated"}[_depth_class(year)]
    r = {**r, "verdict": verdict}
    return {
        "domain": r["domain"], "country": r["country"], "name": r["name"], "outlet_type": r["outlet_type"],
        "languages": r["languages"], "source": "screen", "verdict": r["verdict"],
        "collectability": _SCREEN_CLASS.get(r["verdict"], "route_unknown"), "archive_year": year,
        "tdm_reservation": r.get("tdm_reservation"), "paywall_hint": r.get("paywall_hint") or "",
        "extract_ok_newest": r.get("extract_ok_newest"), "screened_at": r["screened_at"],
        "evidence_url": (r.get("evidence_urls") or [""])[0], "notes": "; ".join(r.get("notes") or []),
    }


def build(screen_files: list[str], profiles_dir: str = "registry/outlet_profiles") -> list[dict]:
    rows: dict[str, dict] = {}
    for p in sorted(Path(profiles_dir).glob("*.json")):
        row = from_profile(json.loads(p.read_text()))
        rows[row["domain"]] = row
    for f in screen_files:
        for line in Path(f).read_text().splitlines():
            if not line.strip():
                continue
            row = from_screen(json.loads(line))
            # 상세 프로파일이 있으면 그쪽이 우선. 같은 도메인을 다시 선별했으면 마지막 결과
            if rows.get(row["domain"], {}).get("source") != "profile":
                rows[row["domain"]] = row
    return sorted(rows.values(), key=lambda r: (r["country"], r["domain"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("screen_files", nargs="*")
    ap.add_argument("--out", default="registry/screening.csv")
    a = ap.parse_args()
    rows = build(a.screen_files)
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    by: dict[str, dict[str, int]] = {}
    for r in rows:
        by.setdefault(r["country"], {}).setdefault(r["collectability"], 0)
        by[r["country"]][r["collectability"]] += 1
    print(f"{len(rows)} outlets, {len(by)} countries -> {a.out}")


if __name__ == "__main__":
    main()
