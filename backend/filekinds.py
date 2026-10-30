"""Which file types the chat accepts, and how to label them."""
import mimetypes
from pathlib import Path

KIND_EXT = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"},          # no .svg: it can carry scripts
    "pdf": {".pdf"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".webm", ".flac"},
    "video": {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi"},
}


def kind_of(name: str, content_type: str = "") -> str:
    """'image' | 'pdf' | 'audio' | 'video' | 'file'"""
    ext, ctype = Path(name).suffix.lower(), (content_type or "").lower()
    for prefix, kind in (("image/", "image"), ("audio/", "audio"), ("video/", "video")):
        if ctype.startswith(prefix):
            return kind
    if ctype == "application/pdf" or ext in KIND_EXT["pdf"]:
        return "pdf"
    for kind, exts in KIND_EXT.items():
        if ext in exts:
            return kind
    return "file"


def content_type_for(kind: str, ext: str) -> str:
    if ext == ".webm":
        return "audio/webm" if kind == "audio" else "video/webm"
    return mimetypes.guess_type("x" + ext)[0] or "application/octet-stream"
