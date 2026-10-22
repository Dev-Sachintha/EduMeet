"""Turn an uploaded file (PDF, text, audio, video) into plain text for question generation."""
import base64
from pathlib import Path

from fastapi import HTTPException

from backend.config import settings
from backend.llm import ask_claude, claude_configured
from backend.transcribe import transcribe_file, whisper_available

TEXT_EXT = {".txt", ".md", ".csv"}
PDF_EXT = {".pdf"}
MEDIA_EXT = {
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wma",          # audio
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv",                  # video
}
ALLOWED_EXT = TEXT_EXT | PDF_EXT | MEDIA_EXT
SUPPORTED_TEXT = "PDF, text (.txt .md .csv), audio (.mp3 .wav .m4a .ogg ...) or video (.mp4 .mkv .mov .webm ...)"
MAX_CLAUDE_PDF_MB = 30


class ExtractError(Exception):
    """A readable problem to show to the teacher."""


def _read_text_file(path: str) -> str:
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeError:
            continue
    return raw.decode("cp1252", errors="replace")


def _read_pdf(path: str, on_progress) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ExtractError("PDF reading is not installed. Run: python -m pip install pypdf") from None
    reader = PdfReader(path)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            raise ExtractError("This PDF is password protected.") from None
    pages = []
    for index, page in enumerate(reader.pages):
        pages.append(page.extract_text() or "")
        on_progress((index + 1) / max(1, len(reader.pages)), "Reading the PDF...")
    text = "\n\n".join(pages).strip()
    if len(text) >= 200:
        return text
    # Scanned PDF (pictures of pages): let Claude read the pages once.
    if not claude_configured():
        raise ExtractError("This PDF has no selectable text (it looks scanned). Add ANTHROPIC_API_KEY to read scanned PDFs.")
    if Path(path).stat().st_size > MAX_CLAUDE_PDF_MB * 1024 * 1024:
        raise ExtractError(f"This scanned PDF is too large to read (limit {MAX_CLAUDE_PDF_MB} MB).")
    on_progress(0.5, "Scanned PDF: the AI is reading the pages...")
    data = base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")
    try:
        return ask_claude(
            "Transcribe the text of this document faithfully, in reading order. Keep headings and lists. "
            "Do not summarise or add comments.",
            [{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
                {"type": "text", "text": "Transcribe all the text in this PDF."},
            ]}],
            8000,
        )
    except HTTPException as exc:
        raise ExtractError(str(exc.detail)) from None


def extract_text(path: str, ext: str, on_progress) -> tuple[str, str]:
    """Returns (kind, text). on_progress(fraction, message)."""
    ext = ext.lower()
    if ext in TEXT_EXT:
        return "text", _read_text_file(path)
    if ext in PDF_EXT:
        return "pdf", _read_pdf(path, on_progress)
    if ext in MEDIA_EXT:
        ok, why = whisper_available()
        if not ok:
            raise ExtractError(why)
        return "audio/video", transcribe_file(path, on_progress)
    raise ExtractError("Unsupported file type. Use " + SUPPORTED_TEXT + ".")
