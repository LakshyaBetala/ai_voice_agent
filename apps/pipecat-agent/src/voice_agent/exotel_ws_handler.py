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

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

from .audio_codec import mulaw_to_wav_for_stt, tts_wav_to_mulaw_8k
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
from .qualification import QualificationSlots
from .r2_client import R2Client, R2Config, R2ConfigError
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


# -- Inbound-audio buffering (simple silence VAD) ---------------------------

# Exotel sends 20ms μ-law chunks (160 bytes each at 8 kHz). We accumulate
# them and flush to STT when either:
#   - silence threshold met (low RMS for `SILENCE_MS_THRESHOLD`)
#   - hard buffer cap reached (avoids runaway when lead never pauses)

SILENCE_MS_THRESHOLD = 700  # ms of quiet → assume utterance ended
MAX_BUFFER_MS = 8000         # hard cap so STT call doesn't grow unbounded
MIN_UTTERANCE_MS = 400       # noise floor — drop buffers shorter than this
EXOTEL_CHUNK_MS = 20         # Exotel's framing

# μ-law amplitude threshold below which a chunk is "silent". μ-law 0xFF/0x00
# encodes near-zero, edges of the byte represent loud audio.
def _is_silent_chunk(mu_law: bytes, threshold: int = 8) -> bool:
    """Rough VAD: count how many bytes are near the silent midpoint."""
    if not mu_law:
        return True
    near_silent = sum(1 for b in mu_law if abs(b - 0x7F) <= threshold or abs(b - 0x80) <= threshold)
    return near_silent > len(mu_law) * 0.85


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
    stream_url: str


@router.post("/exotel/calls", response_model=PlaceCallResponse)
async def trigger_outbound_call(req: PlaceCallRequest) -> PlaceCallResponse:
    """Place an outbound Exotel call. Returns the Exotel call_sid synchronously.

    Audio flow begins when Exotel WS connects to /exotel/stream/{call_id}.
    """
    sid = os.environ.get("EXOTEL_SID", "")
    api_key = os.environ.get("EXOTEL_API_KEY", "")
    api_token = os.environ.get("EXOTEL_API_TOKEN", "")
    region = os.environ.get("EXOTEL_REGION", "")
    from_default = os.environ.get("EXOTEL_FROM_NUMBER", "")
    stream_base = os.environ.get("EXOTEL_STREAM_URL", "").rstrip("/")
    if not (sid and api_key and api_token and stream_base):
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "EXOTEL_SID / EXOTEL_API_KEY / EXOTEL_API_TOKEN / EXOTEL_STREAM_URL must be set",
        )

    call_id = req.lead_id or f"call-{os.urandom(6).hex()}"
    stream_url = f"{stream_base}/exotel/stream/{call_id}"

    # Pre-build the CallContext + dependencies. They're keyed by call_id and
    # picked up when Exotel's WS connects.
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

    try:
        resp = await place_outbound_call(
            request=OutboundCallRequest(
                to=req.to,
                from_=req.from_ or from_default,
                stream_url=stream_url,
                custom_field=call_id,
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
        call_sid=resp.call_sid, status=resp.status, stream_url=stream_url
    )


# -- WebSocket handler ------------------------------------------------------

@router.websocket("/exotel/stream/{call_id}")
async def exotel_stream(ws: WebSocket, call_id: str) -> None:
    """Exotel opens this WS when the call connects. Drives one full conversation."""
    await ws.accept()

    active = _active_calls.get(call_id)
    if active is None:
        # Late connection / restart. Build a fresh context so we don't drop
        # the call — better to qualify generically than reject.
        logger.warning("WS connected for unknown call_id=%s; bootstrapping default ctx", call_id)
        active = _ActiveCall(
            ctx=make_initial_context(
                call_id=call_id, tenant_id="unknown", lead_id=call_id,
                lead_first_name=None, lead_company=None, default_lang="hi-IN",
            ),
            slots=QualificationSlots(),
            deps=_build_deps_from_env(),
        )
        _active_calls[call_id] = active

    session = ExotelStreamSession(_FastapiWSAdapter(ws))
    buffer = bytearray()
    silence_ms = 0
    buffered_ms = 0

    try:
        async for frame in session:
            if isinstance(frame, StreamStartFrame):
                logger.info("call_id=%s stream_sid=%s started", call_id, frame.stream_sid)
                continue
            if isinstance(frame, StreamStopFrame):
                logger.info("call_id=%s stopped: %s", call_id, frame.reason)
                break
            if not isinstance(frame, StreamMediaFrame):
                continue

            chunk = frame.audio_bytes
            buffer.extend(chunk)
            buffered_ms += EXOTEL_CHUNK_MS
            if _is_silent_chunk(chunk):
                silence_ms += EXOTEL_CHUNK_MS
            else:
                silence_ms = 0

            should_flush = (
                (silence_ms >= SILENCE_MS_THRESHOLD and buffered_ms >= MIN_UTTERANCE_MS)
                or buffered_ms >= MAX_BUFFER_MS
            )
            if not should_flush:
                # Hard-stop check between chunks so we don't run past the cap.
                if active.ctx.should_hard_stop():
                    await session.send_clear()
                    break
                continue

            # Run the orchestrator on this utterance.
            wav = mulaw_to_wav_for_stt(bytes(buffer), sample_rate=8000)
            buffer.clear()
            buffered_ms = 0
            silence_ms = 0

            try:
                result = await run_turn(
                    ctx=active.ctx,
                    audio_in=wav,
                    deps=active.deps,
                    prior_slots=active.slots,
                )
            except Exception:
                logger.exception("call_id=%s orchestrator failure; continuing", call_id)
                continue

            active.slots = result.slots

            persist_turn_async(
                active.db,
                call_id=active.ctx.call_id,
                tenant_id=active.ctx.tenant_id,
                lead_id=active.ctx.lead_id,
                turn_idx=active.ctx.turn_idx - 1,
                lead_text=result.lead_text,
                lead_lang=result.lead_lang,
                priya_text=result.priya_text,
                slots_row=result.slots.to_db_row(
                    call_id=active.ctx.call_id,
                    tenant_id=active.ctx.tenant_id,
                    lead_id=active.ctx.lead_id,
                    turn_idx=active.ctx.turn_idx - 1,
                ),
                latency=result.latency_ms,
            )

            if result.bridge_audio:
                try:
                    await session.send_audio(tts_wav_to_mulaw_8k(result.bridge_audio))
                except Exception:
                    logger.exception("bridge audio send failed")

            try:
                await session.send_audio(tts_wav_to_mulaw_8k(result.priya_audio))
            except Exception:
                logger.exception("priya audio send failed")

            if active.ctx.should_hard_stop():
                await session.send_clear()
                break
    except WebSocketDisconnect:
        logger.info("call_id=%s WS disconnected", call_id)
    finally:
        # Keep the slots/context for a short grace period so a webhook can
        # still read final state; in production push to DB instead.
        logger.info(
            "call_id=%s ended elapsed=%.1fs turns=%d cache_hits=%d billed_units=%d",
            call_id,
            active.ctx.elapsed(),
            active.ctx.turn_idx,
            active.ctx.phrase_cache_hits,
            active.ctx.billed_units(),
        )


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
    """Build TurnDependencies from env vars. Each call gets fresh httpx clients
    in production we'd pool these but per-call clients keep tests trivial."""
    from .local_audio import _GeminiAdapter, _NoOpR2, _SarvamSTTAdapter, _SarvamTTSAdapter

    sarvam_key = os.environ.get("SARVAM_API_KEY", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    gemini_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    if not sarvam_key or not gemini_key:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "SARVAM_API_KEY and GEMINI_API_KEY must be set",
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

    return TurnDependencies(
        stt=_SarvamSTTAdapter(api_key=sarvam_key, client=http),
        tts=_SarvamTTSAdapter(api_key=sarvam_key, client=http),
        llm=_GeminiAdapter(api_key=gemini_key, model=gemini_model, client=http),
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
