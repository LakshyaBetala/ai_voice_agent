"""G.711 μ-law codec + WAV stripping helpers.

Exotel and Plivo both deliver call audio as 8 kHz μ-law (mu-law / G.711).
Sarvam STT accepts WAV PCM. Sarvam TTS returns 8 kHz WAV PCM. So per turn:

  Lead → Exotel (μ-law) → us → PCM-WAV → Sarvam STT
  Sarvam TTS → PCM-WAV → us → μ-law → Exotel → Lead

This module owns the conversion. Stdlib only — no audioop in 3.13+ so we
ship a tiny pure-Python implementation. Performance is fine for one
call's worth of 8 kHz mono audio (~8 KB/s).
"""
from __future__ import annotations

import io
import struct
import wave

MU_LAW_BIAS = 0x84
MU_LAW_CLIP = 32635

# G.711 μ-law segment end-points (after BIAS add). Standard table.
_SEG_END = (0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF)


def pcm16_to_mulaw(pcm: bytes) -> bytes:
    """Convert signed-16 PCM bytes (little-endian) to G.711 μ-law bytes.

    1 PCM sample (2 bytes) → 1 μ-law byte. Spec: ITU-T G.711.
    """
    out = bytearray(len(pcm) // 2)
    for i in range(0, len(pcm) - 1, 2):
        sample = struct.unpack_from("<h", pcm, i)[0]
        if sample < 0:
            sample = -sample
            sign = 0x80
        else:
            sign = 0
        if sample > MU_LAW_CLIP:
            sample = MU_LAW_CLIP
        sample = sample + MU_LAW_BIAS
        # Find segment by table lookup (G.711 standard).
        seg = 7
        for idx, end in enumerate(_SEG_END):
            if sample <= end:
                seg = idx
                break
        mantissa = (sample >> (seg + 3)) & 0x0F
        byte = ~(sign | (seg << 4) | mantissa) & 0xFF
        out[i // 2] = byte
    return bytes(out)


def mulaw_to_pcm16(mu_law: bytes) -> bytes:
    """Convert G.711 μ-law bytes to signed-16 PCM bytes (little-endian).

    1 μ-law byte → 1 PCM sample (2 bytes).
    """
    out = bytearray(len(mu_law) * 2)
    for i, b in enumerate(mu_law):
        b = ~b & 0xFF
        sign = b & 0x80
        seg = (b & 0x70) >> 4
        mantissa = b & 0x0F
        sample = ((mantissa << 3) + MU_LAW_BIAS) << seg
        sample = sample - MU_LAW_BIAS
        if sign:
            sample = -sample
        struct.pack_into("<h", out, i * 2, sample)
    return bytes(out)


def wav_to_pcm16(wav_bytes: bytes) -> tuple[bytes, int]:
    """Strip WAV header. Returns (raw PCM bytes, sample rate)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as r:
        if r.getsampwidth() != 2:
            raise ValueError("WAV must be 16-bit PCM")
        if r.getnchannels() != 1:
            raise ValueError("WAV must be mono")
        return r.readframes(r.getnframes()), r.getframerate()


def pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap PCM in a WAV container for Sarvam STT."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def tts_wav_to_mulaw_8k(wav_bytes: bytes) -> bytes:
    """End-to-end: Sarvam TTS WAV → μ-law ready to send to Exotel.

    Sarvam returns 8 kHz mono PCM-WAV when we request speech_sample_rate=8000.
    If Sarvam ever returns 16 kHz we naive-downsample by dropping every
    other sample — adequate for telephony, but log this so we can move
    to a real resampler later.
    """
    pcm, sr = wav_to_pcm16(wav_bytes)
    if sr == 8000:
        return pcm16_to_mulaw(pcm)
    if sr == 16000:
        # Naive 2:1 downsample. Sufficient for PSTN 8 kHz; quality loss
        # is below telephony codec floor anyway.
        downsampled = bytearray(len(pcm) // 2)
        for i in range(0, len(pcm) - 3, 4):
            downsampled[i // 2] = pcm[i]
            downsampled[i // 2 + 1] = pcm[i + 1]
        return pcm16_to_mulaw(bytes(downsampled))
    raise ValueError(f"unsupported TTS sample rate: {sr} (expected 8000 or 16000)")


def mulaw_to_wav_for_stt(mu_law: bytes, sample_rate: int = 8000) -> bytes:
    """End-to-end: Exotel μ-law chunk → WAV ready to send to Sarvam STT."""
    pcm = mulaw_to_pcm16(mu_law)
    return pcm16_to_wav(pcm, sample_rate)
