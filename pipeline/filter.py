"""정규식 2단 필터 (원문 언어 기준). 사전은 codebook/regex_v0.yaml.

    from pipeline.filter import Filter
    Filter().match(text, "ko") -> {"pass": bool, "tier": "tier1"|"tier2"|None, "hits": [...]}

- tier1 패턴은 단독 통과.
- tier2 패턴은 같은 문단 안이나 앞뒤 window_chars 글자 안에 disaster 패턴이 있을 때만 통과.
- 언어별 normalize 전처리 후 매칭한다(NFKC, 터키어 소문자, 아랍어 알리프·타마르부타 통일, 타슈킬 제거).
- 기본 IGNORECASE. case_sensitive_patterns에 든 패턴만 대소문자를 구분한다.
- 사전을 바꾸지 않는다. 사전 변경은 재현율 감사 결과로만 한다(codebook/CHANGELOG.md).
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import regex
import yaml

DICT = Path("codebook/regex_v0.yaml")
_TASHKEEL = regex.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")


def normalize(text: str, steps: list[str]) -> str:
    """NFKC·타슈킬 제거는 글자 수를 바꾸므로 hits의 span은 정규화 텍스트 기준이다."""
    for s in steps:
        if s == "nfkc":
            text = unicodedata.normalize("NFKC", text)
        elif s == "turkish_lower":
            text = text.replace("I", "ı").replace("İ", "i").lower()
        elif s == "arabic_alef":
            text = regex.sub("[أإآ]", "ا", text)
        elif s == "arabic_taa_marbuta":
            text = text.replace("ة", "ه")
        elif s == "strip_tashkeel":
            text = _TASHKEEL.sub("", text)
    return text


class Filter:
    def __init__(self, path: Path = DICT):
        cfg = yaml.safe_load(path.read_text())
        self.version = path.stem
        self.window = int(cfg["settings"]["window_chars"])
        self.langs = {}
        for lang, c in cfg.items():
            if lang == "settings":
                continue
            cs = set(c.get("case_sensitive_patterns") or [])

            def comp(p: str) -> regex.Pattern:
                return regex.compile(p, 0 if p in cs else regex.IGNORECASE)

            self.langs[lang] = {
                "normalize": c.get("normalize") or [],
                "tier1": [(e["pattern"], comp(e["pattern"]), e.get("stage_hint")) for e in c.get("tier1") or []],
                "tier2": [(e["pattern"], comp(e["pattern"]), e.get("stage_hint")) for e in c.get("tier2") or []],
                "disaster": comp(c["disaster"]),
            }

    def match(self, text: str, lang: str) -> dict:
        L = self.langs[lang]
        t = normalize(text, L["normalize"])
        hits = []
        for p, rx, hint in L["tier1"]:
            for m in rx.finditer(t):
                hits.append({"tier": "tier1", "pattern": p, "stage_hint": hint, "span": [m.start(), m.end()], "text": m.group()})
        dis = [m.span() for m in L["disaster"].finditer(t)]
        paras = [m.span() for m in regex.finditer(r"[^\n]+", t)]
        for p, rx, hint in L["tier2"]:
            for m in rx.finditer(t):
                a, b = m.span()
                para = next(((x, y) for x, y in paras if x <= a < y), (a, b))
                ok = any((para[0] <= da and db <= para[1]) or (da < b + self.window and db > a - self.window) for da, db in dis)
                if ok:
                    hits.append({"tier": "tier2", "pattern": p, "stage_hint": hint, "span": [a, b], "text": m.group()})
        tier = "tier1" if any(h["tier"] == "tier1" for h in hits) else ("tier2" if hits else None)
        return {"pass": bool(hits), "tier": tier, "hits": hits, "dict_version": self.version}
