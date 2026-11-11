import pytest

from jobagent.models import job_number


@pytest.mark.parametrize("url,number", [
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Israel-Yokneam/"
     "Networking-QA-and-Automation-Engineer--Network-Systems_JR2025751", "JR2025751"),
    ("https://intel.wd1.myworkdayjobs.com/en-US/External/job/Israel-Haifa/"
     "Formal-Verification-Engineer_JR0287702", "JR0287702"),
    ("https://kla.wd1.myworkdayjobs.com/en-US/Search/job/Yavne-Israel/Integrator_2638486-2", "2638486-2"),
    ("https://www.linkedin.com/jobs/view/4485055993/", "4485055993"),
    ("https://jobify360.co.il/jobs/2776094-jk", "2776094-jk"),
    ("https://jobify360.co.il/jobs/373_13922-emp", "373_13922-emp"),  # "_" only splits on Workday
    ("https://jobs.careers.microsoft.com/global/en/job/1789012/", "1789012"),
    ("https://jobs.apple.com/en-il/details/200594231/c-c-software-engineer", "200594231"),
    ("https://www.metacareers.com/jobs/987654321/", "987654321"),
    ("https://example.com/careers/software-engineer", ""),
])
def test_job_number_from_url(url, number):
    assert job_number(url) == number


def test_job_number_prefers_an_explicit_param():
    url = "https://apply.careers.microsoft.com/careers/job/1970393557038901?job_number=200061418"
    assert job_number(url) == "200061418"


@pytest.mark.parametrize("a, b", [
    ("Check Point Software Technologies Ltd.", "Check Point"),
    ("Applied Materials - Israel", "Applied Materials"),
    ("NVIDIA", "Nvidia"),
    ("Wiz Inc.", "Wiz"),
    ("Plus500™", "Plus500"),
])
def test_company_key_matches_one_employer_across_sources(a, b):
    from jobagent.models import company_key
    assert company_key(a) == company_key(b)


def test_company_key_keeps_different_employers_apart():
    from jobagent.models import company_key
    assert company_key("Annapurna Labs") != company_key("Amazon")
    assert company_key("Check Point") != company_key("Point Software")
