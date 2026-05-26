"""Local mic/speaker harness for testing Priya without telephony.

Why this exists
---------------
Plivo / Exotel KYC takes 24-48 hours. We don't want to wait. This module
lets you talk to Priya through your laptop mic and hear her through your
speakers, hitting the same orchestrator, the same Sarvam STT/TTS, the
same Gemini, and the same R2 phrase cache that the production phone call
will hit.

If this works on your laptop, the only thing left to add for a real call
is the Plivo WebSocket transport — every voice/LLM/cache concern is
already proven by the time you plug a phone in.

Usage:

  cd apps/pipecat-agent
  python -m voice_agent.local_audio --lang hi-IN --lead-name Suresh

Press ENTER to start recording your turn. Press ENTER again to stop.
Priya responds. Repeat. Type 'q' to quit.

Hard caps + phase machine + cost guardrails apply the same as on a real call.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import wave
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

from .pipeline import HARD_CAP_SECONDS, make_initial_context
from .qualification import QualificationSlots
from .r2_client import R2Client, R2Config, R2ConfigError
from .sarvam_stt import STTResult, transcribe_batch
from .sarvam_tts import synthesize as tts_synthesize
from .gemini_llm import generate as gemini_generate, stream_generate as gemini_stream
from .streaming_orchestrator import (
    AudioChunkEvent,
    StreamingDependencies,
    TurnCompleteEvent,
    run_turn_streaming,
)
from .turn_orchestrator import TurnDependencies


# Telephony-grade audio settings. Match what Plivo will deliver later so
# the pipeline behaves identically.
SAMPLE_RATE_HZ = 16000  # mic input; Sarvam accepts 8/16k. 16k = cleaner STT.
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # int16
TTS_OUTPUT_SAMPLE_RATE = 8000  # Sarvam returns 8k WAV by default


# -- Adapter wrappers exposing the Protocols the orchestrator expects -------

@dataclass
class _SarvamSTTAdapter:
    api_key: str
    client: httpx.AsyncClient

    async def transcribe(self, audio: bytes) -> STTResult:
        return await transcribe_batch(audio=audio, api_key=self.api_key, client=self.client)


@dataclass
class _SarvamTTSAdapter:
    api_key: str
    client: httpx.AsyncClient

    async def synth(self, text: str, lang: str) -> bytes:
        result = await tts_synthesize(
            text=text, lang=lang, api_key=self.api_key, client=self.client
        )
        return result.audio


@dataclass
class _GeminiAdapter:
    api_key: str
    model: str
    client: httpx.AsyncClient

    async def respond(self, system_message: str, user_message: str) -> str:
        resp = await gemini_generate(
            system_message=system_message,
            user_message=user_message,
            api_key=self.api_key,
            model=self.model,
            client=self.client,
        )
        return resp.text

    async def stream_respond(self, system_message: str, user_message: str):
        async for chunk in gemini_stream(
            system_message=system_message,
            user_message=user_message,
            api_key=self.api_key,
            model=self.model,
            client=self.client,
        ):
            yield chunk

    async def extract(self, prompt: str) -> str:
        resp = await gemini_generate(
            system_message="You are a JSON extraction engine. Output ONLY valid JSON.",
            user_message=prompt,
            api_key=self.api_key,
            model=self.model,
            client=self.client,
            generation_config={"temperature": 0.1, "maxOutputTokens": 600},
        )
        return resp.text


# -- WAV helpers (PCM int16 ↔ WAV bytes ready for Sarvam) ------------------

def pcm_to_wav_bytes(pcm: bytes, sample_rate: int = SAMPLE_RATE_HZ) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH_BYTES)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_bytes_to_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
    """Return (raw_pcm, sample_rate) so sounddevice can play it back."""
    with wave.open(BytesIO(wav_bytes), "rb") as w:
        sr = w.getframerate()
        pcm = w.readframes(w.getnframes())
    return pcm, sr


# -- Press-ENTER mic capture (sync loop on a thread) -----------------------

def _get_sounddevice():
    try:
        import sounddevice as sd  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
        return sd, np
    except ImportError:
        raise SystemExit(
            "Local audio needs sounddevice + numpy. Install:\n"
            "  pip install sounddevice numpy"
        )


def list_audio_devices() -> None:
    """Print available audio devices so the user can pick the right mic."""
    sd, _ = _get_sounddevice()
    print("\n=== Audio devices ===")
    print(sd.query_devices())
    default_in = sd.query_devices(kind="input")
    print(f"\nDefault INPUT: {default_in['name']} (index {default_in['index']})")
    print()


def record_until_enter(device: int | None = None) -> bytes:
    """Block stdin, record from mic until ENTER pressed.

    Returns raw int16 PCM at SAMPLE_RATE_HZ, mono. Uses sounddevice.
    On Windows, PortAudio sometimes drops the callback after the first
    recording. We use sd.rec() (blocking read) as a more reliable fallback.
    """
    sd, np = _get_sounddevice()

    if device is None:
        dev_info = sd.query_devices(kind="input")
    else:
        dev_info = sd.query_devices(device)
    print(f"[mic: {dev_info['name']}]")

    print("[recording — speak now, press ENTER to stop]")

    import threading
    stop_event = threading.Event()
    chunks: list[Any] = []
    peak_level = [0.0]

    def _record_thread():
        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE_HZ,
                channels=CHANNELS,
                dtype="int16",
                device=device,
                blocksize=1024,
            )
            stream.start()
            while not stop_event.is_set():
                data, overflowed = stream.read(1024)
                if len(data) > 0:
                    chunks.append(data.copy())
                    level = np.abs(data).max()
                    if level > peak_level[0]:
                        peak_level[0] = level
            stream.stop()
            stream.close()
        except Exception as exc:
            print(f"[mic error: {exc}]")

    rec_thread = threading.Thread(target=_record_thread, daemon=True)
    rec_thread.start()

    try:
        input()
    except EOFError:
        pass
    stop_event.set()
    rec_thread.join(timeout=2.0)

    if not chunks:
        print("[no audio chunks captured — mic may be muted or wrong device]")
        return b""

    pcm = np.concatenate(chunks).tobytes()
    duration_ms = len(pcm) / (SAMPLE_RATE_HZ * SAMPLE_WIDTH_BYTES) * 1000
    print(f"[captured {duration_ms:.0f}ms, peak={peak_level[0]}]")

    if peak_level[0] < 50:
        print("[WARNING: very low audio level — mic may be muted]")

    return pcm


def play_pcm(pcm: bytes, sample_rate: int) -> None:
    try:
        import sounddevice as sd  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except ImportError:
        raise SystemExit("sounddevice/numpy missing — see record_until_enter() msg")

    arr = np.frombuffer(pcm, dtype=np.int16)
    sd.play(arr, samplerate=sample_rate)
    sd.wait()


# -- Boot the harness ------------------------------------------------------

def _load_env() -> dict[str, str]:
    """Pull credentials, prefer apps/pipecat-agent/.env then os.environ."""
    try:
        from dotenv import dotenv_values  # type: ignore[import-not-found]
    except ImportError:
        dotenv_values = None

    env: dict[str, str] = {}
    if dotenv_values:
        env.update({k: v for k, v in dotenv_values(".env").items() if v})
    env.update({k: v for k, v in os.environ.items() if v})
    return env


def _build_deps(env: dict[str, str], http: httpx.AsyncClient) -> TurnDependencies:
    sarvam_key = env.get("SARVAM_API_KEY", "")
    gemini_key = env.get("GEMINI_API_KEY", "")
    gemini_model = env.get("GEMINI_MODEL", "gemini-2.5-flash")
    if not sarvam_key:
        raise SystemExit("SARVAM_API_KEY missing in .env")
    if not gemini_key:
        raise SystemExit("GEMINI_API_KEY missing in .env")

    # R2 is optional — without it the phrase cache simply always misses.
    try:
        r2_cfg = R2Config.from_env(env)
        r2 = R2Client(r2_cfg)
        r2_reader = r2
        r2_writer = r2
    except R2ConfigError as exc:
        print(f"[warn] R2 disabled ({exc}). Phrase cache will always miss.")
        r2_reader = _NoOpR2()
        r2_writer = _NoOpR2()

    return TurnDependencies(
        stt=_SarvamSTTAdapter(api_key=sarvam_key, client=http),
        tts=_SarvamTTSAdapter(api_key=sarvam_key, client=http),
        llm=_GeminiAdapter(api_key=gemini_key, model=gemini_model, client=http),
        r2_reader=r2_reader,
        r2_writer=r2_writer,
    )


class _NoOpR2:
    """Fallback when R2 env vars aren't set. Pretends every key is missing."""

    async def get(self, key: str) -> bytes | None:
        return None

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        return None


async def run_local(args: argparse.Namespace) -> None:
    if args.list_devices:
        list_audio_devices()
        return

    env = _load_env()
    async with httpx.AsyncClient(timeout=30.0) as http:
        deps = _build_deps(env, http)

        ctx = make_initial_context(
            call_id="local-test",
            tenant_id=args.tenant_id,
            lead_id="local-lead",
            lead_first_name=args.lead_name,
            lead_company=args.lead_company,
            default_lang=args.lang,
        )

        print(
            f"\n=== Priya local harness ===\n"
            f"Lang: {args.lang}  Lead: {args.lead_name} @ {args.lead_company}\n"
            f"Hard cap: {HARD_CAP_SECONDS}s. Type 'q' + ENTER to quit.\n"
        )

        # ---- PRIYA STARTS FIRST (like a real outbound call) ----
        # On a real call, Priya delivers the intro BEFORE the lead speaks.
        from .prompts import build_intro_text
        intro_text = build_intro_text(lang=args.lang, first_name=args.lead_name)
        print(f"  PRIYA (intro): Synthesizing...")
        try:
            intro_audio = await deps.tts.synth(intro_text, args.lang)
            print(f"  PRIYA: {intro_text}")
            try:
                intro_pcm, intro_sr = wav_bytes_to_pcm(intro_audio)
                play_pcm(intro_pcm, intro_sr)
            except Exception:
                print("  [intro playback failed]")
        except Exception as exc:
            print(f"  [intro TTS failed: {exc}]")
        print()

        slots = QualificationSlots()
        sdeps = StreamingDependencies(
            stt=deps.stt, tts=deps.tts, llm=deps.llm,
            r2_reader=deps.r2_reader, r2_writer=deps.r2_writer,
        )
        device = args.device

        while True:
            if ctx.should_hard_stop():
                print("[hard cap reached — ending call]")
                break

            line = input("Press ENTER to record your turn (or 'q' to quit): ").strip()
            if line.lower() == "q":
                break

            pcm = record_until_enter(device=device)
            if not pcm:
                print("[empty recording — check mic. Try: --list-devices]")
                continue

            wav = pcm_to_wav_bytes(pcm)
            t0 = time.monotonic()

            # Streaming: play each sentence as it arrives
            sentence_count = 0
            async for event in run_turn_streaming(
                ctx=ctx, audio_in=wav, deps=sdeps, prior_slots=slots,
            ):
                if isinstance(event, AudioChunkEvent):
                    sentence_count += 1
                    if sentence_count == 1:
                        first_audio_ms = int((time.monotonic() - t0) * 1000)
                        print(f"\n  [first audio in {first_audio_ms}ms]")
                    print(f"  PRIYA [{event.sentence_idx}]: {event.text}")
                    try:
                        priya_pcm, priya_sr = wav_bytes_to_pcm(event.audio)
                        play_pcm(priya_pcm, priya_sr)
                    except Exception:
                        print("  [audio playback failed — likely not WAV]")
                elif isinstance(event, TurnCompleteEvent):
                    slots = event.slots
                    wall_ms = int((time.monotonic() - t0) * 1000)
                    lm = event.latency_ms
                    print(f"\n  LEAD ({event.lead_lang}): {event.lead_text}")
                    print(
                        f"  [turn={ctx.turn_idx}  phase={ctx.conversation_state.phase.value}  "
                        f"sentences={event.total_sentences}  cache_hits={event.cache_hits}  "
                        f"buying_conf={slots.buying_confidence:.2f}]"
                    )
                    print(
                        f"  [latency  stt={lm.get('stt_ms', 0)}ms  "
                        f"llm_1st_sent={lm.get('llm_first_sentence_ms', 0)}ms  "
                        f"tts_1st={lm.get('tts_first_sentence_ms', 0)}ms  "
                        f"total={lm.get('total_ms', 0)}ms  wall={wall_ms}ms]"
                    )

        print("\n=== Call summary ===")
        print(f"  turns:        {ctx.turn_idx}")
        print(f"  elapsed:      {ctx.elapsed():.1f}s")
        print(f"  billed units: {ctx.billed_units()}")
        print(f"  cache hits:   {ctx.phrase_cache_hits}")
        print(f"  final score:  {slots.score(frozenset())}")


def main() -> None:
    p = argparse.ArgumentParser(prog="voice_agent.local_audio")
    p.add_argument("--lang", default="hi-IN", choices=["hi-IN", "en-IN", "ta-IN"])
    p.add_argument("--lead-name", default="Suresh")
    p.add_argument("--lead-company", default="Acme Chemicals")
    p.add_argument("--tenant-id", default="local-test-tenant")
    p.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    p.add_argument("--device", type=int, default=None, help="Input device index (from --list-devices)")
    args = p.parse_args()
    asyncio.run(run_local(args))


if __name__ == "__main__":
    main()
