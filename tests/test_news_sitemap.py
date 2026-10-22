import xml.etree.ElementTree as ET
from datetime import date

import pytest

from collectors.news_sitemap import date_from_article_url, parse, parse_lenient, span_from_url


@pytest.mark.parametrize("url,expected", [
    ("https://news.kbs.co.kr/sitemap/dailyNewsList/20180107.xml", (date(2018, 1, 7), date(2018, 1, 7))),
    ("https://image.donga.com/sitemap/donga/donga-sitemap-201711-201712.xml.gz", (date(2017, 11, 1), date(2017, 12, 31))),
    ("https://www.cumhuriyet.com.tr/sitemaps/posts-2023-2.xml", (date(2023, 2, 1), date(2023, 2, 28))),
    ("https://www.trthaber.com/sitemaps/news-2023-02.xml", (date(2023, 2, 1), date(2023, 2, 28))),
    ("https://www.hespress.com/sitemap-posttype-post.202309.xml", (date(2023, 9, 1), date(2023, 9, 30))),
    ("https://www.tgcom24.mediaset.it/sitemap/contents/articolo/2016/08/sitemap.xml", (date(2016, 8, 1), date(2016, 8, 31))),
    ("https://www.emol.com/sitemap/xml/sitemap3_2010.xml", (date(2010, 1, 1), date(2010, 12, 31))),
    ("https://www.latimes.com/sitemaps/sitemap-202308.xml", (date(2023, 8, 1), date(2023, 8, 31))),
    ("https://www.cnn.com/sitemap/article/us/2023/08.xml", (date(2023, 8, 1), date(2023, 8, 31))),
    ("https://www.smh.com.au/sitemaps/smh-articles-20220301.xml", (date(2022, 3, 1), date(2022, 3, 1))),
    ("https://www.lismorecitynews.com.au/sitemap-2022.xml", (date(2022, 1, 1), date(2022, 12, 31))),
    ("https://www.biobiochile.cl/static/sitemap-2010-03.xml", (date(2010, 3, 1), date(2010, 3, 31))),
    ("https://www.cnn.com/sitemap/article/us.xml", None),
    ("https://kech24.com/post-sitemap198.xml", None),
])
def test_span_from_url(url, expected):
    assert span_from_url(url) == expected


def test_article_url_date():
    assert date_from_article_url("https://www.emol.com/noticias/nacional/2010/03/05/1234/x.html") == date(2010, 3, 5)
    assert date_from_article_url("https://www.smh.com.au/national/x-20220301-p5a.html") == date(2022, 3, 1)
    assert date_from_article_url("https://news.kbs.co.kr/news/view.do?ncd=3591439") is None


def test_parse_lenient_salvages_unescaped_html():
    b = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
         b'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
         b'<url><loc>https://x.cl/noticias/2011/01/02/1/a.html</loc>'
         b'<image:image><image:caption><param name="movie" value="v.swf"></image:caption></image:image></url>'
         b'<url><loc>https://x.cl/noticias/2011/01/03/2/b.html</loc><lastmod>2011-01-03</lastmod></url></urlset>')
    with pytest.raises(ET.ParseError):
        parse(b)
    kind, ents = parse_lenient(b)
    assert kind == "urlset"
    assert [e["loc"] for e in ents] == ["https://x.cl/noticias/2011/01/02/1/a.html", "https://x.cl/noticias/2011/01/03/2/b.html"]
    assert ents[1]["lastmod"] == "2011-01-03"
