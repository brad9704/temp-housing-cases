import http.server
import random
import threading
import time

import pytest

from collectors.http import CrawlConfig, PoliteClient, RobotsDisallowed, parse_domain_overrides

SEEN = {"ua": [], "times": [], "hits_429": 0}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        SEEN["ua"].append(self.headers.get("User-Agent"))
        SEEN["times"].append((self.path, time.monotonic()))
        if self.path == "/robots.txt":
            body = b"User-agent: TestBot\nDisallow: /private/\nCrawl-delay: 0.5\n"
            self.send_response(200)
        elif self.path == "/limited" and SEEN["hits_429"] == 0:
            SEEN["hits_429"] += 1
            self.send_response(429)
            self.send_header("Retry-After", "1")
            body = b""
        else:
            self.send_response(200)
            body = b"ok"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def client():
    cfg = CrawlConfig(
        user_agent="TestBot/0.1",
        bot_token="TestBot",
        min_delay=0.2,
        max_delay=0.3,
        max_retries=3,
        timeout=5,
        backoff_base=0.1,
    )
    return PoliteClient(cfg, rng=random.Random(0))


def test_honest_user_agent_sent(server, client):
    client.fetch(f"{server}/a")
    assert all(ua == "TestBot/0.1" for ua in SEEN["ua"])


def test_robots_disallow(server, client):
    with pytest.raises(RobotsDisallowed):
        client.fetch(f"{server}/private/x")


def test_crawl_delay_overrides_jitter(server, client):
    SEEN["times"].clear()
    client.fetch(f"{server}/b")
    client.fetch(f"{server}/c")
    ts = [t for p, t in SEEN["times"] if p in ("/b", "/c")]
    assert ts[1] - ts[0] >= 0.5  # robots Crawl-delay 0.5 > jitter 0.2~0.3


def test_retry_after_respected(server, client):
    SEEN["times"].clear()
    r = client.fetch(f"{server}/limited")
    assert r.status == 200 and r.attempts == 2
    ts = [t for p, t in SEEN["times"] if p == "/limited"]
    assert ts[1] - ts[0] >= 1.0


def test_env_accepts_bare_bot_name(monkeypatch):
    monkeypatch.setenv("CRAWL_USER_AGENT", "UNISTDisasterHousingBot/0.1")
    monkeypatch.setenv("CRAWL_BOT_TOKEN", "UNISTDisasterHousingBot")
    assert CrawlConfig.from_env().user_agent == "UNISTDisasterHousingBot/0.1"


TOKEN = "UNISTDisasterHousingBot"


@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (compatible; UNISTDisasterHousingBot/0.1)",
    "Mozilla/5.0 (compatible; UNISTDisasterHousingBot/0.1; +https://example.org/bot)",
])
def test_googlebot_style_format_allowed(monkeypatch, ua):
    monkeypatch.setenv("CRAWL_USER_AGENT", ua)
    monkeypatch.setenv("CRAWL_BOT_TOKEN", TOKEN)
    assert CrawlConfig.from_env().user_agent == ua


@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 UNISTDisasterHousingBot",                                              # 비표준 Mozilla
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 UNISTDisasterHousingBot",   # 브라우저 위장
    "Mozilla/5.0 (compatible; Googlebot/2.1; UNISTDisasterHousingBot)",                 # 구글봇 사칭
    "Mozilla/5.0 (compatible; UNISTDisasterHousingBot/0.1; Yeti)",                      # 네이버 크롤러 사칭
])
def test_spoofing_rejected(monkeypatch, ua):
    monkeypatch.setenv("CRAWL_USER_AGENT", ua)
    monkeypatch.setenv("CRAWL_BOT_TOKEN", TOKEN)
    with pytest.raises(RuntimeError):
        CrawlConfig.from_env()


def test_env_rejects_missing_token(monkeypatch):
    monkeypatch.setenv("CRAWL_USER_AGENT", "SomethingElse/1.0")
    monkeypatch.setenv("CRAWL_BOT_TOKEN", "UNISTDisasterHousingBot")
    with pytest.raises(RuntimeError):
        CrawlConfig.from_env()


def _cfg(state_dir):
    return CrawlConfig(
        user_agent="TestBot/0.1", bot_token="TestBot", min_delay=0.2, max_delay=0.3,
        max_retries=3, timeout=5, backoff_base=0.1, state_dir=str(state_dir),
    )


def test_spacing_and_robots_shared_across_clients(server, tmp_path):
    # 별도 프로세스를 흉내: 상태 디렉터리만 공유하는 클라이언트 두 개
    SEEN["times"].clear()
    PoliteClient(_cfg(tmp_path), rng=random.Random(1)).fetch(f"{server}/d")
    PoliteClient(_cfg(tmp_path), rng=random.Random(2)).fetch(f"{server}/e")
    paths = [p for p, _ in SEEN["times"]]
    assert paths.count("/robots.txt") == 1  # 두 번째 클라이언트는 디스크 캐시 사용
    ts = [t for p, t in SEEN["times"] if p in ("/d", "/e")]
    assert ts[1] - ts[0] >= 0.5  # 두 번째 클라이언트도 Crawl-delay를 지킴


def test_no_concurrent_requests_same_domain(server, tmp_path):
    SEEN["times"].clear()
    clients = [PoliteClient(_cfg(tmp_path), rng=random.Random(i)) for i in range(3)]
    threads = [threading.Thread(target=c.fetch, args=(f"{server}/p{i}",)) for i, c in enumerate(clients)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    ts = sorted(t for p, t in SEEN["times"] if p.startswith("/p"))
    assert len(ts) == 3
    assert all(b - a >= 0.5 for a, b in zip(ts, ts[1:]))


def test_domain_override_slows_only_that_host(server):
    cfg = _cfg(None)
    cfg.state_dir = None
    cfg.domain_overrides = parse_domain_overrides("127.0.0.1=1.0:1.1:9")
    c = PoliteClient(cfg, rng=random.Random(3))
    SEEN["times"].clear()
    c.fetch(f"{server}/f")
    c.fetch(f"{server}/g")
    ts = [t for p, t in SEEN["times"] if p in ("/f", "/g")]
    assert ts[1] - ts[0] >= 1.0
    assert cfg.for_host("other.example") == (0.2, 0.3, 5)


def test_override_cannot_be_faster_than_default():
    cfg = _cfg(None)
    cfg.domain_overrides = parse_domain_overrides("x.org=0.01:0.02:1")
    assert cfg.for_host("x.org") == (0.2, 0.3, 5)


def test_sitemap_parse_and_years():
    from collectors.recon_probe import _parse_sitemap, _years
    xml = b"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <sitemap><loc>https://x.org/sitemap-2011-03.xml</loc></sitemap>
    <sitemap><loc>https://x.org/post-sitemap7.xml</loc><lastmod>2019-05-01T00:00:00Z</lastmod></sitemap>
    </sitemapindex>"""
    kind, ents = _parse_sitemap(xml)
    assert kind == "sitemapindex" and len(ents) == 2
    assert _years(ents[0]) == [2011] and _years(ents[1]) == [2019]


def test_content_signals_star_group_only():
    from collectors.recon_probe import parse_content_signals
    txt = """User-agent: *
Content-Signal: search=yes, ai-train=yes, ai-input=no
Allow: /

User-agent: Amazonbot
Content-Signal: ai-input=yes
Disallow: /
"""
    assert parse_content_signals(txt) == {"search": "yes", "ai-train": "yes", "ai-input": "no"}
