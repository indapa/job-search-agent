import csv
import re
from datetime import date
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from .state import JobPosting

CSV_FIELDS = [
    "relevance_score", "matches_criteria", "title", "company", "location", "work_arrangement", "employment_type",
    "seniority", "salary", "posted_date", "application_deadline", "posting_status", "url", "requirements",
    "description", "source_urls", "status", "notes",
]

_MATCH_ORDER = {"yes": 0, "unclear": 1, "no": 2}


def is_closed(j: JobPosting) -> bool:
    return j.posting_status == "closed"


def sort_jobs(jobs: list[JobPosting]) -> list[JobPosting]:
    return sorted(jobs, key=lambda j: (_MATCH_ORDER[j.matches_criteria], is_closed(j), -j.relevance_score, j.title))


def read_csv(path: Path) -> list[JobPosting]:
    """Load jobs back from a CSV this module wrote, so a report can be rebuilt without searching again."""
    jobs = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            fields = {k: v for k, v in row.items() if k in JobPosting.model_fields and v != ""}
            fields["source_urls"] = row.get("source_urls", "").split()
            jobs.append(JobPosting(**fields))
    return jobs


def write_csv(jobs: list[JobPosting], path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for j in sort_jobs(jobs):
            row = j.model_dump()
            row["source_urls"] = " ".join(j.source_urls)
            row["status"] = ""  # for tracking applications: applied / interviewing / offer / rejected
            row["notes"] = ""
            w.writerow({k: row.get(k, "") if row.get(k) is not None else "" for k in CSV_FIELDS})


def report_sections(jobs: list[JobPosting]) -> tuple[str, list[tuple[str, str, list]]]:
    """The report's summary line and its (heading, blurb, jobs) sections, in display order."""
    ranked = sort_jobs(jobs)
    matches = [j for j in ranked if j.matches_criteria == "yes"]
    unclear_all = [j for j in ranked if j.matches_criteria == "unclear"]
    excluded = [j for j in jobs if j.matches_criteria == "no"]

    strong = [j for j in matches if not is_closed(j)]
    unclear = [j for j in unclear_all if not is_closed(j)]
    closed = [j for j in ranked if j.matches_criteria != "no" and is_closed(j)]

    summary = (
        f"Found {len(jobs)} postings: {len(matches)} match your criteria, {len(unclear_all)} are unclear "
        f"and {len(excluded)} don't match (those are in the CSV only). "
        f"{len(closed)} of the rest look closed or stale."
    )
    sections = [
        ("Strong matches", "", strong),
        ("Worth a look (fit unclear)", "The posting didn't say clearly whether it fits every criterion.", unclear),
        ("Possibly closed", "Old or closed postings. The company may still be hiring for similar roles.", closed),
    ]
    return summary, [s for s in sections if s[2]]


# Unicode TTF fonts to try, in order; fpdf's built-in fonts only cover Latin-1
_FONT_CANDIDATES = [
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
]
_FALLBACK_FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"

_INK, _MUTED, _ACCENT, _WARN = (33, 37, 41), (108, 117, 125), (31, 78, 121), (176, 42, 55)


class _ReportPDF(FPDF):
    def __init__(self, title: str):
        super().__init__(format="Letter")
        self.report_title = title
        self.set_margins(18, 16, 18)
        self.set_auto_page_break(True, margin=16)
        self.body_font = self._load_font()

    def _load_font(self) -> str:
        for regular, bold in _FONT_CANDIDATES:
            if Path(regular).exists() and Path(bold).exists():
                self.add_font("Body", "", regular)
                self.add_font("Body", "B", bold)
                if Path(_FALLBACK_FONT).exists():
                    self.add_font("Fallback", "", _FALLBACK_FONT)
                    self.set_fallback_fonts(["Fallback"])
                return "Body"
        return "Helvetica"

    def clean(self, s: str) -> str:
        if self.body_font != "Helvetica":
            return s
        return s.translate(str.maketrans("\u2014\u2013\u2018\u2019\u201c\u201d", "--''\"\"")).encode(
            "latin-1", "replace").decode("latin-1")

    def use_style(self, size: float, bold: bool = False, color=_INK) -> None:
        self.set_font(self.body_font, "B" if bold else "", size)
        self.set_text_color(*color)

    def para(self, s: str, size: float = 9.5, bold: bool = False, color=_INK, h: float = 4.6, **kw) -> None:
        self.use_style(size, bold, color)
        self.multi_cell(0, h, self.clean(s), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT, **kw)

    def footer(self) -> None:
        self.set_y(-12)
        self.use_style(8, color=_MUTED)
        self.cell(0, 5, self.clean(f"{self.report_title}  ·  page {self.page_no()}"), align="C")


def _short_url(url: str, limit: int = 80) -> str:
    url = re.sub(r"^https?://(www\.)?", "", url)
    return url if len(url) <= limit else url[: limit - 1] + "…"


def _pdf_entry(pdf: _ReportPDF, j: JobPosting) -> None:
    if pdf.get_y() > pdf.h - 60:  # keep a job's heading with its details
        pdf.add_page()
    pdf.use_style(11, bold=True, color=_ACCENT if j.url else _INK)
    pdf.multi_cell(0, 5.4, pdf.clean(j.title), link=j.url or "", align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.para(j.company, size=9.5, color=_MUTED)
    if j.description:
        pdf.ln(0.8)
        pdf.para(j.description)

    details = [
        ("Location", j.location),
        ("Arrangement", None if j.work_arrangement == "unknown" else j.work_arrangement),
        ("Type", None if j.employment_type == "unknown" else j.employment_type),
        ("Seniority", j.seniority),
        ("Salary", j.salary),
        ("Posted", j.posted_date),
        ("Apply by", j.application_deadline),
        ("Requirements", j.requirements),
        ("Fit", f"{j.relevance_score}/10"),
    ]
    pdf.ln(0.8)
    margin = pdf.l_margin
    pdf.set_left_margin(margin + 3)  # indent details, including their wrapped lines
    for label, value in details:
        if not value:
            continue
        warn = label in ("Posted", "Apply by") and is_closed(j)
        pdf.set_x(pdf.l_margin)
        pdf.use_style(9, bold=True)
        pdf.write(4.4, pdf.clean(f"{label}: "))
        pdf.use_style(9, color=_WARN if warn else _INK)
        pdf.write(4.4, pdf.clean(str(value)))
        pdf.ln(4.6)

    for url in j.source_urls:
        pdf.set_x(pdf.l_margin)
        pdf.use_style(8, color=_MUTED)
        pdf.write(4, "Source: ")
        pdf.set_text_color(*_ACCENT)
        pdf.write(4, pdf.clean(_short_url(url)), link=url)
        pdf.ln(4.2)
    pdf.set_left_margin(margin)
    pdf.ln(4)


def write_pdf(jobs: list[JobPosting], profile: dict, path: Path, generated: date | None = None) -> None:
    title = f"Job postings: {', '.join(profile.get('keywords') or []) or 'search results'}"
    pdf = _ReportPDF(title)
    pdf.set_title(title)
    pdf.add_page()

    summary, sections = report_sections(jobs)
    pdf.para(title, size=18, bold=True, h=8)
    pdf.ln(1)
    pdf.para(f"Generated {(generated or date.today()).isoformat()}. {summary}", size=9.5, color=_MUTED)
    pdf.ln(3)
    pdf.set_fill_color(255, 243, 205)
    pdf.para(
        "Before applying, check that the posting is still open on the company's own careers page. This list was "
        "gathered automatically from web pages and may be out of date or incomplete.",
        size=9, bold=True, h=5, fill=True, padding=3,
    )

    for heading, blurb, items in sections:
        if pdf.get_y() > pdf.h - 70:
            pdf.add_page()
        pdf.ln(5)
        pdf.para(f"{heading} ({len(items)})", size=14, bold=True, color=_ACCENT, h=7)
        pdf.set_draw_color(*_ACCENT)
        pdf.line(pdf.l_margin, pdf.get_y() + 0.5, pdf.w - pdf.r_margin, pdf.get_y() + 0.5)
        pdf.ln(3)
        if blurb:
            pdf.para(blurb, size=9, color=_MUTED)
            pdf.ln(2)
        for j in items:
            _pdf_entry(pdf, j)

    pdf.output(str(path))


def write_outputs(
    jobs: list[JobPosting], profile: dict, output_dir: str, stamp: date | None = None, include_csv: bool = True
) -> list[Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = stamp or date.today()
    csv_path, pdf_path = out / f"jobs_{stamp.isoformat()}.csv", out / f"report_{stamp.isoformat()}.pdf"
    if include_csv:
        write_csv(jobs, csv_path)
    write_pdf(jobs, profile, pdf_path, stamp)
    return [csv_path, pdf_path] if include_csv else [pdf_path]
