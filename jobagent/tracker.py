import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from jobagent.models import company_key

# Lazy import at call-site to avoid network hit at module load
# (googleapiclient.discovery.build fetches the discovery doc on first call)

# Daniel's columns. The agent never renames, reorders or deletes these.
EXISTING_HEADERS = [
    "#", "תאריך הגשה", "חברה", "תפקיד",
    "סטטוס", "עדכון אחרון", "תחום", "מיקום / מקור",
]
# Columns the agent adds after the last used column if they're missing.
AGENT_HEADERS = ["ציון התאמה", "גרסת CV", "ערוץ הגשה", "קישור", "הערות", "מספר משרה"]

from jobagent.statuses import (  # noqa: E402  (re-exported: older imports use tracker.STATUS_*)
    COLORS, STATUSES, STATUS_REJECTED, STATUS_REPLIED, STATUS_SUBMITTED, VALID_STATUSES,
)
# The dropdown covers the rows Daniel preset; appended rows land inside this range.
_STATUS_ROWS = 1000

# Field name -> header text. Columns are found by header, not by letter, so
# Daniel can move them by hand without the agent writing into the wrong cell.
_HEADER_OF = dict(zip(
    ["num", "date", "company", "role", "status", "last_updated", "domain", "location_source",
     "match_pct", "cv_variant", "channel", "url", "notes", "job_number"],
    EXISTING_HEADERS + AGENT_HEADERS,
))
# Without these, dedupe and status updates can't work, so a missing one stops the agent.
_REQUIRED = ("num", "date", "company", "role", "status", "last_updated")
# The layout the sheet was created with. Only used when a caller passes no header.
_DEFAULT_COLS = {name: i for i, name in enumerate(_HEADER_OF)}


class SheetLayoutError(Exception):
    """A column the agent depends on is missing or was renamed."""


@dataclass
class ApplicationRow:
    row_num: int        # 1-based sheet row
    date: str           # DD/MM/YYYY
    company: str
    role: str
    status: str
    last_updated: str   # DD/MM/YYYY
    domain: str
    location_source: str
    match_pct: str
    cv_variant: str
    channel: str        # email | portal | LinkedIn
    url: str
    notes: str
    job_number: str = ""


@dataclass
class NewApplication:
    company: str
    role: str
    domain: str
    location_source: str
    match_pct: int
    cv_variant: str
    channel: str        # "email" | "portal" | "LinkedIn"
    url: str
    notes: str
    date: str = ""      # DD/MM/YYYY; filled by tracker if empty
    job_number: str = ""


def _build_service(sa_file: str):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        sa_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


class SheetTracker:
    """Read/write the Google Sheet job tracker."""

    _RANGE = "A:AZ"
    # A fetch cycle asks about every job, and the Sheets API allows ~60 reads a
    # minute. Reads are cached briefly so one cycle costs a handful of calls.
    _CACHE_SECONDS = 120

    def __init__(self, spreadsheet_id: str, service=None, *, sa_file: str = ""):
        self._sheet_id = spreadsheet_id
        self._svc = service or _build_service(sa_file)
        self._values = self._svc.spreadsheets().values()
        self._cache: Optional[list[ApplicationRow]] = None
        self._cols: dict[str, int] = {}
        self._cache_at = 0.0

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read_all(self) -> list[ApplicationRow]:
        """Return all data rows (row 1 is headers, skipped)."""
        now = time.monotonic()
        if self._cache is not None and now - self._cache_at < self._CACHE_SECONDS:
            return self._cache
        result = self._values.get(
            spreadsheetId=self._sheet_id,
            range=self._RANGE,
        ).execute()
        raw_rows = result.get("values", [])
        self._cols = _columns(raw_rows[0]) if raw_rows else {}
        # raw_rows[0] is the header row; data starts at raw_rows[1] → sheet row 2
        self._cache = [_parse_row(i + 2, row, self._cols) for i, row in enumerate(raw_rows[1:])]
        self._cache_at = now
        return self._cache

    def _read_fresh(self) -> list[ApplicationRow]:
        self._cache = None
        return self.read_all()

    def is_duplicate(self, company: str, role: str, job_number: str = "",
                     *, fresh: bool = False) -> bool:
        """True if this job is already in the sheet.

        The job number is the strongest match: it survives a re-titled posting
        and a company spelled differently. Company + role catches rows Daniel
        added by hand without a number. `fresh` skips the cache, for the check
        right before something is sent.
        """
        rows = self._read_fresh() if fresh else self.read_all()
        return any(
            (job_number and _norm(r.job_number) == _norm(job_number))
            or (company_key(r.company) == company_key(company) and _norm(r.role) == _norm(role))
            for r in rows
        )

    def apps_in_last_30_days(self, company: str, today: Optional[date] = None) -> int:
        """Applications to this company dated in the last 30 days, whatever their status.

        A rejected or answered application was still sent: counting only "submitted"
        rows would free a slot the moment a status changed.
        """
        today = today or date.today()
        cutoff = today - timedelta(days=30)
        count = 0
        for r in self.read_all():
            if company_key(r.company) != company_key(company):
                continue
            try:
                d = datetime.strptime(r.date, "%d/%m/%Y").date()
                if d >= cutoff:
                    count += 1
            except ValueError:
                pass
        return count

    def find_row(self, company: str, role: str) -> Optional[int]:
        """Return 1-based sheet row number, or None.

        Always reads fresh: Daniel edits by hand and this number picks the cell to write.
        """
        for r in self._read_fresh():
            if company_key(r.company) == company_key(company) and _norm(r.role) == _norm(role):
                return r.row_num
        return None

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def ensure_agent_headers(self) -> None:
        """Add missing agent headers and style them like Daniel's own headers.

        Only unstyled header cells get the style, so if Daniel restyles one by
        hand, a restart doesn't undo it.
        """
        sheet = self._svc.spreadsheets().get(
            spreadsheetId=self._sheet_id,
            ranges=["1:1"],
            includeGridData=True,
            fields="sheets(properties(sheetId),data(rowData(values(formattedValue,userEnteredFormat))))",
        ).execute()["sheets"][0]
        data = (sheet.get("data") or [{}])[0].get("rowData") or [{}]
        cells = data[0].get("values", [])
        header = [(c.get("formattedValue") or "").strip() for c in cells]
        if EXISTING_HEADERS[0] not in header:
            raise SheetLayoutError(f"header {EXISTING_HEADERS[0]!r} not found in row 1")

        missing = [h for h in AGENT_HEADERS if h not in header]
        if missing:
            start = max((i for i, h in enumerate(header) if h), default=-1) + 1
            self._values.update(
                spreadsheetId=self._sheet_id,
                range=f"{_col_letter(start)}1",
                valueInputOption="USER_ENTERED",
                body={"values": [missing]},
            ).execute()
            header = header[:start] + [""] * (start - len(header)) + missing

        source = header.index(EXISTING_HEADERS[0])
        unstyled = [i for i in (header.index(h) for h in AGENT_HEADERS)
                    if i >= len(cells) or not cells[i].get("userEnteredFormat")]
        if unstyled:
            sheet_id = sheet["properties"]["sheetId"]
            self._svc.spreadsheets().batchUpdate(
                spreadsheetId=self._sheet_id,
                body={"requests": [_copy_format(sheet_id, source, i) for i in unstyled]},
            ).execute()
        self._cache = None

    def ensure_status_options(self) -> None:
        """Make the status dropdown offer every stage, and give each stage a color.

        The dropdown is a plain list validation and the colors are conditional-format
        rules on the status column (checked on Daniel's sheet, 2026-11-11). So the
        list is replaced with the full one, and only colors for stages that have no
        rule yet are added: Daniel's three existing rules are never touched.
        """
        if not self._cols:
            self._read_fresh()
        col = self._cols["status"]
        sheet = self._svc.spreadsheets().get(
            spreadsheetId=self._sheet_id,
            ranges=[f"{_col_letter(col)}2"],
            includeGridData=True,
            fields="sheets(properties(sheetId),conditionalFormats,data(rowData(values(dataValidation))))",
        ).execute()["sheets"][0]
        sheet_id = sheet["properties"]["sheetId"]
        cell = ((sheet.get("data") or [{}])[0].get("rowData") or [{}])[0].get("values") or [{}]
        rule = (cell[0].get("dataValidation") or {}).get("condition") or {}
        current = [v.get("userEnteredValue") for v in rule.get("values") or []]
        colored = {
            v.get("userEnteredValue")
            for cf in sheet.get("conditionalFormats") or []
            if any(r.get("startColumnIndex") == col for r in cf.get("ranges") or [])
            for v in ((cf.get("booleanRule") or {}).get("condition") or {}).get("values") or []
        }
        column = {"sheetId": sheet_id, "startColumnIndex": col, "endColumnIndex": col + 1}
        requests = []
        if current != STATUSES:
            requests.append({"setDataValidation": {
                "range": {**column, "startRowIndex": 1, "endRowIndex": _STATUS_ROWS},
                "rule": {"condition": {"type": "ONE_OF_LIST",
                                       "values": [{"userEnteredValue": s} for s in STATUSES]},
                         "showCustomUi": True, "strict": False},
            }})
        for status in STATUSES:
            if status not in colored:
                requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
                    "ranges": [{**column, "startRowIndex": 1}],
                    "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": status}]},
                                    "format": {"backgroundColor": COLORS[status]}},
                }}})
        if requests:
            self._svc.spreadsheets().batchUpdate(
                spreadsheetId=self._sheet_id, body={"requests": requests}).execute()

    def append_application(self, app: NewApplication) -> int:
        """Append a new row. Returns the 1-based sheet row number."""
        self._read_fresh()  # place values by today's headers, not cached ones
        if not self._cols:
            raise SheetLayoutError("sheet has no header row")
        today_str = app.date or date.today().strftime("%d/%m/%Y")
        values = {
            "num": "=ROW()-1",  # same formula as Daniel's rows, never a number
            "date": today_str,
            "company": app.company,
            "role": app.role,
            "status": STATUS_SUBMITTED,
            "last_updated": today_str,
            "domain": app.domain,
            "location_source": app.location_source,
            "match_pct": str(app.match_pct),
            "cv_variant": app.cv_variant,
            "channel": app.channel,
            "url": app.url,
            "notes": app.notes,
            "job_number": app.job_number,
        }
        row = [""] * (max(self._cols.values()) + 1)
        for name, value in values.items():
            if name in self._cols:
                row[self._cols[name]] = value
        result = self._values.append(
            spreadsheetId=self._sheet_id,
            range="A:A",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        self._cache = None
        # updatedRange looks like "Sheet1!A42:M42" or "'שם גיליון'!A42:M42"
        updated_range = result["updates"]["updatedRange"]
        m = re.search(r"(\d+)$", updated_range)
        return int(m.group(1)) if m else -1

    def update_status(self, row_num: int, status: str, today: Optional[date] = None) -> None:
        """Update the status and last-updated cells of one row."""
        assert status in VALID_STATUSES, f"invalid status: {status!r}"
        if not self._cols:
            self._read_fresh()
        today_str = (today or date.today()).strftime("%d/%m/%Y")
        cells = {"status": status, "last_updated": today_str}
        self._values.batchUpdate(
            spreadsheetId=self._sheet_id,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": [
                    {"range": f"{_col_letter(self._cols[name])}{row_num}", "values": [[value]]}
                    for name, value in cells.items()
                ],
            },
        ).execute()
        self._cache = None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _columns(header: list[str]) -> dict[str, int]:
    """Map field names to column indexes by header text."""
    position = {}
    for i, text in enumerate(header):
        position.setdefault(text.strip(), i)
    cols = {name: position[text] for name, text in _HEADER_OF.items() if text in position}
    missing = [_HEADER_OF[name] for name in _REQUIRED if name not in cols]
    if missing:
        raise SheetLayoutError(f"sheet headers not found: {', '.join(missing)}")
    return cols


def _col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _copy_format(sheet_id: int, source_col: int, target_col: int) -> dict:
    def cell(col: int) -> dict:
        return {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": col, "endColumnIndex": col + 1}
    return {"copyPaste": {"source": cell(source_col), "destination": cell(target_col),
                          "pasteType": "PASTE_FORMAT"}}


def _parse_row(row_num: int, cells: list, cols: Optional[dict[str, int]] = None) -> ApplicationRow:
    cols = _DEFAULT_COLS if cols is None else cols

    def get(name: str) -> str:
        idx = cols.get(name)
        return cells[idx].strip() if idx is not None and idx < len(cells) else ""

    return ApplicationRow(row_num=row_num, **{name: get(name) for name in _HEADER_OF if name != "num"})


def _norm(s: str) -> str:
    return s.strip().lower()
