"""공용 HTTP 수집 클라이언트.

모든 수집기는 이 모듈의 `PoliteClient.fetch()`만 사용한다. 직접 requests/curl_cffi를 부르지 않는다.

보장하는 것
- 봇 이름이 들어간 고정 User-Agent (브라우저 위장, UA 로테이션, TLS 지문 위장 금지)
- robots.txt 준수 (RFC 9309: 4xx는 전체 허용, 5xx·연결 실패는 전체 금지로 간주)
- 도메인별 최소 간격 + 랜덤 지터. robots의 Crawl-delay가 더 길면 그 값을 따른다
- 429/503의 Retry-After 존중, 지수 백오프 + 지터
- 요청 단위 메타데이터 반환 (상태 코드, 최종 URL, 대기 시간, 시각)
- `state_dir`가 있으면 도메인별 잠금 파일(flock)과 다음 허용 시각을 디스크에 둔다.
  여러 프로세스(CLI 반복 실행, 병렬 서브에이전트)가 같은 도메인에 동시 요청하거나
  간격을 건너뛰지 않게 한다. robots.txt도 같은 곳에 TTL 캐시한다.
"""

from __future__ import annotations

import email.utils
import fcntl
import json
import os
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from curl_cffi import requests as cffi_requests
from dotenv import load_dotenv
from protego import Protego

load_dotenv()


# 크롤러 표준 형식 "Mozilla/5.0 (compatible; Bot/1.0; +URL)"은 허용한다.
# 실제 브라우저 토큰과 다른 크롤러 이름(사칭)은 금지한다.
_COMPAT_PREFIX = "Mozilla/5.0 (compatible; "
_BROWSER_MARKERS = ("Chrome/", "Safari/", "Firefox/", "Edg/", "AppleWebKit", "Gecko/", "Windows NT", "Macintosh", "iPhone", "Android")
_OTHER_CRAWLERS = (
    "googlebot", "google-", "adsbot", "mediapartners", "bingbot", "msnbot", "yeti", "daum",
    "applebot", "duckduckbot", "baiduspider", "yandex", "facebookexternalhit", "twitterbot",
    "slurp", "gptbot", "claudebot", "ccbot", "petalbot", "seznambot",
)


def validate_user_agent(ua: str, token: str) -> None:
    if not ua:
        raise RuntimeError("CRAWL_USER_AGENT를 설정하세요.")
    if not token or token not in ua:
        raise RuntimeError("CRAWL_BOT_TOKEN은 비어 있으면 안 되고 CRAWL_USER_AGENT 안에 포함되어야 합니다.")
    if "Mozilla/" in ua and not ua.startswith(_COMPAT_PREFIX):
        raise RuntimeError(f"Mozilla 토큰은 크롤러 표준 형식('{_COMPAT_PREFIX}...')으로만 허용됩니다.")
    if any(m in ua for m in _BROWSER_MARKERS):
        raise RuntimeError("CRAWL_USER_AGENT에 브라우저 식별자가 있습니다. 브라우저 위장은 금지입니다.")
    lowered = ua.lower().replace(token.lower(), "")
    hit = [c for c in _OTHER_CRAWLERS if c in lowered]
    if hit:
        raise RuntimeError(f"다른 크롤러 이름이 포함되어 있습니다({hit}). 사칭은 금지입니다.")


def parse_domain_overrides(spec: str) -> dict[str, tuple[float, float, float]]:
    """"web.archive.org=5:8:120,example.com=3:6:60" -> {host: (min, max, timeout)}"""
    out = {}
    for item in filter(None, (x.strip() for x in spec.split(","))):
        host, _, vals = item.partition("=")
        mn, mx, to = (float(v) for v in vals.split(":"))
        if mx < mn:
            raise RuntimeError(f"CRAWL_DOMAIN_OVERRIDES 오류: {item}")
        out[host.strip().lower()] = (mn, mx, to)
    return out


class RobotsDisallowed(Exception):
    """robots.txt가 해당 URL 수집을 금지함."""


class FetchFailed(Exception):
    """재시도 후에도 요청 실패."""


@dataclass
class CrawlConfig:
    user_agent: str
    bot_token: str
    min_delay: float = 2.0
    max_delay: float = 5.0
    max_retries: int = 3
    timeout: float = 30.0
    backoff_base: float = 10.0
    backoff_cap: float = 300.0
    state_dir: str | None = None
    robots_ttl: float = 86400.0
    # host -> (min_delay, max_delay, timeout). 기본값보다 느슨하게만 쓸 수 있다.
    domain_overrides: dict[str, tuple[float, float, float]] = field(default_factory=dict)

    def for_host(self, host: str) -> tuple[float, float, float]:
        mn, mx, to = self.domain_overrides.get(host, (self.min_delay, self.max_delay, self.timeout))
        return max(mn, self.min_delay), max(mx, self.max_delay), max(to, self.timeout)

    @classmethod
    def from_env(cls) -> "CrawlConfig":
        ua = os.environ.get("CRAWL_USER_AGENT", "").strip()
        token = os.environ.get("CRAWL_BOT_TOKEN", "").strip()
        validate_user_agent(ua, token)
        cfg = cls(
            user_agent=ua,
            bot_token=token,
            min_delay=float(os.environ.get("CRAWL_MIN_DELAY_SEC", 2.0)),
            max_delay=float(os.environ.get("CRAWL_MAX_DELAY_SEC", 5.0)),
            max_retries=int(os.environ.get("CRAWL_MAX_RETRIES", 3)),
            timeout=float(os.environ.get("CRAWL_TIMEOUT_SEC", 30.0)),
            state_dir=os.environ.get("CRAWL_STATE_DIR", "data/cache/crawl_state") or None,
            domain_overrides=parse_domain_overrides(os.environ.get("CRAWL_DOMAIN_OVERRIDES", "")),
        )
        if cfg.min_delay < 1.0 or cfg.max_delay < cfg.min_delay:
            raise RuntimeError("딜레이 설정 오류: 1 <= MIN_DELAY <= MAX_DELAY 이어야 합니다.")
        return cfg


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content: bytes
    headers: dict
    fetched_at: str
    waited_sec: float
    attempts: int


@dataclass
class _DomainState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_allowed: float = 0.0
    robots: Protego | None = None
    robots_mode: str = "unknown"  # parsed | allow_all | deny_all
    robots_raw: str = ""
    host_cfg: tuple[float, float, float] | None = None
    robots_error: str = ""  # deny_all 판정 사유 (5xx 상태 또는 예외)
    last_wait: float = 0.0


class PoliteClient:
    def __init__(self, config: CrawlConfig | None = None, rng: random.Random | None = None):
        self.cfg = config or CrawlConfig.from_env()
        validate_user_agent(self.cfg.user_agent, self.cfg.bot_token)
        self.rng = rng or random.Random()
        # impersonate 인자를 절대 넘기지 않는다.
        self.session = cffi_requests.Session()
        self.session.headers.update({"User-Agent": self.cfg.user_agent})
        self._domains: dict[str, _DomainState] = {}
        self._domains_lock = threading.Lock()

    # ---- domain bookkeeping -------------------------------------------------
    def _state(self, origin: str) -> _DomainState:
        with self._domains_lock:
            if origin not in self._domains:
                self._domains[origin] = _DomainState(host_cfg=self._host_cfg(origin))
            return self._domains[origin]

    @staticmethod
    def _origin(url: str) -> str:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    def _host_cfg(self, origin: str) -> tuple[float, float, float]:
        return self.cfg.for_host(urlsplit(origin).hostname or "")

    def _jitter_delay(self, st: _DomainState) -> float:
        mn, mx, _ = st.host_cfg or (self.cfg.min_delay, self.cfg.max_delay, self.cfg.timeout)
        d = self.rng.uniform(mn, mx)
        if st.robots is not None:
            cd = st.robots.crawl_delay(self.cfg.bot_token)
            if cd:
                d = max(d, float(cd))
        return d

    def _state_file(self, origin: str, suffix: str) -> Path | None:
        if not self.cfg.state_dir:
            return None
        d = Path(self.cfg.state_dir)
        d.mkdir(parents=True, exist_ok=True)
        name = urlsplit(origin).netloc.replace(":", "_")
        return d / f"{name}.{suffix}"

    @contextmanager
    def _turn(self, origin: str, st: _DomainState):
        """도메인 한 칸 차지: 스레드 잠금 + (state_dir 있으면) 프로세스 간 flock.

        진입 시 다음 허용 시각까지 기다리고, 블록 안에서 요청 1건을 보낸 뒤
        호출자가 `_schedule_next`로 다음 허용 시각을 정한다. 시각은 벽시계(time.time) 기준.
        """
        with st.lock:
            lock_path = self._state_file(origin, "lock")
            fh = open(lock_path, "a+") if lock_path else None
            try:
                if fh:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                    fh.seek(0)
                    raw = fh.read().strip()
                    if raw:
                        try:
                            st.next_allowed = max(st.next_allowed, float(raw))
                        except ValueError:
                            pass
                wait = max(0.0, st.next_allowed - time.time())
                if wait:
                    time.sleep(wait)
                st.last_wait = wait
                yield
                if fh:
                    fh.seek(0)
                    fh.truncate()
                    fh.write(repr(st.next_allowed))
                    fh.flush()
            finally:
                if fh:
                    fcntl.flock(fh, fcntl.LOCK_UN)
                    fh.close()

    def _schedule_next(self, st: _DomainState, extra: float = 0.0) -> None:
        st.next_allowed = time.time() + self._jitter_delay(st) + extra

    # ---- robots -------------------------------------------------------------
    def _load_robots(self, origin: str, st: _DomainState) -> None:
        if st.robots_mode != "unknown" or self._robots_from_cache(origin, st):
            return
        with self._turn(origin, st):
            # 잠금을 기다리는 사이 다른 스레드·프로세스가 받아 왔을 수 있다.
            if st.robots_mode != "unknown" or self._robots_from_cache(origin, st):
                return
            text = ""
            try:
                r = self.session.get(f"{origin}/robots.txt", timeout=st.host_cfg[2], allow_redirects=True)
                if r.status_code == 200:
                    mode, text = "parsed", r.text
                elif 400 <= r.status_code < 500:
                    mode = "allow_all"
                else:
                    mode = "deny_all"
                    st.robots_error = f"HTTP {r.status_code}"
            except Exception as e:
                mode = "deny_all"
                st.robots_error = f"{type(e).__name__}: {e}"[:300]
            self._apply_robots(st, mode, text)
            self._schedule_next(st)
            cache = self._state_file(origin, "robots.json")
            # 연결 실패·5xx는 일시적일 수 있으므로 캐시하지 않는다.
            if cache and mode != "deny_all":
                cache.write_text(json.dumps({"fetched_at": time.time(), "mode": mode, "text": text}))

    def _robots_from_cache(self, origin: str, st: _DomainState) -> bool:
        cache = self._state_file(origin, "robots.json")
        if not (cache and cache.exists()):
            return False
        try:
            c = json.loads(cache.read_text())
            if time.time() - c["fetched_at"] >= self.cfg.robots_ttl:
                return False
            self._apply_robots(st, c["mode"], c.get("text", ""))
            return True
        except Exception:
            return False

    @staticmethod
    def _apply_robots(st: _DomainState, mode: str, text: str) -> None:
        st.robots = Protego.parse(text) if mode == "parsed" else None
        st.robots_raw = text
        st.robots_mode = mode

    def robots_text(self, url: str) -> str | None:
        """파싱된 robots.txt 원문 (recon 기록용). 없으면 None."""
        self.robots_status(url)
        return self._state(self._origin(url)).robots_raw or None

    def robots_status(self, url: str) -> str:
        origin = self._origin(url)
        st = self._state(origin)
        self._load_robots(origin, st)
        return st.robots_mode

    def allowed(self, url: str) -> bool:
        origin = self._origin(url)
        st = self._state(origin)
        self._load_robots(origin, st)
        if st.robots_mode == "allow_all":
            return True
        if st.robots_mode == "deny_all":
            return False
        return st.robots.can_fetch(url, self.cfg.bot_token)

    # ---- fetch --------------------------------------------------------------
    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if not value:
            return None
        value = value.strip()
        if value.isdigit():
            return float(value)
        try:
            dt = email.utils.parsedate_to_datetime(value)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            return None

    def _backoff(self, attempt: int) -> float:
        base = min(self.cfg.backoff_cap, self.cfg.backoff_base * (2 ** (attempt - 1)))
        return base * self.rng.uniform(0.5, 1.5)

    def fetch(self, url: str) -> FetchResult:
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        origin = self._origin(url)
        st = self._state(origin)
        waited = 0.0
        last_err: Exception | None = None
        for attempt in range(1, self.cfg.max_retries + 1):
            with self._turn(origin, st):
                waited += st.last_wait
                try:
                    r = self.session.get(url, timeout=st.host_cfg[2], allow_redirects=True)
                except Exception as e:  # 네트워크 오류
                    last_err = e
                    self._schedule_next(st, self._backoff(attempt))
                    continue
                if r.status_code in (429, 503):
                    ra = self._retry_after(r.headers.get("Retry-After"))
                    extra = min(self.cfg.backoff_cap, ra) if ra is not None else self._backoff(attempt)
                    last_err = FetchFailed(f"{r.status_code} {url}")
                    self._schedule_next(st, extra)
                    continue
                if 500 <= r.status_code < 600:
                    last_err = FetchFailed(f"{r.status_code} {url}")
                    self._schedule_next(st, self._backoff(attempt))
                    continue
                self._schedule_next(st)
                return FetchResult(
                    url=url,
                    final_url=str(r.url),
                    status=r.status_code,
                    content=r.content,
                    headers=dict(r.headers),
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                    waited_sec=round(waited, 3),
                    attempts=attempt,
                )
        raise FetchFailed(f"{url}: {last_err}")
