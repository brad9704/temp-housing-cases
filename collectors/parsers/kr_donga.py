"""동아일보 본문 전용 파서. trafilatura가 본문 대신 '공유하기'만 뽑는 기사가 많다(표본 200건 중 50건, 2026-10-22).

본문은 section.news_view 안의 텍스트 노드와 <br> 줄바꿈이다. 사진(figure)·광고(div.view_*adK)·스크립트는 뺀다.
"""

from __future__ import annotations

import lxml.html

_DROP = ("//figure", "//script", "//style", "//*[contains(@class,'adK')]", "//*[contains(@class,'view_ad')]",
         "//*[contains(@class,'article_footer')]", "//*[contains(@class,'byline')]")


def extract(html: bytes) -> str | None:
    doc = lxml.html.fromstring(html)
    nodes = doc.xpath("//section[contains(concat(' ', normalize-space(@class), ' '), ' news_view ')]")
    if not nodes:
        return None
    sec = nodes[0]
    for xp in _DROP:
        for el in sec.xpath("." + xp):
            el.drop_tree()
    for br in sec.xpath(".//br"):
        br.tail = "\n" + (br.tail or "")
    lines = [l.strip() for l in sec.text_content().split("\n")]
    text = "\n".join(l for l in lines if l)
    return text or None
