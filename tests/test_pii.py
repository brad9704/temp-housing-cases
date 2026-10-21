from pipeline.pii import redact

DOC = """Cebu Shelter Cluster Meeting Minutes
Attendees:
Jane Roe, NGO A, jane.roe@example.org
John Doe, NGO B
1. Agenda
Contact the focal point at +63 917 123 4567 or the office at +63 32 412 3456.
Overcrowding reported in two evacuation centres.
"""


def test_redact_masks_and_flags_mobile():
    out, f = redact(DOC, region="PH")
    assert "jane.roe@example.org" not in out and "917 123 4567" not in out and "412 3456" not in out
    assert "[ATTENDEE LIST REMOVED]" in out and "Jane Roe" not in out
    assert "Overcrowding reported" in out
    phones = [x for x in f if x["kind"] == "phone"]
    assert len(phones) == 2
    mobile = [x for x in phones if x["possible_personal"]]
    assert len(mobile) == 1 and mobile[0]["masked"].endswith("67") and "917" not in mobile[0]["masked"]
    assert all("4567" not in (x["masked"] or "") for x in f)
