"""개인정보 마스킹 (구호기관 문서·뉴스 공통).

redact(text, region) -> (가린 텍스트, 발견 목록)
- 이메일 → [EMAIL]
- 전화번호 → [PHONE]. libphonenumber로 국가별 번호 체계를 판정해
  휴대전화(MOBILE, FIXED_LINE_OR_MOBILE, PERSONAL_NUMBER)는 `possible_personal=True`로 표시한다.
  업무용 휴대전화와 개인 휴대전화는 구분할 수 없으므로 알리기 전에 사람이 확인한다.
- 참석자 명단 구간(Attendees/Participants 등 제목 아래)은 [ATTENDEE LIST REMOVED]로 바꾼다.

발견 목록에는 원문 번호를 넣지 않는다. 국가 코드와 끝 두 자리만 남긴 가린 번호를 쓴다.
원문 번호는 내부 보관 원본(PDF·HTML)에만 남는다.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

import phonenumbers
from phonenumbers import PhoneNumberType as T

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PERSONAL_TYPES = {T.MOBILE, T.FIXED_LINE_OR_MOBILE, T.PERSONAL_NUMBER}
_TYPE_NAME = {v: k for k, v in vars(T).items() if isinstance(v, int)}

# 제목 줄만 잡는다: 용어만 있는 줄이거나 "용어:" 형태. "Present in Syria since…", "Participants were…" 같은
# 본문 문장은 잡지 않는다(2026-10-22, IFRC 보고서 본문이 최대 120줄씩 지워지던 오탐 수정)
_ATTENDEE_HEAD = re.compile(
    r"^[ \t]*(list of\s+)?(attendees|attendance(\s+list)?|participants(\s+list)?|persons?\s+present|present|"
    r"liste de pr[ée]sence|asistentes|participantes|المشاركون|الحضور)[ \t]*(\(\d+\))?[ \t]*(?P<inline>:.*)?$",
    re.I | re.M,
)
_SECTION_HEAD = re.compile(
    r"^\s*((\d+|[IVX]+)[.)]\s+\S|agenda\b|action points?\b|minutes\b|discussion\b|summary\b|"
    r"ordre du jour\b|points? d'action\b|agenda del d[ií]a\b)", re.I)
_MAX_ATTENDEE_LINES = 120
# 명단 줄: 가린 연락처가 있거나, 대문자로 시작하는 2~4단어 이름 뒤에 쉼표·괄호·대시·줄끝이 오는 짧은 줄
_NAME_LINE = re.compile(
    r"\[EMAIL\]|\[PHONE\]|^\s*(\d+[.)]\s*)?((Mr|Ms|Mrs|Dr|Mme|M)\.?\s+)?"
    r"[A-ZÀ-Þ][\w'’.-]+(\s+[A-ZÀ-Þ][\w'’.-]+){1,3}\s*([,(–—-]|$)")
_PROSE_WORDS = 12  # 이 단어 수 이상이고 연락처가 없는 줄은 본문으로 보고 제거를 멈춘다


def _is_name_line(line: str) -> bool:
    return bool(_NAME_LINE.search(line)) and (len(line.split()) < _PROSE_WORDS or "[EMAIL]" in line or "[PHONE]" in line)


def _is_prose(line: str) -> bool:
    return len(line.split()) >= _PROSE_WORDS and "[EMAIL]" not in line and "[PHONE]" not in line


@dataclass
class Finding:
    kind: str                 # email | phone | attendee_list
    number_type: str | None = None
    possible_personal: bool = False
    masked: str | None = None
    char_offset: int | None = None   # 가리기 전 원문 기준


def _mask_number(n: phonenumbers.PhoneNumber) -> str:
    nat = str(n.national_number)
    return f"+{n.country_code} " + "•" * max(0, len(nat) - 2) + nat[-2:]


def _drop_attendee_lists(text: str, findings: list[Finding]) -> str:
    out, i = [], 0
    for m in _ATTENDEE_HEAD.finditer(text):
        if m.start() < i:
            continue
        rest = text[m.end():].split("\n")
        # 표의 "Participants" 열 제목 같은 오탐을 거른다: "제목: 내용" 형태가 아니면 다음 4줄 중 2줄 이상이 명단 줄이어야 한다
        inline = bool((m.group("inline") or "").strip(" :\t"))
        ahead = [l for l in rest[1:12] if l.strip()][:4]
        if not inline and sum(_is_name_line(l) for l in ahead) < 2:
            continue
        out.append(text[i:m.start()])
        n = 1  # 제목 다음 줄부터
        while (n < len(rest) and n <= _MAX_ATTENDEE_LINES and not _SECTION_HEAD.match(rest[n])
               and not _is_prose(rest[n])):
            n += 1
        consumed = len("\n".join(rest[:n]))
        out.append("[ATTENDEE LIST REMOVED]")
        findings.append(Finding(kind="attendee_list", char_offset=m.start()))
        i = m.end() + consumed
    out.append(text[i:])
    return "".join(out)


def redact(text: str, region: str | None = None) -> tuple[str, list[dict]]:
    findings: list[Finding] = []
    for m in EMAIL_RE.finditer(text):
        findings.append(Finding(kind="email", char_offset=m.start()))
    spans = []
    for m in phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.VALID):
        t = phonenumbers.number_type(m.number)
        findings.append(Finding(kind="phone", number_type=_TYPE_NAME.get(t, str(t)),
                                possible_personal=t in _PERSONAL_TYPES,
                                masked=_mask_number(m.number), char_offset=m.start))
        spans.append((m.start, m.end))
    # 뒤에서부터 바꿔야 offset이 안 밀린다
    for s, e in sorted(spans, reverse=True):
        text = text[:s] + "[PHONE]" + text[e:]
    text = EMAIL_RE.sub("[EMAIL]", text)
    text = _drop_attendee_lists(text, findings)
    return text, [asdict(f) for f in findings]
