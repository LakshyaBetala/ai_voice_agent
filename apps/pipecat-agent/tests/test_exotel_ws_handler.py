"""Tests for the FastAPI Exotel WS handler.

We don't spin up real Sarvam/Gemini — the orchestrator dependencies are
injected by replacing _build_deps_from_env. We DO use TestClient to drive
the WebSocket end to end.
"""
from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from voice_agent import exotel_ws_handler
from voice_agent.audio_codec import pcm16_to_mulaw
from voice_agent.exotel_transport import (
    base_url_for_region,
    EXOTEL_BASE_DEFAULT,
    EXOTEL_BASE_SG,
    EXOTEL_BASE_US,
)
from voice_agent.qualification import QualificationSlots
from voice_agent.turn_orchestrator import TurnDependencies


# -- Region helper ---------------------------------------------------------

def test_region_helper_routes_sg_singapore_and_us():
    assert base_url_for_region("sg") == EXOTEL_BASE_SG
    assert base_url_for_region("Singapore") == EXOTEL_BASE_SG
    assert base_url_for_region("us") == EXOTEL_BASE_US
    assert base_url_for_region(None) == EXOTEL_BASE_DEFAULT
    assert base_url_for_region("in") == EXOTEL_BASE_DEFAULT  # falls back


# -- WS handler: end-to-end with fake deps --------------------------------

class FakeSTT:
    def __init__(self, transcript: str = "Haan ji"):
        self.transcript = transcript

    async def transcribe(self, audio: bytes):
        from voice_agent.sarvam_stt import STTResult
        return STTResult(self.transcript, "hi-IN", 0.95, "req-1")


class FakeTTS:
    async def synth(self, text: str, lang: str) -> bytes:
        # Return a tiny WAV the codec can convert to μ-law without issue.
        from voice_agent.audio_codec import pcm16_to_wav
        return pcm16_to_wav(b"\x00\x00\x00\x00", 8000)


class FakeLLM:
    async def respond(self, system_message: str, user_message: str) -> str:
        return "Theek hai, batayie."

    async def stream_respond(self, system_message: str, user_message: str):
        yield "Theek hai, batayie."

    async def extract(self, prompt: str) -> str:
        return '{"buying_confidence": 0.5}'


class FakeR2:
    async def get(self, key: str) -> bytes | None:
        return None

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        return None


def _fake_deps() -> TurnDependencies:
    return TurnDependencies(
        stt=FakeSTT(),
        tts=FakeTTS(),
        llm=FakeLLM(),
        r2_reader=FakeR2(),
        r2_writer=FakeR2(),
    )


@pytest.fixture
def app_with_fakes(monkeypatch):
    """Mount the router on a fresh app + monkeypatch deps builder."""
    from fastapi import FastAPI
    monkeypatch.setattr(exotel_ws_handler, "_build_deps_from_env", _fake_deps)
    # Reset the in-memory registry between tests.
    exotel_ws_handler._active_calls.clear()
    app = FastAPI()
    app.include_router(exotel_ws_handler.router)
    return app


def _media_frame(audio: bytes) -> str:
    return json.dumps({
        "event": "media",
        "media": {"payload": base64.b64encode(audio).decode()},
    })


def _start_frame(call_id: str = "ws-1") -> str:
    return json.dumps({
        "event": "start",
        "stream_sid": f"ss-{call_id}",
        "call_sid": call_id,
    })


def _stop_frame() -> str:
    return json.dumps({"event": "stop"})


def _noisy_chunk(byte_count: int = 160) -> bytes:
    """Build a chunk that won't trip the silence VAD (loud edge bytes)."""
    return bytes([0x40, 0xC0] * (byte_count // 2))


def _silent_chunk(byte_count: int = 160) -> bytes:
    return bytes([0x7F] * byte_count)


def test_ws_handler_runs_one_turn_on_silence_flush(app_with_fakes):
    """Send start + 25 noisy + 40 silent chunks → orchestrator runs once and we
    receive Priya's μ-law audio back, then stop frame ends the call."""
    client = TestClient(app_with_fakes)

    with client.websocket_connect("/exotel/stream/test-call-1") as ws:
        ws.send_text(_start_frame("test-call-1"))
        # 25 noisy chunks (500ms speech) → above MIN_UTTERANCE_MS
        for _ in range(25):
            ws.send_text(_media_frame(_noisy_chunk()))
        # 40 silent chunks (800ms quiet) → triggers SILENCE_MS_THRESHOLD flush
        for _ in range(40):
            ws.send_text(_media_frame(_silent_chunk()))

        # Expect at least one outbound media frame from Priya before we send stop.
        outbound = ws.receive_text()
        msg = json.loads(outbound)
        assert msg["event"] == "media"
        assert "payload" in msg["media"]

        ws.send_text(_stop_frame())


def test_ws_handler_skips_buffers_shorter_than_min_utterance(app_with_fakes):
    """A tiny burst of noise + silence should NOT trigger the orchestrator."""
    client = TestClient(app_with_fakes)

    with client.websocket_connect("/exotel/stream/tiny-1") as ws:
        ws.send_text(_start_frame("tiny-1"))
        # Only 5 noisy chunks = 100ms, below MIN_UTTERANCE_MS=400ms
        for _ in range(5):
            ws.send_text(_media_frame(_noisy_chunk()))
        for _ in range(40):
            ws.send_text(_media_frame(_silent_chunk()))
        ws.send_text(_stop_frame())
        # Connection should close cleanly without any outbound media.


def test_ws_handler_handles_unknown_call_id_by_bootstrapping(app_with_fakes):
    """A WS connection without a pre-registered call_id must not crash."""
    client = TestClient(app_with_fakes)

    with client.websocket_connect("/exotel/stream/never-registered") as ws:
        ws.send_text(_start_frame("never-registered"))
        ws.send_text(_stop_frame())

    # The handler should have created an entry in _active_calls for it.
    assert "never-registered" in exotel_ws_handler._active_calls


# -- Outbound trigger endpoint -------------------------------------------

def test_trigger_outbound_call_returns_500_when_creds_missing(monkeypatch, app_with_fakes):
    monkeypatch.delenv("EXOTEL_SID", raising=False)
    client = TestClient(app_with_fakes)
    resp = client.post("/exotel/calls", json={"to": "+919876543210"})
    assert resp.status_code == 500
    assert "EXOTEL_SID" in resp.json()["detail"]


def test_trigger_outbound_call_calls_exotel_and_registers_context(monkeypatch, app_with_fakes):
    """The trigger endpoint should hit Exotel, register the call, and return the sid."""
    monkeypatch.setenv("EXOTEL_SID", "almmatix1")
    monkeypatch.setenv("EXOTEL_API_KEY", "k")
    monkeypatch.setenv("EXOTEL_API_TOKEN", "t")
    monkeypatch.setenv("EXOTEL_REGION", "sg")
    monkeypatch.setenv("EXOTEL_FROM_NUMBER", "04446973311")
    monkeypatch.setenv("EXOTEL_STREAM_URL", "wss://agent.almmatix.in")

    captured = {}

    async def fake_place(*, request, account_sid, api_key, api_token, region=None, client=None, timeout=10.0):
        from voice_agent.exotel_transport import OutboundCallResponse
        captured["to"] = request.to
        captured["from"] = request.from_
        captured["stream_url"] = request.stream_url
        captured["region"] = region
        return OutboundCallResponse(call_sid="exo-123", status="queued", raw={})

    monkeypatch.setattr(exotel_ws_handler, "place_outbound_call", fake_place)

    client = TestClient(app_with_fakes)
    resp = client.post(
        "/exotel/calls",
        json={
            "to": "+919876543210",
            "lead_first_name": "Suresh",
            "lead_company": "Acme",
            "lang_hint": "hi-IN",
            "lead_id": "lead-42",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["call_sid"] == "exo-123"
    assert body["status"] == "queued"
    assert body["stream_url"].endswith("/exotel/stream/lead-42")
    assert captured["to"] == "+919876543210"
    assert captured["from"] == "04446973311"
    assert captured["region"] == "sg"
    # The context should have been pre-registered under the lead_id.
    assert "lead-42" in exotel_ws_handler._active_calls


def test_trigger_outbound_call_bubbles_exotel_error_as_502(monkeypatch, app_with_fakes):
    monkeypatch.setenv("EXOTEL_SID", "almmatix1")
    monkeypatch.setenv("EXOTEL_API_KEY", "k")
    monkeypatch.setenv("EXOTEL_API_TOKEN", "t")
    monkeypatch.setenv("EXOTEL_STREAM_URL", "wss://x")

    async def fake_place(**kwargs):
        from voice_agent.exotel_transport import ExotelError
        raise ExotelError("rate limited")

    monkeypatch.setattr(exotel_ws_handler, "place_outbound_call", fake_place)

    client = TestClient(app_with_fakes)
    resp = client.post("/exotel/calls", json={"to": "+91", "lead_id": "x"})
    assert resp.status_code == 502
    assert "rate limited" in resp.json()["detail"]
    # And the half-registered context should have been cleaned up.
    assert "x" not in exotel_ws_handler._active_calls
