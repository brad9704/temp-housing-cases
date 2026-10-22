"""뉴스 기사 원본(HTML) 수집기. news_sitemap list가 만든 urls.jsonl을 입력으로 쓴다.

    uv run python -m collectors.news_fetch fetch {outlet_id|all} [--sample N] [--seed S] [--limit N]
    uv run python -m collectors.news_fetch status

- 기사 HTML은 {RAW_DIR}/news/<outlet>/html/<sha1>.html.zst, 기록은 html_index.jsonl (URL, 상태, 경로, 코드 상태).
- 이미 받은 URL(ok)은 건너뛴다. 다시 실행하면 이어서 받는다.
- --sample N: 기간 안 URL에서 시드 고정 무작위 N건만 받는다(추출 품질·필터 통과율 점검용). 기록에 sample 태그가 남는다.
- 언론사(도메인)마다 스레드 하나. 같은 도메인 동시 요청은 없다(PoliteClient가 직렬화·간격 유지).
- 연속 401/403/429가 STOP_STREAK에 이르면 그 언론사는 멈추고 stopped.json에 기록한다. 우회하지 않는다.
- 재시도를 다 쓴 실패(5xx·연결 끊김)가 SERVER_ERROR_STREAK번 이어져도 멈춘다(고장 난 서버를 계속 두드리지 않는다).
  이 경우 stopped.json의 kind는 server_errors이고, 서버가 회복된 뒤 파일을 지우고 다시 실행한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yaml
import zstandard

from collectors.http import FetchFailed, PoliteClient, RobotsDisallowed

STOP_STREAK = 3
SERVER_ERROR_STREAK = 5
PLANS = Path("collectors/news_sitemap_plans.yaml")


def _news_dir() -> Path:
    return Path(os.environ.get("RAW_DIR", "data/raw")) / "news"


def _jsonl(path: Path) -> list[dict]:
    # splitlines()는 U+2028 등에서도 나누므로 \n으로만 자른다
    return [json.loads(l) for l in path.read_text().split("\n") if l.strip()] if path.exists() else []


def _code_state() -> dict:
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True).stdout.strip())
    return {"code_commit": head, "code_dirty": dirty}


def _outlets() -> list[str]:
    return sorted(p.parent.name for p in _news_dir().glob("*/urls.jsonl"))


class OutletFetcher:
    def __init__(self, oid: str, code: dict, client: PoliteClient):
        self.oid, self.code, self.client = oid, code, client
        self.dir = _news_dir() / oid
        self.html = self.dir / "html"
        self.html.mkdir(exist_ok=True)
        self.index = self.dir / "html_index.jsonl"
        self.cctx = zstandard.ZstdCompressor(level=10)
        plans = yaml.safe_load(PLANS.read_text())["outlets"]
        # 목록화와 같은 규칙: http를 끊는 사이트(예: emol)는 https로 요청한다. 기록의 url은 사이트맵 원문 그대로 둔다
        self.https_only = bool(plans.get(oid, {}).get("https_only"))

    def done(self) -> set[str]:
        return {x["url"] for x in _jsonl(self.index) if x.get("ok")}

    def _log(self, rec: dict) -> None:
        with self.index.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def one(self, item: dict, tag: str | None) -> tuple[dict, str | None]:
        """(기록, 실패 종류: "blocked" | "server_error" | None)"""
        url = item["url"]
        req = "https://" + url[len("http://"):] if self.https_only and url.startswith("http://") else url
        base = {"url": url, "date": item.get("date"), "sample": tag,
                "fetched_at": datetime.now(timezone.utc).isoformat(), **self.code}
        try:
            r = self.client.fetch(req)
        except RobotsDisallowed:
            rec = {**base, "ok": False, "error": "robots_disallowed"}
            self._log(rec)
            return rec, None
        except FetchFailed as e:
            rec = {**base, "ok": False, "error": f"fetch_failed: {str(e).replace(req, '<url>')[-200:]}"}
            self._log(rec)
            return rec, "blocked" if " 429 " in f" {e} " else "server_error"
        if r.status != 200:
            rec = {**base, "ok": False, "error": f"HTTP {r.status}", "final_url": r.final_url}
            self._log(rec)
            return rec, "blocked" if r.status in (401, 403, 429) else ("server_error" if r.status >= 500 else None)
        h = hashlib.sha1(url.encode()).hexdigest()
        path = self.html / f"{h}.html.zst"
        path.write_bytes(self.cctx.compress(r.content))
        rec = {**base, "ok": True, "path": str(path), "bytes": len(r.content),
               "sha256": hashlib.sha256(r.content).hexdigest(), "final_url": r.final_url, "status": r.status}
        self._log(rec)
        return rec, None

    def run(self, sample: int | None, seed: int, limit: int | None) -> dict:
        urls = _jsonl(self.dir / "urls.jsonl")
        tag = None
        if sample:
            tag = f"random{sample}_seed{seed}"
            urls = random.Random(f"{seed}:{self.oid}").sample(urls, min(sample, len(urls)))
        done = self.done()
        todo = [u for u in urls if u["url"] not in done]
        if limit:
            todo = todo[:limit]
        print(f"{self.oid}: {len(todo)} to fetch ({len(done)} already ok)", flush=True)
        c, streak = Counter(), Counter()
        for i, item in enumerate(todo, 1):
            rec, kind = self.one(item, tag)
            c["ok" if rec["ok"] else rec["error"][:40]] += 1
            streak = Counter({kind: streak[kind] + 1}) if kind else Counter()
            stop = None
            if streak["blocked"] >= STOP_STREAK:
                stop = ("blocked", f"{STOP_STREAK} consecutive 401/403/429")
            elif streak["server_error"] >= SERVER_ERROR_STREAK:
                stop = ("server_errors", f"{SERVER_ERROR_STREAK} consecutive 5xx/connection failures after retries")
            if stop:
                note = {"outlet": self.oid, "stopped_at": datetime.now(timezone.utc).isoformat(),
                        "kind": stop[0], "reason": stop[1], "last_error": rec.get("error")}
                (self.dir / "stopped.json").write_text(json.dumps(note, ensure_ascii=False, indent=1))
                print(f"STOP {self.oid}: {note['reason']}", flush=True)
                break
            if i % 100 == 0:
                print(f"{self.oid} [{i}/{len(todo)}] {dict(c)}", flush=True)
        print(f"{self.oid} done: {dict(c)}", flush=True)
        return {"outlet": self.oid, **c}


def cmd_fetch(a) -> None:
    free_gb = shutil.disk_usage(_news_dir()).free / 1e9
    min_gb = float(os.environ.get("MIN_FREE_GB", 100))
    if free_gb < min_gb:
        raise SystemExit(f"여유 공간 {free_gb:.0f}GB < MIN_FREE_GB {min_gb:.0f}GB: 수집 중단")
    targets = _outlets() if a.outlet == "all" else [a.outlet]
    targets = [o for o in targets if not (_news_dir() / o / "stopped.json").exists()]
    code, client = _code_state(), PoliteClient()
    with ThreadPoolExecutor(max_workers=len(targets) or 1) as ex:
        res = list(ex.map(lambda o: OutletFetcher(o, code, client).run(a.sample, a.seed, a.limit), targets))
    print(json.dumps(res, ensure_ascii=False))


def cmd_status(a) -> None:
    out = []
    for oid in _outlets():
        d = _news_dir() / oid
        last = {x["url"]: x for x in _jsonl(d / "html_index.jsonl")}
        c = Counter("ok" if x.get("ok") else (x.get("error") or "?")[:40] for x in last.values())
        n = sum(1 for l in (d / "urls.jsonl").open("rb"))
        out.append({"outlet": oid, "listed": n, "attempted": len(last), "outcomes": c.most_common(4),
                    "stopped": (d / "stopped.json").exists()})
    print(json.dumps(out, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(prog="news_fetch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch")
    p.add_argument("outlet")
    p.add_argument("--sample", type=int)
    p.add_argument("--seed", type=int, default=20261022)
    p.add_argument("--limit", type=int)
    p.set_defaults(fn=cmd_fetch)
    p = sub.add_parser("status")
    p.set_defaults(fn=cmd_status)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
