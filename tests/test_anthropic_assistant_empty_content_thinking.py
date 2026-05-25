"""R3 fix (audit GAP-5a): assistant message с одними только ThinkingPart
без подписи раньше давал пустой ``content[]`` после нормализации, что
Anthropic реджектит с HTTP 400.

Цепочка: на ряде провайдеров (vLLM-style без подписи, обрыв стрима до
signature_delta, не-нативные Anthropic пути) ассистент возвращает
reasoning, но без подписи. Anthropic API дропает unsigned thinking блоки,
и `normalize_messages_anthropic` оставлял пустой массив content[].

Фикс — финальный fallback в `normalize_messages_anthropic`: если после
обработки всех частей у assistant-сообщения content[] пуст, вставляем
``[{"type": "text", "text": "(empty)"}]``. Совместимо с
hermes-agent/agent/anthropic_adapter.py (строки 1560-1563).

Хорошо сформированные сообщения (с TextPart, signed ThinkingPart,
ToolCallPart) не должны изменяться — проверяем регрессию.
"""
from __future__ import annotations

from cogitum.core.events import (
    Message,
    TextPart,
    ThinkingPart,
    ToolCallPart,
)
from cogitum.core.llm.events_helpers import normalize_messages_anthropic


def test_assistant_only_unsigned_thinking_gets_empty_sentinel():
    """ThinkingPart без signature → дропается → fallback на (empty)."""
    msg = Message(
        role="assistant",
        parts=[ThinkingPart(text="some private reasoning", signature=None)],
    )
    _, out = normalize_messages_anthropic([msg])
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    blocks = out[0]["content"]
    # Главное: content[] НЕ пустой (Anthropic 400 на пустом списке).
    assert blocks, "assistant content[] must not be empty"
    assert len(blocks) == 1
    assert blocks[0] == {"type": "text", "text": "(empty)"}


def test_assistant_only_empty_string_signature_thinking_gets_sentinel():
    """ThinkingPart.signature='' тоже считается отсутствующим."""
    msg = Message(
        role="assistant",
        parts=[ThinkingPart(text="reasoning", signature="")],
    )
    _, out = normalize_messages_anthropic([msg])
    blocks = out[0]["content"]
    assert blocks == [{"type": "text", "text": "(empty)"}]


def test_assistant_signed_thinking_preserved_no_sentinel():
    """С signature thinking-блок сохраняется, sentinel НЕ добавляется."""
    msg = Message(
        role="assistant",
        parts=[ThinkingPart(text="reasoning", signature="sig_abc123")],
    )
    _, out = normalize_messages_anthropic([msg])
    blocks = out[0]["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["signature"] == "sig_abc123"
    # Никакого «(empty)» сюда подмешать нельзя.
    assert all(b.get("text") != "(empty)" for b in blocks)


def test_assistant_text_part_no_sentinel():
    """С обычным TextPart fallback не должен срабатывать."""
    msg = Message(role="assistant", parts=[TextPart(text="hello")])
    _, out = normalize_messages_anthropic([msg])
    blocks = out[0]["content"]
    assert len(blocks) == 1
    assert blocks[0] == {"type": "text", "text": "hello"}


def test_assistant_unsigned_thinking_plus_text_keeps_text():
    """ThinkingPart без подписи дропается, TextPart остаётся — sentinel НЕ нужен."""
    msg = Message(
        role="assistant",
        parts=[
            ThinkingPart(text="hidden reasoning", signature=None),
            TextPart(text="visible answer"),
        ],
    )
    _, out = normalize_messages_anthropic([msg])
    blocks = out[0]["content"]
    # Только TextPart должен пройти, sentinel «(empty)» не подмешивается.
    text_blocks = [b for b in blocks if b["type"] == "text"]
    assert len(text_blocks) == 1
    assert text_blocks[0]["text"] == "visible answer"


def test_assistant_unsigned_thinking_plus_tool_call_no_sentinel():
    """ThinkingPart без подписи дропается, tool_use остаётся — sentinel НЕ нужен."""
    msg = Message(
        role="assistant",
        parts=[
            ThinkingPart(text="planning", signature=None),
            ToolCallPart(id="t1", name="search", arguments={"q": "x"}),
        ],
    )
    _, out = normalize_messages_anthropic([msg])
    blocks = out[0]["content"]
    types = [b["type"] for b in blocks]
    # tool_use на месте, никаких лишних text-блоков «(empty)».
    assert "tool_use" in types
    assert all(b.get("text") != "(empty)" for b in blocks)


def test_user_role_no_sentinel_on_empty_blocks():
    """Sentinel должен срабатывать ТОЛЬКО для assistant — user-сообщение
    с пустым content[] просто отсутствует в выдаче (так как у нас
    финальный guard `if blocks` пропускает его)."""
    # User с TextPart='hi' — нормальный путь, отрабатывает.
    msg_ok = Message(role="user", parts=[TextPart(text="hi")])
    _, out_ok = normalize_messages_anthropic([msg_ok])
    assert out_ok and out_ok[0]["role"] == "user"
    # User с одной только ThinkingPart без signature: после фильтрации
    # blocks пуст, role != "assistant", поэтому sentinel НЕ ставится,
    # сообщение просто не попадает в out (`if blocks` ниже).
    msg_thinking = Message(
        role="user",
        parts=[ThinkingPart(text="r", signature=None)],
    )
    _, out_thinking = normalize_messages_anthropic([msg_thinking])
    assert out_thinking == []


def test_assistant_no_parts_at_all_gets_sentinel():
    """Защита от вырожденного случая: assistant без частей вовсе.

    В обычном flow agent.py не закоммитит такой Message (`if all_parts`
    guard на 1384), но десериализация из sessions.py / fallback summary
    может теоретически прислать пустую `parts` — wire должен остаться
    валидным.
    """
    msg = Message(role="assistant", parts=[])
    _, out = normalize_messages_anthropic([msg])
    assert len(out) == 1
    assert out[0]["content"] == [{"type": "text", "text": "(empty)"}]
