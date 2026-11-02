import logging
from pathlib import Path

log = logging.getLogger(__name__)

MAX_RESUME_CHARS = 15_000
_SUFFIXES = {".pdf", ".txt", ".md"}


def _read(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    return path.read_text(errors="replace")


def load_resumes(folder: str) -> tuple[str, int]:
    """Text of every PDF/TXT/MD resume in the folder, each headed by its filename, and how many were read."""
    root = Path(folder)
    if not root.is_dir():
        return "", 0
    parts = []
    for path in sorted(p for p in root.iterdir() if p.suffix.lower() in _SUFFIXES):
        try:
            # PDF extraction often puts every word on its own line
            text = " ".join(_read(path).split())
        except Exception as e:  # one unreadable file shouldn't stop the run
            log.warning("Couldn't read resume %s: %s", path, e)
            continue
        if text:
            parts.append(f"=== {path.name} ===\n{text}")
    return "\n\n".join(parts)[:MAX_RESUME_CHARS], len(parts)
