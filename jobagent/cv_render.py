"""Render an ATS-safe one-page CV (.docx + .pdf) from cv/master.yaml.

Only `title` and `summary` can be overridden per role. Everything else comes
from the master file, so a fact fixed once is fixed in every generated CV.

ATS rules enforced here:
  - single column, no tables, no images, no headers/footers (name is in the body)
  - real Word bullet lists, standard section names
  - intra-word hyphens are written as non-breaking hyphens, so a line break
    never lands on "fault-tolerance" and parsers never see "faulttolerance"
  - no en/em dashes anywhere (rejected at render time)
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "cv" / "master.yaml"                  # real CV, gitignored
EXAMPLE_MASTER = ROOT / "cv" / "master.example.yaml"  # fictional, committed (tests + template)

FONT = "Calibri"
BODY_PT = 10
MARGIN_IN = 0.55
PAGE_W_IN = 8.5
TEXT_W_IN = PAGE_W_IN - 2 * MARGIN_IN
BANNED_DASHES = ("–", "—")  # en, em
SUMMARY_MAX_CHARS = 600
_INTRAWORD_HYPHEN = re.compile(r"(?<=\w)-(?=\w)")


class CVError(ValueError):
    pass


@dataclass
class RenderResult:
    docx: Path
    pdf: Path | None
    pages: int | None
    text: str


# --------------------------------------------------------------------------- data

def load_master(path: Path = MASTER) -> dict:
    # No fallback to the example file on purpose: a missing real CV must stop the agent,
    # not put a fictional person's CV in front of an employer.
    if not path.exists():
        raise CVError(f"{path} is missing: copy {EXAMPLE_MASTER.name} to {path.name} and fill in your facts")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve(master: dict, variant: str, title: str | None = None,
            summary: str | None = None) -> dict:
    """Build the concrete content for one CV. Raises CVError on bad input."""
    variants = master["variants"]
    if variant not in variants:
        raise CVError(f"unknown variant {variant!r}; have {sorted(variants)}")
    v = variants[variant]

    title = (title or v["title"]).strip()
    summary = " ".join((summary or v["summary"]).split())
    for label, text in (("title", title), ("summary", summary)):
        if any(d in text for d in BANNED_DASHES):
            raise CVError(f"{label} contains an en/em dash")
    if len(summary) > SUMMARY_MAX_CHARS:
        raise CVError(f"summary too long ({len(summary)} > {SUMMARY_MAX_CHARS} chars)")

    groups = master["skill_groups"]
    skills = []
    for gid in v["skills"]:
        if gid not in groups:
            raise CVError(f"variant {variant}: unknown skill group {gid!r}")
        skills.append((groups[gid]["label"], groups[gid]["items"]))

    max_b = v.get("max_bullets", {})
    projects, skipped = [], []
    for pid in v["projects"]:
        p = master["projects"].get(pid)
        if p is None:
            raise CVError(f"variant {variant}: unknown project {pid!r}")
        if p.get("verified", True) is False:
            skipped.append(pid)
            continue
        bullets = p["bullets"][: max_b.get(pid, len(p["bullets"]))]
        projects.append({**p, "bullets": bullets})

    return {
        "contact": master["contact"],
        "title": title,
        "summary": summary,
        "skills": skills,
        "experience": master["experience"],
        "projects": projects,
        "skipped_unverified": skipped,
        "education": master["education"],
        "coursework_key": v.get("coursework", "systems"),
        "military": master.get("military"),
    }


def allowed_facts_text(master: dict, variant: str) -> str:
    """Plain-text dump of every verified fact a variant may draw on.

    This is what the LLM gets when it writes a per-role summary, so it has no
    way to see (or echo) unverified projects.
    """
    c = resolve(master, variant)
    lines = [f"Experience: {e['role']} at {e['org']} ({e['years']})" for e in c["experience"]]
    for e in c["experience"]:
        lines += [f"  - {b}" for b in e["bullets"]]
    for p in c["projects"]:
        lines.append(f"Project: {p['name']} ({p['tech']})")
        lines += [f"  - {b}" for b in p["bullets"]]
    for label, items in c["skills"]:
        lines.append(f"Skills / {label}: {', '.join(items)}")
    for ed in c["education"]:
        lines.append(f"Education: {ed['degree']}, {ed['org']} ({ed['years']})")
        if ed.get("note"):
            lines.append(f"  - {ed['note']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- docx helpers

def _add_text(par, text: str, *, bold=False, italic=False, size=None, color=None):
    """Add a run, converting intra-word hyphens to non-breaking hyphens."""
    run = par.add_run()
    run.bold, run.italic = bold, italic
    run.font.name = FONT
    if size:
        run.font.size = Pt(size)
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    for i, part in enumerate(_INTRAWORD_HYPHEN.split(text)):
        if i:
            run._r.append(OxmlElement("w:noBreakHyphen"))
        if part:
            t = OxmlElement("w:t")
            t.set(qn("xml:space"), "preserve")
            t.text = part
            run._r.append(t)
    return run


def _tight(par, before=0, after=0, line=1.0):
    pf = par.paragraph_format
    pf.space_before, pf.space_after, pf.line_spacing = Pt(before), Pt(after), line
    return par


def _bottom_border(par):
    pPr = par._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for k, val in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "808080")):
        bottom.set(qn(k), val)
    bdr.append(bottom)
    pPr.append(bdr)


def _heading(doc, text):
    p = _tight(doc.add_paragraph(), before=6, after=2)
    _add_text(p, text.upper(), bold=True, size=10.5)
    _bottom_border(p)


def _entry_line(doc, left_bold, left_rest, right):
    p = _tight(doc.add_paragraph(), before=2, after=0)
    p.paragraph_format.tab_stops.add_tab_stop(Inches(TEXT_W_IN), WD_TAB_ALIGNMENT.RIGHT)
    _add_text(p, left_bold, bold=True)
    if left_rest:
        _add_text(p, f" - {left_rest}")
    if right:
        p.add_run("\t")
        _add_text(p, right)


def _bullet(doc, text):
    p = _tight(doc.add_paragraph(style="List Bullet"), after=0)
    p.paragraph_format.left_indent = Inches(0.22)
    p.paragraph_format.first_line_indent = Inches(-0.15)
    _add_text(p, text)


def _plain(doc, text, *, align=None, size=None, bold=False, after=0):
    p = _tight(doc.add_paragraph(), after=after)
    if align:
        p.alignment = align
    _add_text(p, text, size=size, bold=bold)
    return p


# --------------------------------------------------------------------------- render

def build_docx(c: dict) -> Document:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(PAGE_W_IN), Inches(11)
    for side in ("left_margin", "right_margin"):
        setattr(sec, side, Inches(MARGIN_IN))
    sec.top_margin = sec.bottom_margin = Inches(0.45)

    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = FONT, Pt(BODY_PT)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    doc.styles["List Bullet"].font.size = Pt(BODY_PT)

    ct = c["contact"]
    _plain(doc, ct["name"], align=WD_ALIGN_PARAGRAPH.CENTER, size=16, bold=True)
    _plain(doc, c["title"], align=WD_ALIGN_PARAGRAPH.CENTER, size=10.5)
    contact = " • ".join([ct["location"], ct["phone"], ct["email"], ct["linkedin"], ct["github"]])
    _plain(doc, contact, align=WD_ALIGN_PARAGRAPH.CENTER, size=9, after=2)

    _heading(doc, "Summary")
    _plain(doc, c["summary"])

    _heading(doc, "Technical Skills")
    for label, items in c["skills"]:
        p = _tight(doc.add_paragraph())
        _add_text(p, f"{label}: ", bold=True)
        _add_text(p, ", ".join(items))

    _heading(doc, "Experience")
    for e in c["experience"]:
        _entry_line(doc, e["role"], e["org"], e["years"])
        for b in e["bullets"]:
            _bullet(doc, b)

    _heading(doc, "Projects")
    for pr in c["projects"]:
        _entry_line(doc, pr["name"], pr["tech"], None)
        for b in pr["bullets"]:
            _bullet(doc, b)

    _heading(doc, "Education")
    for ed in c["education"]:
        _entry_line(doc, ed["degree"], ed["org"], ed["years"])
        if "coursework" in ed:
            cw = ", ".join(ed["coursework"][c["coursework_key"]])
            _bullet(doc, f"Relevant coursework: {cw}. Top grades: {ed['grades']}.")
        if ed.get("note"):
            _bullet(doc, ed["note"])

    if c.get("military"):
        m = c["military"]
        _heading(doc, "Military Service")
        _entry_line(doc, m["role"], m["org"], m["years"])
        _bullet(doc, m["note"])

    return doc


# One conversion at a time. CVs and prep sheets render from different threads, and
# a second soffice on the same user profile hands off to the first and can exit 0
# without writing a file.
_SOFFICE_LOCK = threading.Lock()


def _to_pdf(docx_path: Path) -> Path:
    pdf = docx_path.with_suffix(".pdf")
    with _SOFFICE_LOCK:
        pdf.unlink(missing_ok=True)  # so a stale PDF can't pass for a fresh one
        subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(docx_path.parent), str(docx_path)],
            check=True, capture_output=True, timeout=120,
        )
    if not pdf.exists():
        raise CVError(f"LibreOffice wrote no PDF for {docx_path.name}")
    return pdf


def _pdf_pages_and_text(pdf: Path) -> tuple[int, str]:
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True).stdout
    pages = int(re.search(r"Pages:\s+(\d+)", info).group(1))
    text = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    return pages, text


def render(variant: str, out_dir: Path, stem: str | None = None, *, title=None, summary=None,
           pdf=True, master_path: Path = MASTER) -> RenderResult:
    master = load_master(master_path)
    c = resolve(master, variant, title, summary)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or f"Daniel_Novojilovsky_CV_{variant}"
    docx_path = out_dir / f"{stem}.docx"
    build_docx(c).save(docx_path)

    if not pdf:
        return RenderResult(docx_path, None, None, "")
    pdf_path = _to_pdf(docx_path)
    pages, text = _pdf_pages_and_text(pdf_path)
    if any(d in text for d in BANNED_DASHES):
        raise CVError("rendered PDF contains an en/em dash")
    if pages != 1:
        raise CVError(f"{pdf_path.name} is {pages} pages; must be 1")
    return RenderResult(docx_path, pdf_path, pages, text)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("variant")
    ap.add_argument("--out", type=Path, default=ROOT / "out")
    ap.add_argument("--stem")
    ap.add_argument("--title")
    ap.add_argument("--summary")
    ap.add_argument("--no-pdf", action="store_true")
    a = ap.parse_args(argv)
    master = load_master()
    skipped = resolve(master, a.variant)["skipped_unverified"]
    if skipped:
        print(f"note: skipped unverified projects: {', '.join(skipped)}", file=sys.stderr)
    r = render(a.variant, a.out, a.stem, title=a.title, summary=a.summary, pdf=not a.no_pdf)
    print(r.docx)
    if r.pdf:
        print(r.pdf, f"({r.pages} page)")


if __name__ == "__main__":
    main()
