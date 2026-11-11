from datetime import date
from unittest.mock import MagicMock, call

import pytest

from jobagent.tracker import (
    AGENT_HEADERS,
    EXISTING_HEADERS,
    STATUS_REJECTED,
    STATUS_REPLIED,
    STATUS_SUBMITTED,
    ApplicationRow,
    NewApplication,
    SheetLayoutError,
    SheetTracker,
    _col_letter,
    _norm,
    _parse_row,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_service(rows: list[list[str]], *, append_range: str = "Sheet1!A5:M5") -> MagicMock:
    """Build a mock Google Sheets service that returns `rows` on values.get."""
    svc = MagicMock()
    values = svc.spreadsheets.return_value.values.return_value
    # get: always return rows (caller provides header row as rows[0])
    values.get.return_value.execute.return_value = {"values": rows}
    # append
    values.append.return_value.execute.return_value = {
        "updates": {"updatedRange": append_range}
    }
    # update
    values.update.return_value.execute.return_value = {}
    return svc


HEADER_ROW = [
    "#", "תאריך הגשה", "חברה", "תפקיד",
    "סטטוס", "עדכון אחרון", "תחום", "מיקום / מקור",
    "ציון התאמה", "גרסת CV", "ערוץ הגשה", "קישור", "הערות",
]

EXISTING_ROWS = [
    HEADER_ROW,
    ["1", "01/01/2025", "Acme", "C++ Dev", "הוגש", "01/01/2025", "systems", "Israel", "80", "systems", "portal", "https://example.com/1", "good match"],
    ["2", "05/01/2025", "Globex", "SW Engineer", "נדחה", "10/01/2025", "systems", "Israel", "70", "systems", "email", "https://example.com/2", ""],
    ["3", "15/10/2025", "Intel", "Systems Dev", "הוגש", "15/10/2025", "systems", "Israel", "75", "systems", "email", "https://example.com/3", ""],
]


def make_tracker(rows=None) -> tuple[SheetTracker, MagicMock]:
    rows = rows if rows is not None else EXISTING_ROWS
    svc = make_service(rows)
    tracker = SheetTracker("sheet-id", service=svc)
    return tracker, svc


# ---------------------------------------------------------------------------
# _parse_row
# ---------------------------------------------------------------------------

def test_parse_row_full():
    cells = ["1", "01/01/2025", "Nvidia", "SWE", "הוגש", "01/01/2025", "systems", "Israel", "82", "systems", "email", "https://url", "notes"]
    row = _parse_row(2, cells)
    assert row.company == "Nvidia"
    assert row.role == "SWE"
    assert row.match_pct == "82"
    assert row.row_num == 2


def test_parse_row_short_cells_defaults_to_empty():
    # Row with only the first 4 columns filled (older manually-entered row)
    cells = ["1", "01/01/2025", "Initech", "Dev"]
    row = _parse_row(2, cells)
    assert row.company == "Initech"
    assert row.match_pct == ""
    assert row.notes == ""


# ---------------------------------------------------------------------------
# read_all
# ---------------------------------------------------------------------------

def test_read_all_skips_header():
    tracker, _ = make_tracker()
    rows = tracker.read_all()
    assert len(rows) == 3  # 3 data rows, header skipped
    assert rows[0].company == "Acme"
    assert rows[0].row_num == 2  # header is row 1, first data row is row 2


def test_read_all_empty_sheet():
    tracker, _ = make_tracker(rows=[HEADER_ROW])
    assert tracker.read_all() == []


def test_read_all_no_values_key():
    svc = MagicMock()
    svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {}
    tracker = SheetTracker("id", service=svc)
    assert tracker.read_all() == []


# ---------------------------------------------------------------------------
# is_duplicate
# ---------------------------------------------------------------------------

def test_is_duplicate_exact_match():
    tracker, _ = make_tracker()
    assert tracker.is_duplicate("Acme", "C++ Dev") is True


def test_is_duplicate_case_insensitive():
    tracker, _ = make_tracker()
    assert tracker.is_duplicate("acme", "c++ dev") is True


def test_is_duplicate_no_match():
    tracker, _ = make_tracker()
    assert tracker.is_duplicate("Nvidia", "SWE") is False


def test_is_duplicate_same_company_different_role():
    tracker, _ = make_tracker()
    assert tracker.is_duplicate("Intel", "Backend Dev") is False


# ---------------------------------------------------------------------------
# apps_in_last_30_days
# ---------------------------------------------------------------------------

def test_apps_in_last_30_days_counts_recent(monkeypatch):
    tracker, _ = make_tracker()
    # Intel row is 15/10/2025; today is 31/10/2025 — within 30 days
    result = tracker.apps_in_last_30_days("Intel", today=date(2025, 10, 31))
    assert result == 1


def test_apps_in_last_30_days_excludes_old():
    tracker, _ = make_tracker()
    # Acme row is 01/01/2025 — way outside 30 days from 31/10/2025
    result = tracker.apps_in_last_30_days("Acme", today=date(2025, 10, 31))
    assert result == 0


def test_apps_in_last_30_days_unknown_company():
    tracker, _ = make_tracker()
    assert tracker.apps_in_last_30_days("UnknownCorp", today=date(2025, 10, 31)) == 0


def test_apps_in_last_30_days_malformed_date_skipped():
    rows = [HEADER_ROW, ["1", "not-a-date", "Acme", "Dev", "הוגש", "", "", "", "", "", "", "", ""]]
    tracker, _ = make_tracker(rows)
    assert tracker.apps_in_last_30_days("Acme", today=date(2025, 10, 31)) == 0


# ---------------------------------------------------------------------------
# find_row
# ---------------------------------------------------------------------------

def test_find_row_returns_correct_row_num():
    tracker, _ = make_tracker()
    assert tracker.find_row("Intel", "Systems Dev") == 4  # 3rd data row → sheet row 4


def test_find_row_missing_returns_none():
    tracker, _ = make_tracker()
    assert tracker.find_row("Nobody", "No Role") is None


# ---------------------------------------------------------------------------
# ensure_agent_headers
# ---------------------------------------------------------------------------

STYLE = {"backgroundColor": {"red": 0.12, "green": 0.23, "blue": 0.37}, "textFormat": {"bold": True}}


def _row1(headers, styled):
    """Row 1 as spreadsheets.get returns it. `styled`: indexes that carry a format."""
    cells = [{"formattedValue": h, **({"userEnteredFormat": STYLE} if i in styled else {})}
             for i, h in enumerate(headers)]
    return {"sheets": [{"properties": {"sheetId": 7}, "data": [{"rowData": [{"values": cells}]}]}]}


def _header_tracker(headers, styled):
    svc = make_service([HEADER_ROW])
    svc.spreadsheets.return_value.get.return_value.execute.return_value = _row1(headers, styled)
    return SheetTracker("id", service=svc), svc


def _formatted_columns(svc):
    if not svc.spreadsheets.return_value.batchUpdate.called:
        return []
    body = svc.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
    return [r["copyPaste"]["destination"]["startColumnIndex"] for r in body["requests"]]


def test_ensure_agent_headers_adds_only_missing_after_last_column():
    tracker, svc = _header_tracker(EXISTING_HEADERS + AGENT_HEADERS[:-1], styled=range(13))
    tracker.ensure_agent_headers()
    update = svc.spreadsheets.return_value.values.return_value.update.call_args.kwargs
    assert update["range"] == "N1"
    assert update["body"]["values"] == [["מספר משרה"]]


def test_ensure_agent_headers_copies_daniels_style_onto_unstyled_headers():
    """Daniel's headers are navy and bold; the agent's were plain."""
    tracker, svc = _header_tracker(EXISTING_HEADERS + AGENT_HEADERS[:-1], styled=range(8))
    tracker.ensure_agent_headers()
    assert _formatted_columns(svc) == [8, 9, 10, 11, 12, 13]
    request = svc.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"][0]
    assert request["copyPaste"]["pasteType"] == "PASTE_FORMAT"
    assert request["copyPaste"]["source"]["startColumnIndex"] == 0
    assert request["copyPaste"]["source"]["sheetId"] == 7


def test_ensure_agent_headers_leaves_hand_styled_headers_alone():
    tracker, svc = _header_tracker(EXISTING_HEADERS + AGENT_HEADERS, styled=range(14))
    tracker.ensure_agent_headers()
    svc.spreadsheets.return_value.values.return_value.update.assert_not_called()
    assert _formatted_columns(svc) == []


def test_ensure_agent_headers_refuses_a_sheet_without_daniels_headers():
    tracker, _ = _header_tracker(["something else"], styled=())
    with pytest.raises(SheetLayoutError):
        tracker.ensure_agent_headers()


# ---------------------------------------------------------------------------
# append_application
# ---------------------------------------------------------------------------

def test_append_returns_row_number():
    svc = make_service(EXISTING_ROWS, append_range="Sheet1!A10:M10")
    tracker = SheetTracker("id", service=svc)
    app = NewApplication(
        company="Nvidia", role="SWE", domain="systems",
        location_source="Israel", match_pct=82, cv_variant="systems",
        channel="email", url="https://example.com/nv", notes="good fit",
        date="31/10/2025",
    )
    row_num = tracker.append_application(app)
    assert row_num == 10


def test_append_sends_correct_values():
    svc = make_service(EXISTING_ROWS)
    tracker = SheetTracker("id", service=svc)
    values = svc.spreadsheets.return_value.values.return_value

    app = NewApplication(
        company="KLA", role="Algo Dev", domain="systems",
        location_source="Israel", match_pct=75, cv_variant="systems",
        channel="portal", url="https://kla.com/job", notes="C++ match",
        date="31/10/2025",
    )
    tracker.append_application(app)

    call_kwargs = values.append.call_args.kwargs
    sent_row = call_kwargs["body"]["values"][0]
    assert sent_row[0] == "=ROW()-1"      # formula
    assert sent_row[1] == "31/10/2025"    # date
    assert sent_row[2] == "KLA"           # company
    assert sent_row[4] == "הוגש"          # status
    assert sent_row[8] == "75"            # match %
    assert call_kwargs["valueInputOption"] == "USER_ENTERED"


def test_append_uses_today_when_date_empty(monkeypatch):
    svc = make_service(EXISTING_ROWS)
    tracker = SheetTracker("id", service=svc)
    values = svc.spreadsheets.return_value.values.return_value

    monkeypatch.setattr("jobagent.tracker.date", type("FakeDate", (), {
        "today": staticmethod(lambda: date(2025, 10, 31)),
        "strftime": date.strftime,
    }))

    app = NewApplication(
        company="X", role="Y", domain="d", location_source="l",
        match_pct=60, cv_variant="systems", channel="email",
        url="u", notes="n",  # date="" (default)
    )
    tracker.append_application(app)
    sent_row = values.append.call_args.kwargs["body"]["values"][0]
    assert sent_row[1] == "31/10/2025"


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------

def _status_writes(svc):
    body = svc.spreadsheets.return_value.values.return_value.batchUpdate.call_args.kwargs["body"]
    assert body["valueInputOption"] == "USER_ENTERED"
    return {d["range"]: d["values"][0][0] for d in body["data"]}


def test_update_status_writes_correct_cells():
    tracker, svc = make_tracker()
    tracker.update_status(5, STATUS_REJECTED, today=date(2025, 10, 31))
    assert _status_writes(svc) == {"E5": "נדחה", "F5": "31/10/2025"}


def test_update_status_follows_moved_columns():
    moved = ["#", "סטטוס", "תאריך הגשה", "חברה", "תפקיד", "עדכון אחרון"]
    tracker, svc = make_tracker([moved])
    tracker.update_status(5, STATUS_REJECTED, today=date(2025, 10, 31))
    assert _status_writes(svc) == {"B5": "נדחה", "F5": "31/10/2025"}


def test_update_status_invalid_raises():
    tracker, _ = make_tracker()
    with pytest.raises(AssertionError):
        tracker.update_status(2, "invalid status")


# ---------------------------------------------------------------------------
# _norm
# ---------------------------------------------------------------------------

def test_norm():
    assert _norm("  Nvidia  ") == "nvidia"
    assert _norm("C++ Dev") == "c++ dev"


# ---------------------------------------------------------------------------
# Read caching: a cycle asks about every job, the Sheets API allows ~60 reads/min
# ---------------------------------------------------------------------------

def _caching_tracker():
    svc = make_service(rows=[EXISTING_HEADERS])
    values = svc.spreadsheets.return_value.values.return_value
    return SheetTracker("sheet", service=svc), values


def test_repeated_questions_cost_one_api_read():
    tracker, values = _caching_tracker()
    for _ in range(50):
        tracker.is_duplicate("Nvidia", "SWE")
        tracker.apps_in_last_30_days("Nvidia")
    assert values.get.return_value.execute.call_count == 1


def test_find_row_always_reads_fresh():
    """It picks the cell to write, and Daniel edits rows by hand."""
    tracker, values = _caching_tracker()
    tracker.read_all()
    tracker.find_row("Nvidia", "SWE")
    assert values.get.return_value.execute.call_count == 2


def test_append_invalidates_cache():
    tracker, values = _caching_tracker()
    tracker.read_all()
    tracker.append_application(NewApplication(
        company="Nvidia", role="SWE", domain="systems", location_source="Israel",
        match_pct=80, cv_variant="systems", channel="portal", url="u", notes="n"))
    tracker.read_all()
    # read, then append re-reads the headers, then the read after it is fresh
    assert values.get.return_value.execute.call_count == 3


# ---------------------------------------------------------------------------
# Job numbers and header-based columns
# ---------------------------------------------------------------------------

HEADER_WITH_NUMBER = HEADER_ROW + ["מספר משרה"]
ROWS_WITH_NUMBER = [
    HEADER_WITH_NUMBER,
    ["1", "01/11/2026", "Nvidia", "SWE", "הוגש", "01/11/2026", "", "", "", "", "", "", "", "JR2025751"],
    ["2", "01/11/2026", "Initech", "Dev", "נדחה", "01/11/2026"],  # hand-added, no number
]


def test_duplicate_by_job_number_even_if_title_changed():
    tracker, _ = make_tracker(ROWS_WITH_NUMBER)
    assert tracker.is_duplicate("NVIDIA Corp", "Software Engineer II", "JR2025751") is True


def test_job_number_doesnt_match_rows_without_one():
    tracker, _ = make_tracker(ROWS_WITH_NUMBER)
    assert tracker.is_duplicate("Intel", "SWE", "") is False


def test_hand_added_row_still_caught_by_company_and_role():
    tracker, _ = make_tracker(ROWS_WITH_NUMBER)
    assert tracker.is_duplicate("initech", "dev", "12345") is True


def test_fresh_duplicate_check_rereads_the_sheet():
    tracker, svc = make_tracker(ROWS_WITH_NUMBER)
    tracker.is_duplicate("X", "Y")
    tracker.is_duplicate("X", "Y", fresh=True)
    assert svc.spreadsheets.return_value.values.return_value.get.return_value.execute.call_count == 2


def test_append_writes_job_number_under_its_header():
    svc = make_service(ROWS_WITH_NUMBER)
    tracker = SheetTracker("id", service=svc)
    tracker.append_application(NewApplication(
        company="KLA", role="Dev", domain="systems", location_source="Israel", match_pct=70,
        cv_variant="systems", channel="portal", url="u", notes="n", job_number="2638486-2"))
    sent = svc.spreadsheets.return_value.values.return_value.append.call_args.kwargs["body"]["values"][0]
    assert sent[HEADER_WITH_NUMBER.index("מספר משרה")] == "2638486-2"


def test_columns_found_by_header_after_daniel_moves_them():
    moved = ["#", "חברה", "תאריך הגשה", "תפקיד", "סטטוס", "עדכון אחרון", "מספר משרה"]
    svc = make_service([moved, ["1", "Nvidia", "01/11/2026", "SWE", "הוגש", "01/11/2026", "JR1"]])
    tracker = SheetTracker("id", service=svc)
    row = tracker.read_all()[0]
    assert (row.company, row.date, row.job_number) == ("Nvidia", "01/11/2026", "JR1")

    tracker.append_application(NewApplication(
        company="KLA", role="Dev", domain="d", location_source="l", match_pct=70,
        cv_variant="systems", channel="portal", url="u", notes="n", date="07/11/2026",
        job_number="JR2"))
    sent = svc.spreadsheets.return_value.values.return_value.append.call_args.kwargs["body"]["values"][0]
    assert sent == ["=ROW()-1", "KLA", "07/11/2026", "Dev", "הוגש", "07/11/2026", "JR2"]


def test_renamed_required_header_stops_the_agent():
    """Silently reading "" as every company would break dedupe."""
    renamed = ["#", "תאריך הגשה", "שם החברה", "תפקיד", "סטטוס", "עדכון אחרון"]
    tracker, _ = make_tracker([renamed])
    with pytest.raises(SheetLayoutError, match="חברה"):
        tracker.read_all()


def test_append_to_sheet_without_headers_raises():
    tracker, _ = make_tracker([])
    with pytest.raises(SheetLayoutError):
        tracker.append_application(NewApplication(
            company="X", role="Y", domain="d", location_source="l", match_pct=1,
            cv_variant="systems", channel="portal", url="u", notes="n"))


@pytest.mark.parametrize("index,letter", [(0, "A"), (4, "E"), (13, "N"), (25, "Z"), (26, "AA")])
def test_col_letter(index, letter):
    assert _col_letter(index) == letter


def test_apps_in_last_30_days_counts_any_status():
    """A rejected or answered application was still sent; it keeps its slot."""
    rows = [HEADER_ROW,
            ["1", "20/10/2025", "Acme", "Dev", STATUS_REJECTED, "", "", "", "", "", "", "", ""],
            ["2", "21/10/2025", "Acme", "QA", "התקבלה תשובה", "", "", "", "", "", "", "", ""]]
    tracker, _ = make_tracker(rows)
    assert tracker.apps_in_last_30_days("Acme", today=date(2025, 10, 31)) == 2


# ---------------------------------------------------------------------------
# ensure_status_options (dropdown + colors)
# ---------------------------------------------------------------------------

def _sheet_meta(values, colored):
    return {"sheets": [{
        "properties": {"sheetId": 7},
        "data": [{"rowData": [{"values": [{"dataValidation": {"condition": {
            "type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in values]}}}]}]}],
        "conditionalFormats": [{"ranges": [{"startColumnIndex": 4, "endColumnIndex": 5}],
                                "booleanRule": {"condition": {"type": "NUMBER_EQ",
                                                              "values": [{"userEnteredValue": c}]}}}
                               for c in colored],
    }]}


def test_ensure_status_options_adds_stages_and_only_missing_colors():
    from jobagent.statuses import STATUSES
    tracker, svc = make_tracker()
    old = ["הוגש", "נדחה", "התקבלה תשובה"]
    svc.spreadsheets.return_value.get.return_value.execute.return_value = _sheet_meta(old, old)
    tracker.ensure_status_options()
    reqs = svc.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
    rule = reqs[0]["setDataValidation"]
    assert [v["userEnteredValue"] for v in rule["rule"]["condition"]["values"]] == STATUSES
    assert rule["range"]["startColumnIndex"] == 4 and rule["range"]["startRowIndex"] == 1
    added = [r["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
             for r in reqs[1:]]
    assert sorted(added) == sorted(set(STATUSES) - set(old))   # Daniel's 3 rules untouched


def test_ensure_status_options_is_a_no_op_when_done():
    from jobagent.statuses import STATUSES
    tracker, svc = make_tracker()
    svc.spreadsheets.return_value.get.return_value.execute.return_value = _sheet_meta(STATUSES, STATUSES)
    tracker.ensure_status_options()
    svc.spreadsheets.return_value.batchUpdate.assert_not_called()


def test_update_status_accepts_every_stage():
    from jobagent.statuses import STATUS_INTERVIEW
    tracker, svc = make_tracker()
    tracker.update_status(3, STATUS_INTERVIEW, today=date(2026, 11, 11))
    data = svc.spreadsheets.return_value.values.return_value.batchUpdate.call_args.kwargs["body"]["data"]
    assert [d["values"][0][0] for d in data] == [STATUS_INTERVIEW, "11/11/2026"]
