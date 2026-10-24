from functools import lru_cache

from faster_whisper import WhisperModel

from . import config


@lru_cache(maxsize=1)
def _model() -> WhisperModel:
    # int8 on CPU keeps the Droplet happy; nobody is waiting at 3 AM.
    return WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")


def transcribe(path: str) -> tuple[str, float]:
    """Return (transcript, audio duration in seconds)."""
    segments, info = _model().transcribe(path, vad_filter=True, beam_size=1)
    text = " ".join(s.text.strip() for s in segments).strip()
    return text, float(info.duration)
