import pytest

from jobagent.recruiter import recruiter_email


def test_finds_address_in_hebrew_posting():
    text = "לשליחת קורות חיים: Jobs@Example.co.il בהצלחה"
    assert recruiter_email("jobify", text) == "jobs@example.co.il"


def test_no_address_means_portal_flow():
    assert recruiter_email("jobify", "Apply on our site") == ""
    assert recruiter_email("jobify", "") == ""


@pytest.mark.parametrize("address", [
    "noreply@x.com", "no-reply@x.com", "privacy@x.com", "accommodations@x.com",
    "applicantaccommodations@x.com", "support@x.com", "recruitingfraud@x.com",
])
def test_skips_addresses_that_are_not_for_cvs(address):
    assert recruiter_email("jobify", f"Contact {address} for help") == ""


def test_skips_boilerplate_and_keeps_the_real_address():
    text = "Questions: privacy@x.com. Send your CV to talent@x.com"
    assert recruiter_email("jobify", text) == "talent@x.com"


@pytest.mark.parametrize("source", ["workday:nvidia", "apple", "meta", "microsoft"])
def test_first_party_portals_never_use_email(source):
    """An address in these postings is HR boilerplate, not a way to apply."""
    assert recruiter_email(source, "Send your CV to talent@x.com") == ""


def test_linkedin_posting_can_name_a_recruiter():
    assert recruiter_email("linkedin_email", "CVs to dana@x.com") == "dana@x.com"
