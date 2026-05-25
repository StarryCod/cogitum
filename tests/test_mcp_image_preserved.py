"""Audit M2: MCP image / audio / resource content must preserve metadata.

Earlier ``_call_tool_result_to_text`` collapsed all non-text content
items to the literal string ``"[<type> content]"``, dropping:
  * the base64 payload,
  * the mime type,
  * resource URIs.

The model received a content-free placeholder it couldn't act on.
Even when we don't pass the raw bytes through (the tool-result wire
shape is text), surfacing size + mime + URI lets the model reason
about it ("ok, the screenshot tool returned a 47 KB image/png — I
should call inspect_image next", or "the resource at file:///foo is
empty — try a different path").

Test: build mock CallToolResult-shaped objects and verify the new
formatter keeps the metadata.
"""
from __future__ import annotations

from types import SimpleNamespace

from cogitum.core.mcp.client import _call_tool_result_to_text


def _result(content_items, is_error=False):
    return SimpleNamespace(content=list(content_items), isError=is_error)


def test_image_content_preserves_size_and_mime():
    item = SimpleNamespace(
        type="image",
        data="A" * 1024,  # 1 KB of fake base64
        mimeType="image/png",
    )
    out = _call_tool_result_to_text(_result([item]))
    assert "image content" in out
    assert "1024 bytes" in out
    assert "image/png" in out


def test_audio_content_preserves_size_and_mime():
    item = SimpleNamespace(
        type="audio",
        data="B" * 2048,
        mimeType="audio/wav",
    )
    out = _call_tool_result_to_text(_result([item]))
    assert "audio content" in out
    assert "2048 bytes" in out
    assert "audio/wav" in out


def test_image_without_mime_falls_back_to_octet_stream():
    item = SimpleNamespace(type="image", data="x" * 4)
    out = _call_tool_result_to_text(_result([item]))
    assert "application/octet-stream" in out
    assert "4 bytes" in out


def test_text_content_still_passes_through():
    """Don't regress the happy path."""
    item = SimpleNamespace(type="text", text="hello world")
    out = _call_tool_result_to_text(_result([item]))
    assert out == "hello world"


def test_mixed_text_and_image_in_single_result():
    text_item = SimpleNamespace(type="text", text="see image:")
    image_item = SimpleNamespace(
        type="image", data="Z" * 100, mimeType="image/jpeg",
    )
    out = _call_tool_result_to_text(_result([text_item, image_item]))
    assert "see image:" in out
    assert "100 bytes" in out
    assert "image/jpeg" in out


def test_resource_content_surfaces_uri():
    inner = SimpleNamespace(uri="file:///tmp/data.bin", mimeType="application/x-bin")
    item = SimpleNamespace(type="resource", resource=inner)
    out = _call_tool_result_to_text(_result([item]))
    assert "file:///tmp/data.bin" in out
    assert "application/x-bin" in out


def test_image_in_error_still_surfaces_metadata():
    item = SimpleNamespace(
        type="image", data="C" * 64, mimeType="image/png",
    )
    out = _call_tool_result_to_text(_result([item], is_error=True))
    assert out.startswith("ERROR:")
    assert "64 bytes" in out
    assert "image/png" in out
