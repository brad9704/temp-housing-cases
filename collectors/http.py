"""공용 HTTP 수집 클라이언트.

모든 수집기는 이 모듈의 `PoliteClient.fetch()`만 사용한다. 직접 requests/curl_cffi를 부르지 않는다.

보장하는 것
- 봇 이름이 들어간 고정 User-Agent (브라우저 위장, UA 로테이션, TLS 지문 위장 금지)
- robots.txt 준수 (RFC 9309: 4xx는 전체 허용, 5xx·연결 실패는 전체 금지로 간주)
- 도메인별 최소 간격 + 랜덤 지터. robots의 Crawl-delay가 더 길면 그 값을 따른다
- 429/503의 Retry-After 존중, 지수 백오프 + 지터
- 요청 단위 메타데이터 반환 (상태 코드, 최종 URL, 대기 시간, 시각)
"""

from __future__ import annotations

import email.utils
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
                self._domains[origin] = _DomainState()
            return self._domains[origin]

    @staticmethod
    def _origin(url: str) -> str:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    def _jitter_delay(self, st: _DomainState) -> float:
        d = self.rng.uniform(self.cfg.min_delay, self.cfg.max_delay)
        if st.robots is not None:
            cd = st.robots.crawl_delay(self.cfg.bot_token)
            if cd:
                d = max(d, float(cd))
        return d

    def _wait_turn(self, st: _DomainState) -> float:
        """호출자는 st.lock을 잡고 있어야 한다."""
        now = time.monotonic()
        wait = max(0.0, st.next_allowed - now)
        if wait:
            time.sleep(wait)
        return wait

    def _schedule_next(self, st: _DomainState, extra: float = 0.0) -> None:
        st.next_allowed = time.monotonic() + self._jitter_delay(st) + extra

    # ---- robots -------------------------------------------------------------
    def _load_robots(self, origin: str, st: _DomainState) -> None:
        if st.robots_mode != "unknown":
            return
        self._wait_turn(st)
        try:
            r = self.session.get(f"{origin}/robots.txt", timeout=self.cfg.timeout, allow_redirects=True)
            if r.status_code == 200:
                st.robots = Protego.parse(r.text)
                st.robots_mode = "parsed"
            elif 400 <= r.status_code < 500:
                st.robots_mode = "allow_all"
            else:
                st.robots_mode = "deny_all"
        except Exception:
            st.robots_mode = "deny_all"
        finally:
            self._schedule_next(st)

    def robots_status(self, url: str) -> str:
        origin = self._origin(url)
        st = self._state(origin)
        with st.lock:
            self._load_robots(origin, st)
        return st.robots_mode

    def allowed(self, url: str) -> bool:
        origin = self._origin(url)
        st = self._state(origin)
        with st.lock:
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
            with st.lock:
                waited += self._wait_turn(st)
                extra = 0.0
                try:
                    r = self.session.get(url, timeout=self.cfg.timeout, allow_redirects=True)
                except Exception as e:  # 네트워크 오류
                    last_err = e
                    extra = self._backoff(attempt)
                    self._schedule_next(st, extra)
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
