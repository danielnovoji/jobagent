"""Application stages for the tracker sheet's status column (Daniel, 2026-11-11).

The first three are Daniel's original dropdown values and colors; the rest were
added so a row can follow an application through its stages. One list here feeds
the sheet dropdown, the reply classifier and the Telegram buttons.
"""
from __future__ import annotations

STATUS_SUBMITTED  = "הוגש"
STATUS_REPLIED    = "התקבלה תשובה"
STATUS_SCREENING  = "שיחת היכרות"
STATUS_ASSIGNMENT = "מבחן / מטלה"
STATUS_INTERVIEW  = "מחכה לראיון"  # Daniel's own wording
STATUS_OFFER      = "הצעת עבודה"
STATUS_REJECTED   = "נדחה"
STATUS_WITHDREW   = "ויתרתי"

# Dropdown order: the pipeline, then the two ways it ends.
STATUSES = [STATUS_SUBMITTED, STATUS_REPLIED, STATUS_SCREENING, STATUS_ASSIGNMENT,
            STATUS_INTERVIEW, STATUS_OFFER, STATUS_REJECTED, STATUS_WITHDREW]
VALID_STATUSES = set(STATUSES)
# No more emails are expected, so the reply scan stops watching these rows.
CLOSED_STATUSES = {STATUS_REJECTED, STATUS_WITHDREW}
# Stages where the interview prep sheet is worth putting back on top.
PREP_STATUSES = {STATUS_REPLIED, STATUS_SCREENING, STATUS_ASSIGNMENT, STATUS_INTERVIEW}

# Short codes for Telegram buttons (callback data is capped at 64 bytes, and Hebrew
# is 2 bytes a letter). Codes, not list positions, so reordering can't break old buttons.
CODES = {"sub": STATUS_SUBMITTED, "rep": STATUS_REPLIED, "scr": STATUS_SCREENING,
         "tst": STATUS_ASSIGNMENT, "int": STATUS_INTERVIEW, "off": STATUS_OFFER,
         "rej": STATUS_REJECTED, "wd": STATUS_WITHDREW}
CODE_OF = {status: code for code, status in CODES.items()}

# Background colors for the sheet. The first three match Daniel's existing rules,
# which are left as they are; only missing ones are added.
COLORS = {
    STATUS_SUBMITTED:  {"red": 1.0, "green": 0.949, "blue": 0.8},
    STATUS_REPLIED:    {"red": 0.776, "green": 0.937, "blue": 0.808},
    STATUS_SCREENING:  {"red": 0.788, "green": 0.886, "blue": 0.973},
    STATUS_ASSIGNMENT: {"red": 0.851, "green": 0.824, "blue": 0.957},
    STATUS_INTERVIEW:  {"red": 0.988, "green": 0.851, "blue": 0.698},
    STATUS_OFFER:      {"red": 0.576, "green": 0.851, "blue": 0.596},
    STATUS_REJECTED:   {"red": 1.0, "green": 0.780, "blue": 0.808},
    STATUS_WITHDREW:   {"red": 0.851, "green": 0.851, "blue": 0.851},
}
