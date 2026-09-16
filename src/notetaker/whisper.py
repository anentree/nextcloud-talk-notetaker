"""Local speech-to-text with faster-whisper.

Runs on the CPU inside the container, so a call costs no API quota and the
audio never leaves the machine. Two rules shape this file:

- ``faster_whisper`` is imported inside the functions, never at module top.
  A Gemini-only install must not fail to start because a native library of
  a backend it does not use will not load.
- Every slow step beats the heartbeat. A one-hour call takes 45-60 minutes
  here on the single-threaded poll loop, and the container healthcheck
  declares the process dead after ~170 s without a beat.
"""

from __future__ import annotations

import ctypes
import gc
import logging
import os
import subprocess
import sys
import tempfile
import threading
import traceback

log = logging.getLogger(__name__)

DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_MODEL_DIR = "/data/models"
# The one command every message points at. Works on compose v2 and the v1
# binary; --no-deps because the download needs no other service.
DOWNLOAD_COMMAND = "docker compose run --rm --no-deps notetaker download-model"
# Style bias for the decoder (see transcribe()). Not a vocabulary list.
INITIAL_PROMPT = "Hello, welcome to the meeting. Let's get started, shall we?"

# Test seams. None means "resolve the real faster_whisper symbol at call time".
_model_factory = None
_download_fn = None


class WhisperFailed(RuntimeError):
    """Local transcription produced no result (conversion, load or decode).

    The caller retains the recording and counts these; after
    transcriber.WHISPER_MAX_FAILURES the recording goes to Gemini audio.
    """


def _resolve_model_factory():
    if _model_factory is not None:
        return _model_factory
    from faster_whisper import WhisperModel

    return WhisperModel


def _resolve_download_fn():
    if _download_fn is not None:
        return _download_fn
    from faster_whisper import download_model

    return download_model


def _beat(heartbeat_fn) -> None:
    if heartbeat_fn is None:
        return
    try:
        heartbeat_fn()
    except Exception:
        log.debug("Heartbeat callback failed", exc_info=True)


def _cpu_threads() -> int:
    """All cores but one: Nextcloud and the next call's Chromium share this box."""
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cores = os.cpu_count() or 2
    return max(1, cores - 1)


def _to_wav(audio_path: str) -> str:
    """16 kHz mono PCM in the container's /tmp (AUDIO_DIR is a tmpfs that
    counts against the memory limit)."""
    fd, wav = tempfile.mkstemp(prefix="notetaker-whisper-", suffix=".wav")
    os.close(fd)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav],
            capture_output=True,
            check=True,
        )
    except BaseException:
        try:
            os.unlink(wav)
        except OSError:
            pass
        raise
    return wav


def _release_memory() -> None:
    """glibc does not hand CTranslate2's arena back to the OS on its own."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # pragma: no cover - non-glibc platforms
        pass


def transcribe(
    audio_path: str,
    model_name: str,
    model_dir: str,
    language: str = "en",
    heartbeat_fn=None,
) -> list[dict]:
    """Return ``[{start, end, text}, ...]`` in seconds, text stripped, blanks dropped.

    Raises WhisperFailed for any failure; the original is ``__cause__``.
    """
    wav = None
    model = None
    segments = None
    try:
        wav = _to_wav(audio_path)
        _beat(heartbeat_fn)
        threads = _cpu_threads()
        log.info("Loading Whisper model %s from %s (%d threads)", model_name, model_dir, threads)
        model = _resolve_model_factory()(
            model_name,
            device="cpu",
            compute_type="int8",
            cpu_threads=threads,
            download_root=model_dir,
            local_files_only=True,
        )
        _beat(heartbeat_fn)
        log.info("Transcribing %s with Whisper (language=%s)", audio_path, language or "auto")
        _beat(heartbeat_fn)
        segments, _info = model.transcribe(
            wav,
            vad_filter=True,
            beam_size=5,
            language=language or None,
            # Without a punctuated prompt this model answered in lowercase
            # with no punctuation on the 2026-09-16 benchmark; with one it
            # capitalised names and punctuated normally. It biases style only.
            initial_prompt=INITIAL_PROMPT,
        )
        out: list[dict] = []
        for segment in segments:
            text = (segment.text or "").strip()
            if text:
                out.append({"start": float(segment.start), "end": float(segment.end), "text": text})
            _beat(heartbeat_fn)
        log.info("Whisper produced %d segments", len(out))
        return out
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"")[-400:]
        raise WhisperFailed(f"ffmpeg failed: {stderr!r}") from exc
    except Exception as exc:
        # The traceback's frames pin the model; drop them so the trim below
        # can actually return the arena. File/line survive for logging.
        traceback.clear_frames(exc.__traceback__)
        raise WhisperFailed(f"{type(exc).__name__}: {exc}") from exc
    finally:
        # An un-drained generator (mid-decode failure) still references the
        # model; drop both before trimming or malloc_trim frees nothing.
        segments = None  # noqa: F841
        model = None  # noqa: F841
        _release_memory()
        if wav:
            try:
                os.unlink(wav)
            except OSError:
                pass


def download(model_name: str = DEFAULT_MODEL, model_dir: str = DEFAULT_MODEL_DIR) -> str:
    """Fetch the model into ``model_dir`` (resumable); returns the snapshot path."""
    return _resolve_download_fn()(model_name, cache_dir=model_dir)


def model_is_downloaded(model_name: str, model_dir: str) -> bool:
    """True when the snapshot is complete on disk. A ValueError (unknown
    model name) propagates so preflight can name the real problem."""
    try:
        _resolve_download_fn()(model_name, cache_dir=model_dir, local_files_only=True)
    except OSError:  # huggingface_hub's LocalEntryNotFoundError is a FileNotFoundError
        return False
    return True


def _dir_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def download_from_env(progress_every: float = 5.0) -> int:
    """Body of ``notetaker download-model``. Reads only the two Whisper settings
    so it works with a half-written .env and inside the wizard.

    faster_whisper disables huggingface_hub's progress bar, so a poller
    prints the bytes on disk every few seconds; 1.6 GB in silence looks hung.
    """
    name = os.getenv("WHISPER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    model_dir = os.getenv("WHISPER_MODEL_DIR", DEFAULT_MODEL_DIR)
    print(f"Downloading Whisper model '{name}' into {model_dir} ...", flush=True)
    stop = threading.Event()

    def report():
        while not stop.wait(progress_every):
            print(f"  ... {_dir_bytes(model_dir) // (1024 * 1024)} MB on disk so far", flush=True)

    poller = threading.Thread(target=report, daemon=True)
    poller.start()
    try:
        path = download(name, model_dir)
    except Exception as exc:
        print(f"Download failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        stop.set()
        poller.join(timeout=1)
    print(f"Whisper model '{name}' is ready at {path}", flush=True)
    return 0
