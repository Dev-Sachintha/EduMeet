"""Speech-to-text with Whisper (faster-whisper) running on this computer - free, no API key.

The first caption downloads the model once (tiny ~75 MB, base ~145 MB, small ~480 MB).
"""
import os
import re
import tempfile
import threading

from backend.config import settings

_model = None
_model_lock = threading.Lock()
_slots = threading.BoundedSemaphore(max(1, settings.ai_max_parallel_transcriptions))

# Whisper sometimes "hears" these in silence/noise
_PHANTOM = {
    "thanks for watching", "thank you for watching", "please subscribe", "subscribe to my channel",
    "subtitles by the amaraorg community", "like and subscribe",
}


def whisper_available() -> tuple[bool, str]:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False, "Speech recognition is not installed. Run: python -m pip install faster-whisper"
    return True, ""


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            _model = WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
        return _model


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    plain = re.sub(r"[^\w\s]", "", text.lower()).strip()
    if len(plain) < 2 or plain in _PHANTOM:
        return ""
    return text


def transcribe_bytes(data: bytes) -> str | None:
    """Transcribe one short audio chunk (webm/opus). Returns None when the server is busy."""
    if not _slots.acquire(blocking=False):
        return None
    try:
        model = get_model()
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as handle:
            handle.write(data)
            path = handle.name
        try:
            segments, _info = model.transcribe(
                path, language=settings.whisper_language or None, vad_filter=True,
                beam_size=1, condition_on_previous_text=False,
            )
            text = " ".join(segment.text.strip() for segment in segments)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return clean_text(text)
    finally:
        _slots.release()


_file_slot = threading.Lock()  # long files are transcribed one at a time


def transcribe_file(path: str, on_progress=None) -> str:
    """Transcribe a whole audio/video file (any format PyAV can read). on_progress(fraction, message)."""
    if on_progress:
        on_progress(0.0, "Waiting for the speech engine...")
    with _file_slot:
        model = get_model()
        if on_progress:
            on_progress(0.0, "Transcribing...")
        segments, info = model.transcribe(
            path, language=settings.whisper_language or None, vad_filter=True,
            beam_size=1, condition_on_previous_text=False,
        )
        total = float(getattr(info, "duration", 0) or 0)
        parts: list[str] = []
        for segment in segments:
            line = segment.text.strip()
            if line:
                parts.append(line)
            if on_progress and total:
                on_progress(min(0.99, segment.end / total), "Transcribing...")
        return re.sub(r"[ \t]+", " ", "\n".join(parts)).strip()
