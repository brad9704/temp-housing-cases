import pytest

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


@pytest.mark.parametrize("line", [
    "Present in Syria since 1967, the ICRC is a neutral, impartial, and independent organization.",
    "Participants were given choices between group or individual sessions.",
    "present the objectives and scope of the intervention. Community meetings have also been held.",
    "Participants of Lessons Learnt",
])
def test_prose_is_not_an_attendee_heading(line):
    text = f"Intro.\n{line}\nShelter kits were late because of road damage.\n"
    red, finds = redact(text)
    assert "Shelter kits were late" in red
    assert not [f for f in finds if f["kind"] == "attendee_list"]


@pytest.mark.parametrize("head", ["Participants:", "ATTENDEES", "List of participants", "Attendance (12)", "Participants: UNHCR, IOM"])
def test_attendee_heading_forms(head):
    text = f"Meeting notes\n{head}\nJohn Doe, NGO A\nJane Roe, NGO B\n1. Updates\nShelter gaps remain.\n"
    red, finds = redact(text)
    assert "John Doe" not in red and "Shelter gaps remain." in red
    assert [f for f in finds if f["kind"] == "attendee_list"]


def test_training_table_participants_column_is_kept():
    text = ("Training Type Branch Duration Date # Men # Women # Total\nParticipants\n"
            "First aid Kabul 3 days 12/03 10 12 22\nPSS Herat 2 days 14/03 8 9 17\n"
            "The trainings were delayed because the venue was used as an evacuation centre for flood-affected families.\n")
    red, finds = redact(text)
    assert red == text
    assert not [f for f in finds if f["kind"] == "attendee_list"]


def test_attendee_removal_stops_at_prose():
    text = ("Participants\nJohn Doe, NGO A\nJane Roe, NGO B\n"
            "The collective centre still has no separate toilets for women and the cluster asked for funding.\n")
    red, _ = redact(text)
    assert "John Doe" not in red and "separate toilets" in red


def test_inline_attendee_heading_alone_is_removed():
    red, finds = redact("Notes\nParticipants: UNHCR (J. Doe), IOM (R. Roe)\n1. Updates\nGaps remain.\n")
    assert "J. Doe" not in red and "Gaps remain." in red
    assert [f for f in finds if f["kind"] == "attendee_list"]
