"""Tests for the Sarvam Bulbul v3 TTS adapter."""
from __future__ import annotations

import base64
import json

import httpx
import pytest

from voice_agent.sarvam_tts import (
    DEFAULT_MODEL,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_SPEAKER,
    SARVAM_TTS_URL,
    SarvamTTSError,
    _extract_audio,
    make_phrase_synthesizer,
    synthesize,
)


def test_extract_audio_handles_list_form():
    encoded = base64.b64encode(b"WAV-DATA").decode()
    assert _extract_audio({"audios": [encoded]}) == b"WAV-DATA"


def test_extract_audio_handles_string_form():
    encoded = base64.b64encode(b"WAV-DATA").decode()
    assert _extract_audio({"audios": encoded}) == b"WAV-DATA"


def test_extract_audio_rejects_empty_list():
    with pytest.raises(SarvamTTSError, match="empty"):
        _extract_audio({"audios": []})


def test_extract_audio_rejects_bad_base64():
    with pytest.raises(SarvamTTSError, match="b64"):
        _extract_audio({"audios": ["%%not-base64%%"]})


@pytest.mark.asyncio
async def test_synthesize_posts_correct_body():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["header"] = request.headers.get("api-subscription-key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"audios": [base64.b64encode(b"WAV").decode()], "request_id": "r1"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await synthesize(
            text="Haan ji",
            lang="hi-IN",
            api_key="sk_test",
            client=client,
        )

    assert captured["url"] == SARVAM_TTS_URL
    assert captured["header"] == "sk_test"
    assert captured["body"]["target_language_code"] == "hi-IN"
    assert captured["body"]["speaker"] == DEFAULT_SPEAKER
    assert captured["body"]["model"] == DEFAULT_MODEL
    assert captured["body"]["speech_sample_rate"] == DEFAULT_SAMPLE_RATE
    assert captured["body"]["inputs"] == ["Haan ji"]
    assert res.audio == b"WAV"
    assert res.request_id == "r1"


@pytest.mark.asyncio
async def test_synthesize_rejects_empty_text():
    with pytest.raises(SarvamTTSError, match="empty"):
        await synthesize(text="   ", lang="hi-IN", api_key="sk")


@pytest.mark.asyncio
async def test_synthesize_rejects_missing_key():
    with pytest.raises(SarvamTTSError, match="missing"):
        await synthesize(text="ok", lang="hi-IN", api_key="")


@pytest.mark.asyncio
async def test_synthesize_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SarvamTTSError, match="429"):
            await synthesize(text="x", lang="hi-IN", api_key="sk", client=client)


@pytest.mark.asyncio
async def test_phrase_synthesizer_returns_bytes_and_matches_protocol():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"audios": [base64.b64encode(b"OK").decode()]}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        synth = make_phrase_synthesizer(api_key="sk_test", client=client)
        out = await synth("Achha", "hi-IN")
    assert out == b"OK"
