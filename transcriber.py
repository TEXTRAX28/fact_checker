import os
import wave
import tempfile
import numpy as np

# Local Whisper (faster-whisper)
_model = None

SAMPLE_RATE = 16000

def _whisper_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel("base", device="cuda", compute_type="float16")
    return _model

def _save_wav(audio: np.ndarray) -> str:
    f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    with wave.open(f.name, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes((audio * 32767).astype(np.int16).tobytes())
    return f.name

def _transcribe_path(path: str) -> str | None:
    try:
        model = _whisper_model()
        segments, _ = model.transcribe(path, language="id")
        text = " ".join(segment.text for segment in segments).strip()
        return f"[UNKNOWN] {text}" if text else None
    except Exception as e:
        print(f"Whisper error: {e}")
        return None

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
