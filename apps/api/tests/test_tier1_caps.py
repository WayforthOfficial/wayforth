"""tests/test_tier1_caps.py — Unit tests for Tier 1 per-call input caps.

Pure-unit: no network, no DB, no live deployment required.

Services covered:
  - DeepL:       2,000-char text limit → HTTP 413 on breach
  - ElevenLabs:  500-char text limit   → HTTP 413 on breach
  - AssemblyAI:  ~10-min audio limit (12 MB Content-Length heuristic) → HTTP 413 on breach
  - Stability:   1 image per call      → HTTP 413 when samples/n > 1

Run: pytest apps/api/tests/test_tier1_caps.py -v
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Allow imports from apps/api
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException
from services.managed import (
    call_assemblyai,
    call_deepl,
    call_elevenlabs,
    call_stability,
)

_FAKE_KEY = "test-api-key"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_mock_response(status_code: int, json_data: dict | None = None, content: bytes = b"") -> MagicMock:
    """Build a MagicMock that looks like an httpx Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.content = content
    resp.text = ""
    resp.headers = {}
    return resp


def _patch_async_client(responses: list):
    """Return a patch for httpx.AsyncClient that yields mock responses in order.

    Each call to client.post / client.get / client.head returns the next
    response in the list.
    """
    call_state = {"i": 0}

    async def _next_response(*args, **kwargs):
        idx = call_state["i"]
        call_state["i"] += 1
        return responses[idx]

    client_mock = MagicMock()
    client_mock.post = AsyncMock(side_effect=_next_response)
    client_mock.get = AsyncMock(side_effect=_next_response)
    client_mock.head = AsyncMock(side_effect=_next_response)

    ctx_mock = MagicMock()
    ctx_mock.__aenter__ = AsyncMock(return_value=client_mock)
    ctx_mock.__aexit__ = AsyncMock(return_value=False)

    return patch("services.managed.httpx.AsyncClient", return_value=ctx_mock), client_mock


# ─────────────────────────────────────────────────────────────────────────────
# DeepL
# ─────────────────────────────────────────────────────────────────────────────


async def test_deepl_413_when_text_exceeds_2000_chars():
    text = "x" * 2001
    with pytest.raises(HTTPException) as exc_info:
        await call_deepl({"text": text, "target_lang": "DE"}, _FAKE_KEY)
    assert exc_info.value.status_code == 413
    assert "2,000" in exc_info.value.detail


async def test_deepl_200_when_text_is_exactly_2000_chars():
    text = "x" * 2000
    deepl_resp = _make_mock_response(
        200,
        json_data={"translations": [{"text": "translated", "detected_source_language": "EN"}]},
    )
    patcher, _ = _patch_async_client([deepl_resp])
    with patcher:
        result = await call_deepl({"text": text, "target_lang": "DE"}, _FAKE_KEY)
    assert result["translated_text"] == "translated"


# ─────────────────────────────────────────────────────────────────────────────
# ElevenLabs
# ─────────────────────────────────────────────────────────────────────────────


async def test_elevenlabs_413_when_text_exceeds_500_chars():
    text = "y" * 501
    with pytest.raises(HTTPException) as exc_info:
        await call_elevenlabs({"text": text}, _FAKE_KEY)
    assert exc_info.value.status_code == 413
    assert "500" in exc_info.value.detail


async def test_elevenlabs_200_when_text_is_exactly_500_chars():
    text = "y" * 500
    audio_bytes = b"\xff\xfb\x90\x00" * 10  # fake MP3 bytes
    tts_resp = _make_mock_response(200, content=audio_bytes)
    tts_resp.content = audio_bytes
    patcher, _ = _patch_async_client([tts_resp])
    with patcher:
        result = await call_elevenlabs({"text": text}, _FAKE_KEY)
    assert result["characters"] == 500
    assert result["content_type"] == "audio/mpeg"


# ─────────────────────────────────────────────────────────────────────────────
# AssemblyAI
# ─────────────────────────────────────────────────────────────────────────────


async def test_assemblyai_413_when_head_returns_large_content_length():
    head_resp = MagicMock()
    head_resp.status_code = 200
    head_resp.headers = {"content-length": "13000000"}  # 13 MB > 12 MB limit

    # head_client and main_client share the same AsyncClient mock in sequence.
    # First context manager invocation → HEAD client, second → POST + poll clients.
    call_count = {"n": 0}

    head_client_mock = MagicMock()
    head_client_mock.head = AsyncMock(return_value=head_resp)

    head_ctx = MagicMock()
    head_ctx.__aenter__ = AsyncMock(return_value=head_client_mock)
    head_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("services.managed.httpx.AsyncClient", return_value=head_ctx):
        with pytest.raises(HTTPException) as exc_info:
            await call_assemblyai({"audio_url": "https://example.com/audio.mp3"}, _FAKE_KEY)

    assert exc_info.value.status_code == 413
    assert "10 min" in exc_info.value.detail


async def test_assemblyai_passes_through_when_head_fails():
    """When the HEAD request raises an exception, the cap check is bypassed and
    the transcript job is submitted normally."""
    head_client_mock = MagicMock()
    head_client_mock.head = AsyncMock(side_effect=Exception("connection error"))

    head_ctx = MagicMock()
    head_ctx.__aenter__ = AsyncMock(return_value=head_client_mock)
    head_ctx.__aexit__ = AsyncMock(return_value=False)

    # After HEAD fails, AssemblyAI submit + poll calls fire.
    submit_resp = _make_mock_response(200, json_data={"id": "abc123"})
    poll_resp = _make_mock_response(
        200, json_data={"status": "completed", "text": "hello world"}
    )

    main_call_count = {"n": 0}

    async def _main_responses(*args, **kwargs):
        n = main_call_count["n"]
        main_call_count["n"] += 1
        return [submit_resp, poll_resp][n]

    main_client_mock = MagicMock()
    main_client_mock.post = AsyncMock(side_effect=_main_responses)
    main_client_mock.get = AsyncMock(return_value=poll_resp)

    main_ctx = MagicMock()
    main_ctx.__aenter__ = AsyncMock(return_value=main_client_mock)
    main_ctx.__aexit__ = AsyncMock(return_value=False)

    # First AsyncClient() call → HEAD client (fails), subsequent calls → main client
    side_effect_order = [head_ctx, main_ctx, main_ctx]
    effect_state = {"i": 0}

    def _client_factory(*args, **kwargs):
        i = effect_state["i"]
        effect_state["i"] += 1
        return side_effect_order[i]

    with patch("services.managed.httpx.AsyncClient", side_effect=_client_factory):
        result = await call_assemblyai(
            {"audio_url": "https://example.com/audio.mp3"}, _FAKE_KEY
        )

    assert result["status"] == "completed"
    assert result["text"] == "hello world"


# ─────────────────────────────────────────────────────────────────────────────
# Stability AI
# ─────────────────────────────────────────────────────────────────────────────


async def test_stability_413_when_samples_exceeds_1():
    with pytest.raises(HTTPException) as exc_info:
        await call_stability({"prompt": "a sunset", "samples": 2}, _FAKE_KEY)
    assert exc_info.value.status_code == 413
    assert "1 image" in exc_info.value.detail


async def test_stability_413_when_n_exceeds_1():
    with pytest.raises(HTTPException) as exc_info:
        await call_stability({"prompt": "a sunset", "n": 3}, _FAKE_KEY)
    assert exc_info.value.status_code == 413
    assert "1 image" in exc_info.value.detail


async def test_stability_200_when_samples_is_1():
    artifact = {"base64": "abc123==", "seed": 42, "finishReason": "SUCCESS"}
    sdxl_resp = _make_mock_response(200, json_data={"artifacts": [artifact]})
    patcher, _ = _patch_async_client([sdxl_resp])
    with patcher:
        result = await call_stability({"prompt": "a sunset", "samples": 1}, _FAKE_KEY)
    assert result["image_base64"] == "abc123=="
    assert result["quality"] == "core"
