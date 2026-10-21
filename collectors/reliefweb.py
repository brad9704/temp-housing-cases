"""ReliefWeb API 수집기 (구호기관 문서 트랙).

    uv run python -m collectors.reliefweb list-reports [--theme "Shelter and Non-Food Items"] [--from 2000]
    uv run python -m collectors.reliefweb summary

- appname은 .env의 RELIEFWEB_APP_NAME. 로그·출력·레코드에 appname을 남기지 않는다.
- api.reliefweb.int robots는 `Disallow: /`라서 CRAWL_ROBOTS_EXCEPTIONS 예외가 있어야 동작한다.
- 하루 호출 한도(1,000회)를 넘지 않도록 날짜별 호출 수를 CRAWL_STATE_DIR에 기록하고 DAILY_CAP에서 멈춘다.
  멈춘 뒤 같은 명령을 다시 실행하면 끝난 월 단위 구간은 건너뛴다.
- 메타데이터만 받는다. 본문(PDF)은 별도 단계.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from collectors.http import PoliteClient

API = "https://api.reliefweb.int/v2/reports"
PAGE = 1000
DAILY_CAP = 900  # 공식 한도 1,000회/일에 여유
FIELDS = [
    "id", "title", "url", "date.created", "date.original", "language.code",
    "source.name", "source.shortname", "source.type.name",
    "primary_country.iso3", "country.iso3",
    "disaster.id", "disaster.name", "disaster_type.name",
    "format.name", "theme.name", "file.url", "file.mimetype",
]


class DailyCapReached(Exception):
    pass


def _raw_dir() -> Path:
    d = Path(os.environ.get("RAW_DIR", "data/raw")) / "reliefweb"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _code_state() -> dict:
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True).stdout.strip())
    return {"code_commit": head, "code_dirty": dirty}


class Api:
    def __init__(self) -> None:
        self.client = PoliteClient()
        self.app = os.environ.get("RELIEFWEB_APP_NAME", "").strip()
        if not self.app:
            raise RuntimeError("RELIEFWEB_APP_NAME가 .env에 없습니다.")
        if not self.client.is_robots_exception(API):
            raise RuntimeError("CRAWL_ROBOTS_EXCEPTIONS에 api.reliefweb.int가 없습니다 (CLAUDE.md 예외 규칙 참고).")
        state = Path(os.environ.get("CRAWL_STATE_DIR", "data/cache/crawl_state"))
        state.mkdir(parents=True, exist_ok=True)
        self.counter = state / f"reliefweb_calls_{date.today().isoformat()}.txt"

    def _bump(self) -> int:
        n = int(self.counter.read_text()) if self.counter.exists() else 0
        if n >= DAILY_CAP:
            raise DailyCapReached(f"오늘 호출 {n}회, 상한 {DAILY_CAP}")
        self.counter.write_text(str(n + 1))
        return n + 1

    def get(self, params: list[tuple[str, str]]) -> dict:
        self._bump()
        r = self.client.fetch(f"{API}?{urlencode([('appname', self.app), *params])}")
        d = json.loads(r.content)
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}: {d.get('error')}")
        return d


def _conditions(theme: str | None, start: str, end: str) -> list[tuple[str, str]]:
    conds = [("date.created", None, start, end)]
    if theme:
        conds.append(("theme.name", theme, None, None))
    p: list[tuple[str, str]] = [("filter[operator]", "AND")]
    for i, (field, value, frm, to) in enumerate(conds):
        p.append((f"filter[conditions][{i}][field]", field))
        if value is not None:
            p.append((f"filter[conditions][{i}][value]", value))
        else:
            p.append((f"filter[conditions][{i}][value][from]", frm))
            p.append((f"filter[conditions][{i}][value][to]", to))
    return p


def _months(year_from: int) -> list[tuple[str, str, str]]:
    out = []
    today = date.today()
    for y in range(year_from, today.year + 1):
        for m in range(1, 13):
            if (y, m) > (today.year, today.month):
                break
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            out.append((f"{y}-{m:02d}", f"{y}-{m:02d}-01T00:00:00+00:00", f"{ny}-{nm:02d}-01T00:00:00+00:00"))
    return out


def cmd_list_reports(a) -> None:
    api = Api()
    out = _raw_dir() / "reports"
    out.mkdir(exist_ok=True)
    meta = {"source": API, "theme_filter": a.theme, **_code_state()}
    fields = [("fields[include][]", f) for f in FIELDS]
    done = skipped = 0
    try:
        for label, start, end in _months(a.year_from):
            path = out / f"{label}.jsonl"
            if path.exists():
                skipped += 1
                continue
            base = _conditions(a.theme, start, end) + fields + [("sort[]", "date.created:asc"), ("limit", str(PAGE))]
            recs, offset, total = [], 0, None
            while total is None or offset < total:
                d = api.get(base + [("offset", str(offset))])
                total = d.get("totalCount", 0)
                fetched_at = datetime.now(timezone.utc).isoformat()
                for item in d.get("data", []):
                    recs.append({**item.get("fields", {}), "_meta": {**meta, "fetched_at": fetched_at}})
                if not d.get("data"):
                    break
                offset += PAGE
            tmp = path.with_suffix(".part")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))
            tmp.replace(path)  # 월 구간이 끝까지 받아졌을 때만 완료 파일 생성
            done += 1
            print(f"{label}: {len(recs)} (total {total})", flush=True)
    except DailyCapReached as e:
        print(f"중단: {e}. 내일 같은 명령을 다시 실행하면 이어서 받습니다.")
    print(f"months done {done}, skipped (already saved) {skipped}")


def cmd_summary(a) -> None:
    recs = []
    for p in sorted((_raw_dir() / "reports").glob("*.jsonl")):
        recs += [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    def flat(r, key, sub):
        v = r.get(key) or []
        v = v if isinstance(v, list) else [v]
        return [x.get(sub) for x in v if isinstance(x, dict)]
    fmt = Counter(f for r in recs for f in flat(r, "format", "name"))
    src = Counter(t for r in recs for s in (r.get("source") or []) for t in [((s.get("type") or {}).get("name"))])
    dt = Counter(t for r in recs for t in flat(r, "disaster_type", "name"))
    year = Counter(((r.get("date") or {}).get("created") or "????")[:4] for r in recs)
    lang = Counter(l for r in recs for l in flat(r, "language", "code"))
    print(json.dumps({
        "reports": len(recs),
        "with_files": sum(1 for r in recs if r.get("file")),
        "format": fmt.most_common(),
        "source_type": src.most_common(),
        "disaster_type": dt.most_common(15),
        "language": lang.most_common(10),
        "year": sorted(year.items()),
    }, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(prog="reliefweb")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list-reports")
    p.add_argument("--theme", default="Shelter and Non-Food Items")
    p.add_argument("--from", dest="year_from", type=int, default=1990)
    p.set_defaults(fn=cmd_list_reports)
    p = sub.add_parser("summary")
    p.set_defaults(fn=cmd_summary)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
