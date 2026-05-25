"""Audit C9: пустые tool_result/text блоки никогда не должны попадать
в Anthropic Messages API.

Anthropic реджектит сообщения с пустым ``tool_result.content`` или
пустым ``text`` блоком на ассистенте с HTTP 400. Раньше Cogitum
конвертировал ``ToolResultPart(content="")`` напрямую в wire-формат
без подмены. Если MCP-сервер вернул пустоту, либо tool тихо отдал
``""``/``None``, провайдер падал на /v1/messages с непонятной ошибкой
для модели.

Теперь ``normalize_messages_anthropic`` подменяет:
  * пустой/whitespace ``ToolResultPart.content`` → ``"(no output)"``
  * пустой/whitespace ``TextPart`` на ассистенте → ``" "`` (один
    пробел; нельзя дропать, иначе сломается порядок tool_use блоков)
"""
from __future__ import annotations

from cogitum.core.events import (
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from cogitum.core.llm.events_helpers import normalize_messages_anthropic


def _tool_message(tool_call_id: str, content: str, is_error: bool = False):
    return Message(
        role="tool",
        parts=[
            ToolResultPart(
                tool_call_id=tool_call_id,
                content=content,
                is_error=is_error,
            )
        ],
    )


def test_empty_tool_result_content_replaced_with_placeholder():
    msgs = [_tool_message("call_1", "")]
    _, out = normalize_messages_anthropic(msgs)
    assert len(out) == 1
    blocks = out[0]["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "tool_result"
    assert blocks[0]["tool_use_id"] == "call_1"
    assert blocks[0]["content"] == "(no output)"


def test_whitespace_only_tool_result_content_replaced():
    msgs = [_tool_message("call_2", "   \n\t  ")]
    _, out = normalize_messages_anthropic(msgs)
    assert out[0]["content"][0]["content"] == "(no output)"


def test_none_tool_result_content_replaced():
    # ``ToolResultPart.content`` объявлен как ``str``, но защищаемся
    # от плагинов, которые случайно подсунут None.
    msg = Message(
        role="tool",
        parts=[ToolResultPart(tool_call_id="call_3", content=None)],  # type: ignore[arg-type]
    )
    _, out = normalize_messages_anthropic([msg])
    assert out[0]["content"][0]["content"] == "(no output)"


def test_non_empty_tool_result_content_unchanged():
    msgs = [_tool_message("call_4", "real output")]
    _, out = normalize_messages_anthropic(msgs)
    assert out[0]["content"][0]["content"] == "real output"


def test_error_flag_preserved_with_placeholder_substitution():
    msgs = [_tool_message("call_5", "", is_error=True)]
    _, out = normalize_messages_anthropic(msgs)
    block = out[0]["content"][0]
    assert block["content"] == "(no output)"
    assert block["is_error"] is True


def test_empty_assistant_text_replaced_with_space():
    # Пустой text на ассистенте: Anthropic реджектит. Нельзя просто
    # выкинуть блок — порядок tool_use / text критичен. Подменяем на
    # один пробел.
    msg = Message(role="assistant", parts=[TextPart(text="")])
    _, out = normalize_messages_anthropic([msg])
    assert len(out) == 1
    text_blocks = [b for b in out[0]["content"] if b["type"] == "text"]
    assert len(text_blocks) == 1
    assert text_blocks[0]["text"].strip() == ""
    # Главное условие: НЕ пустая строка (Anthropic 400)
    assert text_blocks[0]["text"] != ""


def test_empty_assistant_text_with_tool_use_keeps_block_count():
    # Защита от регрессии: если блок text пустой и рядом tool_use,
    # ничего нельзя дропать.
    msg = Message(
        role="assistant",
        parts=[
            TextPart(text=""),
            ToolCallPart(id="t1", name="search", arguments={"q": "hi"}),
        ],
    )
    _, out = normalize_messages_anthropic([msg])
    assert len(out) == 1
    blocks = out[0]["content"]
    types = [b["type"] for b in blocks]
    assert types == ["text", "tool_use"]
    text_block = blocks[0]
    assert text_block["text"] != ""


def test_user_text_empty_unchanged_passthrough():
    # На user-роли пустой text иногда нужен (плейсхолдер), и
    # Anthropic это допускает. Не вмешиваемся.
    msg = Message(role="user", parts=[TextPart(text="")])
    _, out = normalize_messages_anthropic([msg])
    assert out[0]["content"][0]["text"] == ""


def test_non_empty_assistant_text_unchanged():
    msg = Message(role="assistant", parts=[TextPart(text="hello")])
    _, out = normalize_messages_anthropic([msg])
    assert out[0]["content"][0]["text"] == "hello"
