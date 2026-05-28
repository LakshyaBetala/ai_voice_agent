"""FastAPI WebSocket handler for Exotel Voice Streaming.

Exotel opens a WebSocket to us when a call connects. The handler:

  1. Reads inbound μ-law audio chunks from the lead.
  2. Buffers them until a silence threshold (simple VAD) or a max-buffer.
  3. Converts buffer → WAV PCM → Sarvam STT (via the orchestrator).
  4. Sends Priya's TTS audio (μ-law) back over the same WS.

The orchestrator drives all conversation logic — this file only handles
the audio framing + WS lifecycle. Per-call CallContext lives in memory
keyed by Exotel's stream_sid.

Mounted under voice_agent.server at:

  WS  /exotel/stream/{call_id}
  POST /exotel/calls          (place outbound + return call_sid)
"""
from __future__ import annotations

import array
import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

from .audio_codec import exotel_pcm_to_wav_for_stt, tts_wav_to_exotel_pcm
from .exotel_transport import (
    ExotelError,
    ExotelStreamSession,
    OutboundCallRequest,
    StreamMediaFrame,
    StreamStartFrame,
    StreamStopFrame,
    place_outbound_call,
)
from .pipeline import HARD_CAP_SECONDS, CallContext, make_initial_context
from .prompts import build_intro_text
from .qualification import QualificationSlots
from .r2_client import R2Client, R2Config, R2ConfigError
from .streaming_orchestrator import (
    AudioChunkEvent,
    StreamingDependencies,
    TurnCompleteEvent,
    run_turn_streaming,
)
from .supabase_client import (
    AgentSupabaseClient,
    SupabaseConfig,
    SupabaseConfigError,
    persist_turn_async,
)
from .turn_orchestrator import TurnDependencies, run_turn

logger = logging.getLogger(__name__)

router = APIRouter()


# -- In-process call registry ----------------------------------------------
#
# Maps call_id → CallContext + slots. Lives only as long as the process.
# For multi-instance deployment we'd push this to Redis; SPC's demo runs
# on one Hetzner box so an in-memory dict is fine for now.

@dataclass
class _ActiveCall:
    ctx: CallContext
    slots: QualificationSlots
    deps: TurnDependencies
    db: AgentSupabaseClient | None = None


_active_calls: dict[str, _ActiveCall] = {}

# call_id of the most recently triggered outbound call. Exotel's static
# Voicebot applet URL means the WS arrives with no CustomField to match on,
# so for the (single-concurrent) demo we fall back to this — that's the call
# we just dialed, carrying the right lead name + language.
_last_pending_call_id: str | None = None


# -- Inbound-audio buffering (simple silence VAD) ---------------------------

# Exotel AgentStream sends ~100ms raw 16-bit PCM chunks (3200 bytes at
# 16 kHz, 1600 at 8 kHz). We accumulate them and flush to STT when either:
#   - silence threshold met (low peak amplitude for `SILENCE_MS_THRESHOLD`)
#   - hard buffer cap reached (avoids runaway when lead never pauses)

SILENCE_MS_THRESHOLD = 700  # ms of quiet → assume utterance ended
MAX_BUFFER_MS = 8000         # hard cap so STT call doesn't grow unbounded
MIN_UTTERANCE_MS = 400       # noise floor — drop buffers shorter than this

# Stream sample rate must match the Voicebot applet's configured rate.
EXOTEL_STREAM_SAMPLE_RATE = int(os.environ.get("EXOTEL_STREAM_SAMPLE_RATE", "8000"))

# Peak-amplitude threshold below which a raw-PCM chunk counts as "silent".
# Mirrors the local harness VAD (SILENCE_THRESHOLD=300).
_PCM_SILENCE_THRESHOLD = 300


def _is_silent_pcm(pcm: bytes, threshold: int = _PCM_SILENCE_THRESHOLD) -> bool:
    """Rough VAD on signed-16 little-endian PCM: peak below threshold = silent."""
    if len(pcm) < 2:
        return True
    arr = array.array("h")
    arr.frombytes(pcm[: len(pcm) // 2 * 2])
    if not arr:
        return True
    return max(abs(s) for s in arr) < threshold


def _chunk_ms(pcm: bytes, sample_rate: int) -> int:
    """Duration in ms of a raw-PCM chunk (2 bytes/sample, mono)."""
    return int((len(pcm) // 2) / sample_rate * 1000)


# Half-duplex: we stream Priya's whole reply into Exotel's buffer instantly,
# but it PLAYS over several seconds. While it plays (plus a tail for the
# line to settle) we ignore all inbound audio — otherwise we transcribe her
# own echo and she talks over herself ("rapping"). This is time-based, not
# silence-based, because we can't reliably hear playback end on a phone line.
SPEAK_TAIL_SEC = 0.7

# Only run a turn when the lead actually SPOKE this much (non-silent audio).
# Pure silence/comfort-noise must never trigger a response, or Priya nags
# "Sir, sun pa rahe hain?" on every quiet moment.
MIN_VOICED_MS = 350

# Outbound frames to Exotel must be small (multiples of 320 bytes / ~100ms).
# 1600 bytes = 800 samples = 100ms at 8 kHz. We slice TTS audio into these.
_OUT_FRAME_BYTES = 1600


def _audio_dur_sec(pcm: bytes, sample_rate: int) -> float:
    """Playback duration of raw 16-bit mono PCM."""
    return (len(pcm) // 2) / sample_rate


async def _send_pcm_chunked(session: ExotelStreamSession, pcm: bytes) -> None:
    """Send raw PCM to Exotel in small, applet-friendly frames."""
    for i in range(0, len(pcm), _OUT_FRAME_BYTES):
        await session.send_audio(pcm[i : i + _OUT_FRAME_BYTES])


async def _play_text(
    session: ExotelStreamSession, active: "_ActiveCall", text: str
) -> float:
    """Synthesize `text`, stream it to the lead, record it as a Priya turn.
    Returns the audio's playback duration in seconds (for the mute window)."""
    if not text.strip():
        return 0.0
    lang = active.ctx.language_state.current.value
    wav = await active.deps.tts.synth(text, lang)
    pcm = tts_wav_to_exotel_pcm(wav, EXOTEL_STREAM_SAMPLE_RATE)
    await _send_pcm_chunked(session, pcm)
    active.ctx.conversation_state.record_priya_turn(text)
    return _audio_dur_sec(pcm, EXOTEL_STREAM_SAMPLE_RATE)


# -- Outbound trigger endpoint ---------------------------------------------

class PlaceCallRequest(BaseModel):
    to: str = Field(..., description="E.164 lead number, e.g. +919876543210")
    from_: str | None = Field(None, alias="from", description="ExoPhone (defaults to EXOTEL_FROM_NUMBER)")
    lead_first_name: str | None = None
    lead_company: str | None = None
    lang_hint: str = "hi-IN"
    tenant_id: str = "default-tenant"
    lead_id: str | None = None


class PlaceCallResponse(BaseModel):
    call_sid: str
    status: str
    flow_url: str


@router.post("/exotel/calls", response_model=PlaceCallResponse)
async def trigger_outbound_call(req: PlaceCallRequest) -> PlaceCallResponse:
    """Place an outbound Exotel call. Returns the Exotel call_sid synchronously.

    Exotel dials the lead, then runs the App flow (EXOTEL_FLOW_URL) whose
    Voicebot applet opens a WebSocket to /exotel/stream/{call_id}. The
    applet's WSS URL is configured in App Bazaar (static); we correlate the
    pre-built context via CustomField=call_id, echoed in the start frame.
    """
    sid = os.environ.get("EXOTEL_SID", "")
    api_key = os.environ.get("EXOTEL_API_KEY", "")
    api_token = os.environ.get("EXOTEL_API_TOKEN", "")
    region = os.environ.get("EXOTEL_REGION", "")
    caller_id = req.from_ or os.environ.get("EXOTEL_FROM_NUMBER", "")
    flow_url = os.environ.get("EXOTEL_FLOW_URL", "").strip()
    if not (sid and api_key and api_token and caller_id and flow_url):
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "EXOTEL_SID / EXOTEL_API_KEY / EXOTEL_API_TOKEN / EXOTEL_FROM_NUMBER / "
            "EXOTEL_FLOW_URL must be set",
        )

    call_id = req.lead_id or f"call-{os.urandom(6).hex()}"

    # Pre-build the CallContext + dependencies. Keyed by call_id; picked up
    # when Exotel's WS connects and reports CustomField=call_id.
    deps = _build_deps_from_env()
    ctx = make_initial_context(
        call_id=call_id,
        tenant_id=req.tenant_id,
        lead_id=req.lead_id or call_id,
        lead_first_name=req.lead_first_name,
        lead_company=req.lead_company,
        default_lang=req.lang_hint,
    )
    db = _build_db_client()
    _active_calls[call_id] = _ActiveCall(ctx=ctx, slots=QualificationSlots(), deps=deps, db=db)
    global _last_pending_call_id
    _last_pending_call_id = call_id

    status_cb_base = os.environ.get("EXOTEL_STATUS_CALLBACK_URL", "").rstrip("/")
    status_callback = (
        f"{status_cb_base}/exotel/status/{call_id}" if status_cb_base else None
    )

    try:
        resp = await place_outbound_call(
            request=OutboundCallRequest(
                to=req.to,
                caller_id=caller_id,
                flow_url=flow_url,
                custom_field=call_id,
                status_callback=status_callback,
                record=True,
                time_limit_seconds=HARD_CAP_SECONDS,
            ),
            account_sid=sid,
            api_key=api_key,
            api_token=api_token,
            region=region,
        )
    except ExotelError as exc:
        _active_calls.pop(call_id, None)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"exotel: {exc}") from exc

    return PlaceCallResponse(
        call_sid=resp.call_sid, status=resp.status, flow_url=flow_url
    )


# -- WebSocket handler ------------------------------------------------------

def _resolve_active_call(path_call_id: str, custom_field: str | None) -> tuple[str, _ActiveCall]:
    """Find the pre-built call context for this WS connection.

    The Voicebot applet's WSS URL is static in App Bazaar, so the path
    `call_id` may be a placeholder (e.g. "live"). The authoritative key is
    the CustomField echoed in the start frame. Falls back to the path id,
    then bootstraps a fresh context so we never drop a live call.
    """
    for key in (custom_field, path_call_id, _last_pending_call_id):
        if key and key in _active_calls:
            if key == _last_pending_call_id and key not in (custom_field, path_call_id):
                logger.info("WS matched last-pending call_id=%s (name carried)", key)
            return key, _active_calls[key]

    call_id = custom_field or path_call_id
    logger.warning("WS start for unknown call_id=%s; bootstrapping default ctx", call_id)
    active = _ActiveCall(
        ctx=make_initial_context(
            call_id=call_id, tenant_id="unknown", lead_id=call_id,
            lead_first_name=None, lead_company=None, default_lang="hi-IN",
        ),
        slots=QualificationSlots(),
        deps=_build_deps_from_env(),
    )
    _active_calls[call_id] = active
    return call_id, active


@router.websocket("/exotel/stream/{call_id}")
async def exotel_stream(ws: WebSocket, call_id: str) -> None:
    """Exotel opens this WS when the call connects. Drives one full conversation."""
    await ws.accept()

    session = ExotelStreamSession(_FastapiWSAdapter(ws))
    active: _ActiveCall | None = None
    buffer = bytearray()
    silence_ms = 0
    buffered_ms = 0
    voiced_ms = 0  # how much actual (non-silent) speech is in the buffer
    # Wall-clock time until which Priya is still speaking; ignore inbound
    # audio until then so she doesn't transcribe her own echo.
    speaking_until = 0.0

    try:
        async for frame in session:
            if isinstance(frame, StreamStartFrame):
                call_id, active = _resolve_active_call(call_id, frame.custom_field)
                logger.info(
                    "call_id=%s stream_sid=%s started (custom_field=%s)",
                    call_id, frame.stream_sid, frame.custom_field,
                )
                # Priya opens the call — greet first, then wait for the lead.
                try:
                    intro = build_intro_text(
                        lang=active.ctx.language_state.current.value,
                        first_name=active.ctx.lead_first_name,
                    )
                    dur = await _play_text(session, active, intro)
                    speaking_until = time.monotonic() + dur
                    logger.info(
                        "call_id=%s intro played (%.1fs): %s", call_id, dur, intro[:60]
                    )
                except Exception:
                    logger.exception("call_id=%s intro playback failed", call_id)
                buffer.clear()
                buffered_ms = silence_ms = voiced_ms = 0
                continue
            if isinstance(frame, StreamStopFrame):
                logger.info("call_id=%s stopped: %s", call_id, frame.reason)
                break
            if not isinstance(frame, StreamMediaFrame):
                continue
            if active is None:
                # Media before start (shouldn't happen) — bootstrap from path.
                call_id, active = _resolve_active_call(call_id, None)

            chunk = frame.audio_bytes

            # Half-duplex: while Priya's reply is still playing (+ tail),
            # ignore everything — it's her own voice echoing back.
            if time.monotonic() < speaking_until + SPEAK_TAIL_SEC:
                buffer.clear()
                buffered_ms = silence_ms = voiced_ms = 0
                if active.ctx.should_hard_stop():
                    await session.send_clear()
                    break
                continue

            chunk_ms = _chunk_ms(chunk, EXOTEL_STREAM_SAMPLE_RATE)
            silent = _is_silent_pcm(chunk)
            if silent:
                silence_ms += chunk_ms
            else:
                silence_ms = 0
                voiced_ms += chunk_ms
            # Don't accumulate leading silence — only buffer once speech starts.
            if voiced_ms > 0:
                buffer.extend(chunk)
                buffered_ms += chunk_ms

            # Flush only when the lead actually spoke, then paused. Pure
            # silence never flushes → Priya stays quiet and waits naturally.
            should_flush = voiced_ms >= MIN_VOICED_MS and (
                silence_ms >= SILENCE_MS_THRESHOLD or buffered_ms >= MAX_BUFFER_MS
            )
            if not should_flush:
                if active.ctx.should_hard_stop():
                    await session.send_clear()
                    break
                continue

            # Run the STREAMING orchestrator — audio chunks arrive as
            # sentences are generated, so the lead hears Priya's first
            # sentence ~1.5s after they stop talking (vs 8-10s sequential).
            wav = exotel_pcm_to_wav_for_stt(bytes(buffer), EXOTEL_STREAM_SAMPLE_RATE)
            buffer.clear()
            buffered_ms = silence_ms = voiced_ms = 0

            streaming_deps = StreamingDependencies(
                stt=active.deps.stt,
                tts=active.deps.tts,
                llm=active.deps.llm,
                r2_reader=active.deps.r2_reader,
                r2_writer=active.deps.r2_writer,
                voice_id=active.deps.voice_id,
            )

            turn_end_call = False
            try:
                async for event in run_turn_streaming(
                    ctx=active.ctx,
                    audio_in=wav,
                    deps=streaming_deps,
                    prior_slots=active.slots,
                ):
                    if isinstance(event, AudioChunkEvent):
                        try:
                            out_pcm = tts_wav_to_exotel_pcm(
                                event.audio, EXOTEL_STREAM_SAMPLE_RATE
                            )
                            await _send_pcm_chunked(session, out_pcm)
                            speaking_until = (
                                max(speaking_until, time.monotonic())
                                + _audio_dur_sec(out_pcm, EXOTEL_STREAM_SAMPLE_RATE)
                            )
                        except Exception:
                            logger.exception("audio chunk send failed")
                    elif isinstance(event, TurnCompleteEvent):
                        active.slots = event.slots
                        turn_end_call = event.end_call
                        persist_turn_async(
                            active.db,
                            call_id=active.ctx.call_id,
                            tenant_id=active.ctx.tenant_id,
                            lead_id=active.ctx.lead_id,
                            turn_idx=active.ctx.turn_idx - 1,
                            lead_text=event.lead_text,
                            lead_lang=event.lead_lang,
                            priya_text=event.priya_full_text,
                            slots_row=event.slots.to_db_row(
                                call_id=active.ctx.call_id,
                                tenant_id=active.ctx.tenant_id,
                                lead_id=active.ctx.lead_id,
                                turn_idx=active.ctx.turn_idx - 1,
                            ),
                            latency=event.latency_ms,
                        )
            except Exception:
                logger.exception("call_id=%s streaming orchestrator failure", call_id)

            # Priya just spoke — reset capture. speaking_until (set per audio
            # chunk above) already keeps us deaf until her reply finishes.
            buffer.clear()
            buffered_ms = silence_ms = voiced_ms = 0

            if turn_end_call:
                # Let the goodbye line finish playing, then hang up by
                # closing the WS (Exotel ends the call on disconnect).
                wait = speaking_until - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait + 0.3)
                logger.info("call_id=%s hanging up (end_call)", call_id)
                break

            if active.ctx.should_hard_stop():
                await session.send_clear()
                break
    except WebSocketDisconnect:
        logger.info("call_id=%s WS disconnected", call_id)
    finally:
        # Keep the slots/context for a short grace period so a webhook can
        # still read final state; in production push to DB instead.
        if active is not None:
            logger.info(
                "call_id=%s ended elapsed=%.1fs turns=%d cache_hits=%d billed_units=%d",
                call_id,
                active.ctx.elapsed(),
                active.ctx.turn_idx,
                active.ctx.phrase_cache_hits,
                active.ctx.billed_units(),
            )
        else:
            logger.info("call_id=%s ended before stream start (no audio)", call_id)


# -- StatusCallback webhook (Exotel POSTs after call ends) -----------------

@router.post("/exotel/status/{call_id}")
async def exotel_status_callback(call_id: str, request: Any = None) -> dict:
    """Exotel POSTs call completion data here (duration, status, recording URL).

    We merge it with the in-memory call state and persist to Supabase.
    This is the CRM integration point — each tenant's call outcome lands here.
    """
    from fastapi import Request as FastAPIRequest

    # Exotel sends form-encoded POST with: CallSid, Status, Duration,
    # RecordingUrl, From, To, Direction, StartTime, EndTime, etc.
    active = _active_calls.get(call_id)
    if active is None:
        logger.warning("status callback for unknown call_id=%s", call_id)
        return {"status": "ok", "call_id": call_id, "warning": "unknown_call"}

    logger.info(
        "call_id=%s status_callback: elapsed=%.1fs turns=%d billed=%d slots=%s",
        call_id,
        active.ctx.elapsed(),
        active.ctx.turn_idx,
        active.ctx.billed_units(),
        active.slots.to_summary(),
    )

    if active.db:
        try:
            await active.db.update_call_status(
                call_id=call_id,
                tenant_id=active.ctx.tenant_id,
                status="completed",
                billed_units=active.ctx.billed_units(),
                duration_sec=active.ctx.elapsed(),
                turns=active.ctx.turn_idx,
            )
        except Exception:
            logger.exception("call_id=%s failed to persist final status", call_id)

    _active_calls.pop(call_id, None)
    return {"status": "ok", "call_id": call_id}


# -- Adapters ---------------------------------------------------------------

class _FastapiWSAdapter:
    """Wrap FastAPI's WebSocket to look like the WebSocketLike protocol the
    ExotelStreamSession expects (which uses send/recv text)."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send(self, data: str) -> None:
        await self._ws.send_text(data)

    async def recv(self) -> str:
        return await self._ws.receive_text()


def _build_deps_from_env() -> TurnDependencies:
    """Build TurnDependencies from env vars. Mirrors local_audio._build_deps so
    the phone path uses the SAME low-latency stack as the local harness:
    Sarvam STT + Groq Llama-4-Scout LLM + Cartesia Sonic-3.5 TTS.

    Each call gets fresh httpx clients; in production we'd pool these but
    per-call clients keep tests trivial."""
    from .local_audio import (
        _CartesiaTTSAdapter,
        _ElevenLabsTTSAdapter,
        _GeminiAdapter,
        _GroqAdapter,
        _HybridTTSAdapter,
        _NoOpR2,
        _SarvamSTTAdapter,
        _SarvamTTSAdapter,
    )

    sarvam_key = os.environ.get("SARVAM_API_KEY", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    gemini_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    groq_key = os.environ.get("GROQ_API_KEY", "")
    groq_model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    cartesia_key = os.environ.get("CARTESIA_API_KEY", "")
    cartesia_voice = os.environ.get("CARTESIA_VOICE", "arushi")
    eleven_key = os.environ.get("ELEVENLABS_API_KEY", "")
    eleven_voice = os.environ.get("ELEVENLABS_VOICE_ID", "")
    eleven_model = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")
    if not sarvam_key:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "SARVAM_API_KEY must be set (STT)",
        )
    if not groq_key and not gemini_key:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "GROQ_API_KEY or GEMINI_API_KEY must be set (LLM)",
        )

    http = httpx.AsyncClient(timeout=15.0)

    try:
        r2_cfg = R2Config.from_env()
        r2 = R2Client(r2_cfg)
        r2_reader: Any = r2
        r2_writer: Any = r2
    except R2ConfigError as exc:
        logger.warning("R2 disabled (%s); phrase cache will always miss", exc)
        r2_reader = _NoOpR2()
        r2_writer = _NoOpR2()

    if groq_key:
        llm_adapter: Any = _GroqAdapter(
            api_key=groq_key, model=groq_model, client=http,
            gemini_key=gemini_key, gemini_model=gemini_model,
        )
    else:
        llm_adapter = _GeminiAdapter(api_key=gemini_key, model=gemini_model, client=http)

    if eleven_key:
        el_adapter = _ElevenLabsTTSAdapter(
            api_key=eleven_key, client=http,
            voice_id=eleven_voice or "EXAVITQu4vr4xnSDxMaL", model=eleven_model,
        )
        if cartesia_key:
            # Hindi/English → ElevenLabs (realism); Tamil → Cartesia nithya.
            tts_adapter: Any = _HybridTTSAdapter(
                primary=el_adapter,
                tamil=_CartesiaTTSAdapter(api_key=cartesia_key, client=http, voice="nithya"),
            )
        else:
            tts_adapter = el_adapter
    elif cartesia_key:
        tts_adapter = _CartesiaTTSAdapter(api_key=cartesia_key, client=http, voice=cartesia_voice)
    else:
        tts_adapter = _SarvamTTSAdapter(api_key=sarvam_key, client=http)

    return TurnDependencies(
        stt=_SarvamSTTAdapter(api_key=sarvam_key, client=http),
        tts=tts_adapter,
        llm=llm_adapter,
        r2_reader=r2_reader,
        r2_writer=r2_writer,
    )


def _build_db_client() -> AgentSupabaseClient | None:
    """Build Supabase client from env. Returns None if unconfigured."""
    try:
        cfg = SupabaseConfig.from_env()
        return AgentSupabaseClient(cfg)
    except SupabaseConfigError as exc:
        logger.warning("Supabase disabled (%s); no DB persistence", exc)
        return None
