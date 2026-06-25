import os
import wave
import tempfile
import numpy as np
from groq import Groq

SAMPLE_RATE = 16000
_client = None

def _groq():
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client

def _save_wav(audio: np.ndarray) -> str:
    f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    with wave.open(f.name, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes((audio * 32767).astype(np.int16).tobytes())
    return f.name

def _transcribe_path(path: str) -> str | None:
    with open(path, "rb") as f:
        result = _groq().audio.transcriptions.create(
            file=("audio.wav", f),
            model="whisper-large-v3",
            response_format="text",
        )
    text = str(result).strip()
    return f"[UNKNOWN] {text}" if text else None

def transcribe_chunk(audio: np.ndarray) -> str | None:
    path = _save_wav(audio)
    try:
        return _transcribe_path(path)
    except Exception:
        return None
    finally:
        os.unlink(path)

def transcribe_file(path: str) -> str | None:
    try:
        return _transcribe_path(path)
    except Exception:
        return None
