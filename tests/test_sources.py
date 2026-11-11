"""Tests for all scraper parse functions, using fixture files."""
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jobagent.filters import passes_location
from jobagent.models import JobRecord, job_number
from jobagent.sources import apple, jobify, jsonld, linkedin, meta, microsoft, workday

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Workday
# ---------------------------------------------------------------------------

def _nvidia_config():
    return workday.WorkdayCompany(
        company="Nvidia",
        tenant="nvidia",
        host="nvidia.wd5.myworkdayjobs.com",
        site="NVIDIAExternalCareerSite",
    )


def test_workday_parse_israel_job():
    data = json.loads((FIXTURES / "workday_nvidia.json").read_text())
    cfg = _nvidia_config()
    jobs = [workday.parse_posting(p, cfg) for p in data["jobPostings"]]
    jobs = [j for j in jobs if j]
    israel = [j for j in jobs if "Israel" in j.location]
    assert len(israel) == 2
    assert any(j.title == "Software Engineer, Networking" for j in israel)


def test_workday_parse_constructs_url():
    posting = {
        "title": "SWE",
        "locationsText": "Israel, Tel Aviv",
        "postedOn": "Posted 3 Days Ago",
        "externalPath": "/job/Israel/SWE_JR1",
        "bulletFields": [],
    }
    job = workday.parse_posting(posting, _nvidia_config())
    assert job is not None
    assert job.url == "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Israel/SWE_JR1"


def test_workday_parse_missing_external_path_returns_none():
    posting = {"title": "SWE", "locationsText": "Israel"}
    assert workday.parse_posting(posting, _nvidia_config()) is None


def test_workday_parse_empty_title_returns_none():
    posting = {"title": "", "externalPath": "/job/x", "locationsText": "Israel"}
    assert workday.parse_posting(posting, _nvidia_config()) is None


def test_workday_parse_description_from_bullets():
    posting = {
        "title": "SWE",
        "locationsText": "Israel",
        "externalPath": "/job/IL/SWE_JR1",
        "bulletFields": ["C++ required", "Linux experience"],
    }
    job = workday.parse_posting(posting, _nvidia_config())
    assert "C++ required" in job.description
    assert "Linux experience" in job.description


def test_workday_parse_posted_date():
    posting = {
        "title": "SWE",
        "locationsText": "Israel",
        "externalPath": "/job/IL/SWE_JR1",
        "bulletFields": [],
        "postedOn": "Posted 5 Days Ago",
    }
    job = workday.parse_posting(posting, _nvidia_config())
    assert job.posted_date  # should be a non-empty ISO date


def test_workday_fetch_company_dedupes():
    """fetch_company should not return duplicate URLs across location_terms."""
    cfg = workday.WorkdayCompany(
        company="Nvidia", tenant="nvidia",
        host="nvidia.wd5.myworkdayjobs.com", site="NVIDIAExternalCareerSite",
        location_terms=["Israel", "Remote"],
    )
    fixture = json.loads((FIXTURES / "workday_nvidia.json").read_text())

    client = MagicMock()
    resp = MagicMock()
    resp.json.return_value = fixture
    client.post.return_value = resp

    jobs = workday.fetch_company(client, cfg)
    urls = [j.url for j in jobs]
    assert len(urls) == len(set(urls))


# ---------------------------------------------------------------------------
# Microsoft (pcsx API; fixtures cut from real responses, 2026-11-11)
# ---------------------------------------------------------------------------

def _ms_fixture(name):
    return json.loads((FIXTURES / name).read_text())["data"]


def test_microsoft_parses_search_page():
    data = _ms_fixture("microsoft_pcsx_search.json")
    jobs = [microsoft.parse_position(p) for p in data["positions"]]
    assert len(jobs) == 3 and all(jobs)
    first = data["positions"][0]
    assert jobs[0].title == first["name"].strip()
    assert all("Israel" in j.location and passes_location(j.location) for j in jobs)
    assert jobs[0].url == (f"https://apply.careers.microsoft.com/careers/job/{first['id']}"
                           f"?job_number={first['displayJobId']}")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", jobs[0].posted_date)


def test_microsoft_job_number_is_the_public_one():
    """The sheet and dedupe need the number the posting shows, not Eightfold's id."""
    first = _ms_fixture("microsoft_pcsx_search.json")["positions"][0]
    job = microsoft.parse_position(first)
    assert job_number(job.url) == first["displayJobId"]


def test_microsoft_paginates_until_count(monkeypatch):
    monkeypatch.setattr(microsoft, "SITES", microsoft.SITES[:1])
    page = {"data": {"positions": [{"id": 1, "name": "SWE", "displayJobId": "9"}], "count": 21}}
    client = MagicMock()
    client.get.return_value.json.return_value = page
    microsoft.fetch(client)
    assert [c.kwargs["params"]["start"] for c in client.get.call_args_list] == [0, 10, 20]


def test_microsoft_description_from_detail():
    text = microsoft.parse_description(_ms_fixture("microsoft_pcsx_detail.json"))
    assert len(text) > 1000 and "<" not in text and "Microsoft" in text


def test_microsoft_description_uses_internal_id():
    client = MagicMock()
    client.get.return_value.json.return_value = {"data": {"jobDescription": "<p>Hi</p>"}}
    job = JobRecord(source="microsoft", company="Microsoft", title="SWE", location="Israel",
                    url="https://apply.careers.microsoft.com/careers/job/1970393557038901?job_number=200061418")
    assert microsoft.fetch_description(client, job) == "Hi"
    assert client.get.call_args.kwargs["params"]["position_id"] == "1970393557038901"


def test_microsoft_skips_positions_without_title_or_id():
    assert microsoft.parse_position({"id": 1}) is None
    assert microsoft.parse_position({"name": "SWE"}) is None


# ---------------------------------------------------------------------------
# Apple (data embedded in the server-rendered page; fixtures cut from real pages)
# ---------------------------------------------------------------------------

def test_apple_parses_search_page():
    search = apple.loader_data((FIXTURES / "apple_search_page.html").read_text())["search"]
    jobs = [apple.parse_result(r) for r in search["searchResults"]]
    assert len(jobs) == 3 and all(jobs)
    first = search["searchResults"][0]
    assert jobs[0].url == f"https://jobs.apple.com/en-il/details/{first['id']}"
    assert job_number(jobs[0].url) == first["id"]
    assert all(passes_location(j.location) for j in jobs)
    assert jobs[0].location.endswith(", Israel")


def test_apple_paginates_until_total():
    def page(n):
        rows = [{"id": f"{n}-1", "postingTitle": "SWE", "locations": []}]
        lit = json.dumps(json.dumps({"loaderData": {"search": {"searchResults": rows, "totalRecords": 45}}}))
        return MagicMock(text=f"<script>window.__staticRouterHydrationData = JSON.parse({lit});</script>")
    client = MagicMock()
    client.get.side_effect = lambda url, params: page(params["page"])
    assert len(apple.fetch(client)) == 3  # 45 results at 20 a page


def test_apple_description_from_detail_page():
    text = apple.parse_detail((FIXTURES / "apple_detail_page.html").read_text())
    assert "Responsibilities" in text and "Minimum Qualifications" in text


def test_apple_hydration_survives_quotes_in_the_data():
    data = {"loaderData": {"search": {"searchResults": [], "totalRecords": 0, "note": 'say "hi"); x'}}}
    html = f"<script>window.__staticRouterHydrationData = JSON.parse({json.dumps(json.dumps(data))});</script>"
    assert apple.loader_data(html)["search"]["note"] == 'say "hi"); x'


def test_apple_page_without_data_raises():
    with pytest.raises(ValueError):
        apple.loader_data("<html>maintenance</html>")


def test_apple_skips_results_without_title_or_id():
    assert apple.parse_result({"id": "1"}) is None
    assert apple.parse_result({"postingTitle": "SWE"}) is None


def test_apple_parse_missing_id_returns_none():
    assert apple.parse_result({"postingTitle": "SWE", "locations": []}) is None


# ---------------------------------------------------------------------------
# Jobify
# ---------------------------------------------------------------------------

def test_jobify_parse_returns_two_jobs():
    html = (FIXTURES / "jobify_page.html").read_text()
    jobs = jobify.parse_page(html)
    assert len(jobs) == 2  # third card has no href → skipped


def test_jobify_parse_titles():
    html = (FIXTURES / "jobify_page.html").read_text()
    jobs = jobify.parse_page(html)
    titles = [j.title for j in jobs]
    assert "C++ Developer" in titles
    assert "Systems Software Engineer" in titles


def test_jobify_parse_company():
    html = (FIXTURES / "jobify_page.html").read_text()
    jobs = {j.title: j for j in jobify.parse_page(html)}
    assert jobs["C++ Developer"].company == "Check Point"


def test_jobify_parse_location():
    html = (FIXTURES / "jobify_page.html").read_text()
    jobs = {j.title: j for j in jobify.parse_page(html)}
    # Location comes from the Hebrew city text in the card
    assert jobs["C++ Developer"].location  # non-empty


def test_jobify_parse_url():
    html = (FIXTURES / "jobify_page.html").read_text()
    jobs = {j.title: j for j in jobify.parse_page(html)}
    assert jobs["C++ Developer"].url == "https://jobify360.co.il/jobs/123-in"
    assert jobs["Systems Software Engineer"].url == "https://jobify360.co.il/jobs/456-in"


def test_jobify_parse_empty_html():
    assert jobify.parse_page("<html><body></body></html>") == []


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

def test_meta_parse_results():
    data = json.loads((FIXTURES / "meta_graphql.json").read_text())
    jobs = [meta.parse_result(r) for r in data["data"]["job_search"]["results"]]
    jobs = [j for j in jobs if j]
    assert len(jobs) == 2


def test_meta_parse_uses_apply_link():
    item = {
        "id": "abc123",
        "title": "SWE",
        "location": "Tel Aviv",
        "apply_link": "https://www.metacareers.com/jobs/abc123/",
    }
    job = meta.parse_result(item)
    assert job.url == "https://www.metacareers.com/jobs/abc123/"


def test_meta_parse_fallback_url():
    item = {"id": "abc123", "title": "SWE", "location": "Tel Aviv"}
    job = meta.parse_result(item)
    assert "abc123" in job.url


def test_meta_parse_missing_title_returns_none():
    assert meta.parse_result({"id": "1", "location": "IL"}) is None


def test_meta_parse_missing_id_returns_none():
    assert meta.parse_result({"title": "SWE", "location": "IL"}) is None


# ---------------------------------------------------------------------------
# Workday: pagination and real locations
# ---------------------------------------------------------------------------

def _page(n, total, start=0):
    return {"total": total, "jobPostings": [
        {"title": f"SWE {start + i}", "locationsText": "Israel, Haifa",
         "externalPath": f"/job/IL/SWE_{start + i}", "bulletFields": []}
        for i in range(n)
    ]}


class _FakeClient:
    """Serves queued POST pages and GET details, recording what was asked."""

    def __init__(self, pages=(), detail=None):
        self.pages = list(pages)
        self.detail = detail
        self.got = []

    def post(self, url, json=None):
        return MagicMock(json=lambda: self.pages.pop(0))

    def get(self, url):
        self.got.append(url)
        if isinstance(self.detail, Exception):
            raise self.detail
        return MagicMock(json=lambda: self.detail)


def _single_term():
    return workday.WorkdayCompany(company="KLA", tenant="kla", host="kla.wd1.myworkdayjobs.com",
                                  site="Search", location_terms=["Israel"])


def test_workday_paginates_past_page_two():
    """Workday sends the real total on page 1 only, then 0."""
    pages = [_page(20, 60, 0), _page(20, 0, 20), _page(20, 0, 40)]
    jobs = workday.fetch_company(_FakeClient(pages), _single_term())
    assert len(jobs) == 60


def test_workday_stops_on_empty_page():
    pages = [_page(20, 60, 0), _page(0, 0)]
    assert len(workday.fetch_company(_FakeClient(pages), _single_term())) == 20


def test_workday_stops_when_total_reached_even_if_api_repeats():
    """Intel's tenant ignores offset and repeats the same page."""
    client = _FakeClient([_page(16, 16)])
    assert len(workday.fetch_company(client, _single_term())) == 16


def test_workday_caps_broad_searches():
    per_page = [_page(20, 100000, i * 20) for i in range(40)]
    jobs = workday.fetch_company(_FakeClient(per_page), _single_term())
    assert len(jobs) == workday.MAX_RESULTS_PER_TERM


def _multi_location_page():
    page = _page(1, 1)
    page["jobPostings"][0]["locationsText"] = "2 Locations"
    return page


def test_workday_resolves_real_locations_instead_of_trusting_search_term():
    """This job matched "Remote" as text but is really in California."""
    detail = {"jobPostingInfo": {
        "location": "Milpitas, CA",
        "additionalLocations": ["Virtual/Remote USA"],
        "jobDescription": "<p>Real description</p>",
    }}
    client = _FakeClient([_multi_location_page()], detail)
    job = workday.fetch_company(client, _single_term())[0]
    assert job.location == "Milpitas, CA; Virtual/Remote USA"
    assert job.description == "Real description"
    assert client.got[0].startswith("https://kla.wd1.myworkdayjobs.com/wday/cxs/kla/Search/")


def test_workday_failed_detail_keeps_placeholder_so_filter_drops_it():
    client = _FakeClient([_multi_location_page()], RuntimeError("boom"))
    job = workday.fetch_company(client, _single_term())[0]
    assert job.location == "2 Locations"


def test_workday_skips_detail_call_for_known_jobs():
    client = _FakeClient([_multi_location_page()], {"jobPostingInfo": {}})
    url = workday.WorkdayCompany.job_url(_single_term(), "/job/IL/SWE_0")
    workday.fetch_company(client, _single_term(), known_urls={url})
    assert client.got == []


def test_workday_skips_detail_call_for_senior_titles():
    page = _multi_location_page()
    page["jobPostings"][0]["title"] = "Senior SWE"
    client = _FakeClient([page], {"jobPostingInfo": {}})
    workday.fetch_company(client, _single_term())
    assert client.got == []


# ---------------------------------------------------------------------------
# Jobify and LinkedIn descriptions
# ---------------------------------------------------------------------------

def test_jobify_stamps_israel_on_hebrew_cities():
    """Jobify is Israel-only, so its jobs must always pass the location filter."""
    jobs = jobify.parse_page((FIXTURES / "jobify_page.html").read_text())
    assert all("Israel" in j.location for j in jobs)
    assert all(passes_location(j.location) for j in jobs)
    by_title = {j.title: j for j in jobs}
    assert by_title["C++ Developer"].location == "תל אביב - יפו, Israel"


def test_jobify_fetch_description_from_job_page():
    page = (FIXTURES / "jobify_job.html").read_bytes()
    client = MagicMock()
    client.get.return_value = MagicMock(content=page)
    job = JobRecord(source="jobify", company="X", title="Y", location="Israel", url="u")
    desc = jobify.fetch_description(client, job)
    assert "ניסיון של 2 שנים" in desc
    assert "<" not in desc


def test_jobify_fetch_description_returns_empty_on_error():
    client = MagicMock()
    client.get.side_effect = RuntimeError("down")
    job = JobRecord(source="jobify", company="X", title="Y", location="Israel", url="u")
    assert jobify.fetch_description(client, job) == ""


def test_linkedin_prefers_json_ld():
    page = (FIXTURES / "linkedin_job.html").read_text(encoding="utf-8")
    assert "C++ on Linux" in linkedin.parse_description(page)


def test_linkedin_falls_back_to_visible_block():
    page = '<div class="show-more-less-html__markup"><p>Visible text</p></div>'
    assert linkedin.parse_description(page) == "Visible text"


def test_linkedin_blocked_page_returns_empty():
    assert linkedin.parse_description("<html>Sign in</html>") == ""


def test_jsonld_skips_other_types_and_bad_blocks():
    page = (
        '<script type="application/ld+json">not json</script>'
        '<script type="application/ld+json">{"@type": "FAQPage", "description": "no"}</script>'
        '<script type="application/ld+json">{"@type": "JobPosting", "description": "yes"}</script>'
    )
    assert jsonld.job_description(page) == "yes"


def test_jsonld_unescapes_double_encoded_html():
    assert jsonld.clean_html("&lt;p&gt;Fish &amp;amp; chips&lt;/p&gt;") == "Fish &amp; chips"


def test_page_text_decodes_utf8_without_charset_header():
    """`resp.text` would guess ISO-8859-1 here and garble the apostrophe."""
    resp = MagicMock(content="it’s תל אביב".encode("utf-8"))
    assert jsonld.page_text(resp) == "it’s תל אביב"


# ---------------------------------------------------------------------------
# Greenhouse, SmartRecruiters, Amazon (fixtures cut from real responses, 2026-11-11)
# ---------------------------------------------------------------------------

from jobagent.sources import amazon, greenhouse, smartrecruiters  # noqa: E402


def test_greenhouse_parses_board_and_keeps_israel_jobs():
    data = json.loads((FIXTURES / "greenhouse_list.json").read_text())
    board = greenhouse.Board("JFrog", "jfrog")
    jobs = [greenhouse.parse_job(board, item) for item in data["jobs"]]
    assert all(jobs) and {j.company for j in jobs} == {"JFrog"}
    israel = [j for j in jobs if passes_location(j.location)]
    assert len(israel) >= 2
    item = data["jobs"][-1]
    assert jobs[-1].url == f"https://job-boards.greenhouse.io/jfrog/jobs/{item['id']}"
    assert job_number(jobs[-1].url) == str(item["id"])


def test_greenhouse_description_is_unescaped_text():
    data = json.loads((FIXTURES / "greenhouse_job.json").read_text())
    text = greenhouse.parse_content(data["content"])
    assert len(text) > 500 and "&lt;" not in text and "<p>" not in text


def test_greenhouse_description_call_uses_token_and_id():
    client = MagicMock()
    client.get.return_value.json.return_value = {"content": "&lt;p&gt;Hi&lt;/p&gt;"}
    job = JobRecord(source="greenhouse:jfrog", company="JFrog", title="SWE", location="Israel",
                    url="https://job-boards.greenhouse.io/jfrog/jobs/7939091")
    assert greenhouse.fetch_description(client, job) == "Hi"
    assert client.get.call_args.args[0] == "https://boards-api.greenhouse.io/v1/boards/jfrog/jobs/7939091"


def test_greenhouse_one_broken_board_doesnt_hide_the_others(monkeypatch):
    monkeypatch.setattr(greenhouse, "BOARDS", [greenhouse.Board("A", "a"), greenhouse.Board("B", "b")])
    client = MagicMock()
    ok = MagicMock(); ok.json.return_value = {"jobs": [{"id": 1, "title": "SWE", "location": {"name": "Israel"}}]}
    client.get.side_effect = [RuntimeError("404"), ok]
    assert [j.company for j in greenhouse.fetch(client)] == ["B"]


def test_smartrecruiters_parses_postings():
    data = json.loads((FIXTURES / "smartrecruiters_list.json").read_text())
    co = smartrecruiters.Company("Check Point", "CheckPointSoftwareTechnologies2")
    jobs = [smartrecruiters.parse_posting(co, item) for item in data["content"]]
    assert all(jobs) and all(passes_location(j.location) for j in jobs)
    item = data["content"][0]
    assert jobs[0].url == f"https://jobs.smartrecruiters.com/CheckPointSoftwareTechnologies2/{item['id']}"
    assert job_number(jobs[0].url) == item["id"]


def test_smartrecruiters_description_from_job_ad():
    data = json.loads((FIXTURES / "smartrecruiters_job.json").read_text())
    text = smartrecruiters.parse_job_ad(data)
    assert len(text) > 300 and "<" not in text


def test_smartrecruiters_paginates_until_total():
    client = MagicMock()
    page = {"totalFound": 150, "content": [{"id": str(i), "name": "SWE"} for i in range(100)]}
    last = {"totalFound": 150, "content": [{"id": str(i), "name": "SWE"} for i in range(50)]}
    client.get.return_value.json.side_effect = [page, last]
    assert len(smartrecruiters._fetch_company(client, smartrecruiters.COMPANIES[0])) == 150
    assert [c.kwargs["params"]["offset"] for c in client.get.call_args_list] == [0, 100]


def test_amazon_parses_search_with_description():
    data = json.loads((FIXTURES / "amazon_search.json").read_text())
    jobs = [amazon.parse_job(item) for item in data["jobs"]]
    assert all(jobs) and all(passes_location(j.location) for j in jobs)
    item = data["jobs"][0]
    assert jobs[0].url == f"https://www.amazon.jobs/en/jobs/{item['id_icims']}"
    assert job_number(jobs[0].url) == item["id_icims"]
    assert len(jobs[0].description) > 100        # no second call needed
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", jobs[0].posted_date)


def test_amazon_company_names():
    assert amazon._company("Annapurna Labs Ltd.") == "Annapurna Labs"
    assert amazon._company("AWS EMEA SARL (Israel Branch)") == "Amazon"


from jobagent.sources import comeet  # noqa: E402


def test_comeet_parses_positions_with_descriptions():
    data = json.loads((FIXTURES / "comeet_positions.json").read_text())
    board = comeet.Board("Cardo", "cardosystems", "F2.003")
    jobs = [comeet.parse_position(board, p) for p in data]
    assert all(jobs) and {j.company for j in jobs} == {"Cardo"}
    israeli = [j for j in jobs if passes_location(j.location)]
    assert israeli and all(j.location.endswith(", Israel") for j in israeli)
    assert "Requirements" in jobs[0].description and len(jobs[0].description) > 300
    assert job_number(jobs[0].url) == data[0]["uid"]


def test_comeet_reads_the_token_from_the_page():
    client = MagicMock()
    page = MagicMock(text='<script>var c = {"token":"ABCDEF1234567890"};</script>')
    api = MagicMock(); api.json.return_value = []
    client.get.side_effect = [page, api]
    comeet.fetch(client)
    assert client.get.call_args.kwargs["params"]["token"] == "ABCDEF1234567890"


def test_eightfold_site_sets_company_and_host():
    item = {"id": 77, "name": "PHY Engineer", "displayJobId": "3080000", "locations": ["Kfar-Netter, Israel"]}
    qualcomm = next(s for s in microsoft.SITES if s.source == "qualcomm")
    job = microsoft.parse_position(item, qualcomm)
    assert job.company == "Qualcomm" and job.url.startswith("https://careers.qualcomm.com/careers/job/77")
    assert job_number(job.url) == "3080000"
