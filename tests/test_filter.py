from pipeline.filter import Filter, normalize

F = Filter()


def test_tier1_passes_alone():
    r = F.match("포항시는 흥해읍에 임시 조립주택 50동을 짓기로 했다.", "ko")
    assert r["pass"] and r["tier"] == "tier1"


def test_tier2_needs_disaster_context():
    assert not F.match("대피소 위치 안내 표지판을 새로 달았다.", "ko")["pass"]
    r = F.match("지진 이재민 200여 명이 흥해실내체육관 대피소에서 지내고 있다.", "ko")
    assert r["pass"] and r["tier"] == "tier2"


def test_tier2_window_does_not_cross_far_text():
    far = "대피소 안내." + " 가" * 400 + "\n지진 소식."
    assert not F.match(far, "ko")["pass"]


def test_case_sensitive_abbreviation():
    assert F.match("Le casette SAE consegnate ai terremotati di Amatrice", "it")["pass"]
    assert not F.match("la sae della provincia, terremoto", "it")["pass"]


def test_arabic_normalization():
    assert normalize("مساكن مؤقتة", ["arabic_taa_marbuta"]) == "مساكن مؤقته"
    assert F.match("مساكن مؤقتة للمتضررين من الزلزال", "ar")["pass"]


def test_turkish_lower():
    assert normalize("KONTEYNER KENT", ["turkish_lower"]) == "konteyner kent"
    assert F.match("Hatay'da KONTEYNER KENT kuruldu", "tr")["pass"]
