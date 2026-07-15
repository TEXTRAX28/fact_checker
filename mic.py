import io
import os
import queue
import threading
import wave
from concurrent.futures import ThreadPoolExecutor

import sounddevice as sd
import webrtcvad
from groq import Groq

from fact_checker import fact_check
from display import show_results

# webrtcvad only accepts 8000/16000/32000/48000 Hz and 10/20/30ms frames.
SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
SILENCE_MS = 800          # trailing silence before an utterance is flushed
MAX_UTTERANCE_S = 20      # safety valve against VAD misfiring and buffering forever
MIN_UTTERANCE_S = 0.3     # drops clicks/breath shorter than this
VAD_MODE = 2              # webrtcvad aggressiveness, 0 (least) - 3 (most)
ASR_MODEL = "whisper-large-v3-turbo"
MAX_PENDING = 3           # cap on unresolved fact-check calls; without this an
                          # unattended session queues utterances forever, burning
                          # DeepInfra/Groq calls on a backlog nobody will read in order

_groq_client = None
def _groq():
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _groq_client


class UtteranceSegmenter:
    # Buffers 30ms frames while the VAD says "speech," flushes the utterance on
    # SILENCE_MS of trailing silence. Non-overlapping by construction, so no
    # window-boundary dedup logic is needed (see feature#1.md).
    def __init__(self, vad):
        self.vad = vad
        self._silence_frames_needed = SILENCE_MS // FRAME_MS
        self._max_frames = int(MAX_UTTERANCE_S * 1000 // FRAME_MS)
        self._min_speech_frames = int(MIN_UTTERANCE_S * 1000 // FRAME_MS)
        self._buf = []
        self._speech_frame_count = 0
        self._silence_run = 0

    def push(self, frame: bytes) -> bytes | None:
        is_speech = self.vad.is_speech(frame, SAMPLE_RATE)
        if is_speech:
            self._buf.append(frame)
            self._speech_frame_count += 1
            self._silence_run = 0
            if self._speech_frame_count >= self._max_frames:
                return self._flush()
        elif self._buf:
            self._buf.append(frame)  # keep the natural trailing silence in the clip
            self._silence_run += 1
            if self._silence_run >= self._silence_frames_needed:
                return self._flush()
        return None

    def _flush(self) -> bytes | None:
        # Gate on speech-frame count, not total buffered frames: the trailing
        # silence tail alone is >= _silence_frames_needed frames, so gating on
        # total length would never drop a short blip.
        frames = self._buf
        keep = self._speech_frame_count >= self._min_speech_frames
        self._buf, self._speech_frame_count, self._silence_run = [], 0, 0
        return b"".join(frames) if keep else None


def _to_wav_bytes(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


def _transcribe(pcm: bytes) -> str:
    resp = _groq().audio.transcriptions.create(
        model=ASR_MODEL,
        file=("utterance.wav", _to_wav_bytes(pcm)),
    )
    return resp.text.strip()


def _print_one_result(result: dict):
    show_results([result])


_pending_lock = threading.Lock()
_pending = 0

def _submit_fact_check(pool: ThreadPoolExecutor, text: str, verbose: bool):
    global _pending
    with _pending_lock:
        if _pending >= MAX_PENDING:
            print(f"[!] {_pending} utterance(s) already queued, dropping this one to catch up")
            return
        _pending += 1
        depth = _pending
    if depth > 1:
        print(f"[!] {depth} utterance(s) queued, falling behind")

    def _done(_future):
        global _pending
        with _pending_lock:
            _pending -= 1

    pool.submit(fact_check, text, on_result=_print_one_result, verbose=verbose).add_done_callback(_done)


def _handle_utterance(pcm: bytes, factcheck_pool: ThreadPoolExecutor, verbose: bool):
    # Runs in the ASR pool. Mic disconnect / API failure -> drop this utterance,
    # keep listening (same shape as _fetch_article()'s per-tier try/except).
    try:
        text = _transcribe(pcm)
    except Exception as e:
        print(f"[ERROR] Transcription: {type(e).__name__}: {e}")
        return
    if not text:
        return
    if verbose:
        print(f"[v] Heard: {text}")
    _submit_fact_check(factcheck_pool, f"[SPEAKER_A] {text}", verbose)


def _listen(segmenter: UtteranceSegmenter, utterance_queue: "queue.Queue[bytes]", stop_event: threading.Event):
    def callback(indata, frames, time_info, status):
        utterance = segmenter.push(bytes(indata))
        if utterance:
            utterance_queue.put(utterance)

    try:
        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES,
                                dtype="int16", channels=1, callback=callback):
            stop_event.wait()
    except Exception as e:
        print(f"[ERROR] Microphone: {type(e).__name__}: {e}")
        stop_event.set()


def run_mic(verbose: bool = False):
    if not os.environ.get("GROQ_API_KEY"):
        print("ERROR: GROQ_API_KEY not set (needed for speech-to-text). Add it to .env")
        return

    utterance_queue: "queue.Queue[bytes]" = queue.Queue()
    stop_event = threading.Event()
    segmenter = UtteranceSegmenter(webrtcvad.Vad(VAD_MODE))

    listener = threading.Thread(target=_listen, args=(segmenter, utterance_queue, stop_event), daemon=True)
    listener.start()

    # Not a `with` block on purpose: `ThreadPoolExecutor.__exit__` defaults to
    # shutdown(wait=True), which would drain the entire queued backlog before
    # Ctrl+C actually exits. cancel_futures below drops anything not yet
    # started instead, so only the one utterance already mid-flight finishes.
    asr_pool = ThreadPoolExecutor(max_workers=1)
    factcheck_pool = ThreadPoolExecutor(max_workers=1)

    print("Listening... speak naturally, Ctrl+C to stop.\n")
    try:
        while not stop_event.is_set():
            try:
                pcm = utterance_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            asr_pool.submit(_handle_utterance, pcm, factcheck_pool, verbose)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        stop_event.set()
        listener.join(timeout=2)
        asr_pool.shutdown(wait=False, cancel_futures=True)
        factcheck_pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    # Self-check: UtteranceSegmenter must ignore leading silence, buffer speech,
    # flush on trailing silence, drop sub-MIN_UTTERANCE_S blips even though the
    # trailing silence tail pads the buffer past that length, and hard-cap at
    # MAX_UTTERANCE_S — all without a real mic, webrtcvad, or network call.
    class _ScriptedVad:
        def __init__(self, verdicts):
            self._verdicts = list(verdicts)
        def is_speech(self, buf, rate):
            return self._verdicts.pop(0)

    def _drive(seg, script):
        result = None
        for _ in script:
            result = seg.push(frame)
        return result

    frame = b"\x00\x00" * FRAME_SAMPLES
    silence_needed = SILENCE_MS // FRAME_MS
    min_frames = int(MIN_UTTERANCE_S * 1000 // FRAME_MS)
    max_frames = int(MAX_UTTERANCE_S * 1000 // FRAME_MS)
    speech_frames = min_frames + 5

    script = [False, False] + [True] * speech_frames + [False] * silence_needed
    result = _drive(UtteranceSegmenter(_ScriptedVad(script)), script)
    assert result == frame * (speech_frames + silence_needed), \
        "flush = buffered speech + trailing silence tail, leading silence ignored"

    short_script = [True] * (min_frames - 1) + [False] * silence_needed
    result = _drive(UtteranceSegmenter(_ScriptedVad(short_script)), short_script)
    assert result is None, "utterances shorter than MIN_UTTERANCE_S of speech must be dropped, not queued"

    long_script = [True] * max_frames
    result = _drive(UtteranceSegmenter(_ScriptedVad(long_script)), long_script)
    assert result == frame * max_frames, "must hard-flush at MAX_UTTERANCE_S even with no trailing silence"

    print("OK: all self-checks pass")
