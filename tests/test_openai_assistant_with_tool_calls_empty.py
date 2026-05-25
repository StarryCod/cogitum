"""Audit F12: assistant message с tool_calls И пустым ``content``
должен идти на OpenAI-compat провайдеры с ``content: null``, не ``""``.

Спецификация OpenAI Chat Completions явно допускает обе формы, но
на практике несколько распространённых compat-серверов (старый
llama.cpp HTTP, ряд гейтвеев) реджектят пустую строку с HTTP 400,
а ``null`` принимают всегда. Эта проверка фиксирует поведение
``normalize_messages_openai``.
"""
from __future__ import annotations

from cogitum.core.events import (
    Message,
    TextPart,
    ToolCallPart,
)
from cogitum.core.llm.events_helpers import normalize_messages_openai


def _assistant_with_tool_calls(*, text: str | None = None):
    parts: list = []
    if text is not None:
        parts.append(TextPart(text=text))
    parts.append(
        ToolCallPart(id="call_1", name="ls", arguments={"path": "/"})
    )
    return Message(role="assistant", parts=parts)


def test_assistant_tool_call_only_emits_null_content():
    # F12 главное условие: только tool_call, никакого text — content
    # должен быть None, не "".
    out = normalize_messages_openai([_assistant_with_tool_calls()])
    assert len(out) == 1
    msg = out[0]
    assert msg["role"] == "assistant"
    assert msg["content"] is None
    assert "tool_calls" in msg
    assert len(msg["tool_calls"]) == 1


def test_assistant_tool_call_with_text_keeps_text_content():
    out = normalize_messages_openai(
        [_assistant_with_tool_calls(text="thinking out loud")]
    )
    msg = out[0]
    assert msg["content"] == "thinking out loud"
    assert "tool_calls" in msg


def test_assistant_tool_call_with_empty_text_still_null():
    # Пустой TextPart на ассистенте С tool_calls — не повод оставить
    # "". Тот же риск 400 у compat-провайдера.
    out = normalize_messages_openai(
        [_assistant_with_tool_calls(text="")]
    )
    msg = out[0]
    assert msg["content"] is None


def test_plain_assistant_text_only_keeps_string_content():
    # Без tool_calls контракт прежний: пустая строка ОК — модель
    # просто промолчала. Не подменяем на null, чтобы не сломать
    # legacy callers, которые проверяют isinstance(content, str).
    msg = Message(role="assistant", parts=[TextPart(text="")])
    out = normalize_messages_openai([msg])
    assert out[0]["content"] == ""
    assert "tool_calls" not in out[0]


def test_user_message_with_empty_text_unchanged():
    msg = Message(role="user", parts=[TextPart(text="")])
    out = normalize_messages_openai([msg])
    assert out[0]["content"] == ""


def test_assistant_tool_call_id_and_arguments_serialized_correctly():
    msg = Message(
        role="assistant",
        parts=[
            ToolCallPart(id="call_42", name="grep", arguments={"q": "тест"}),
        ],
    )
    out = normalize_messages_openai([msg])
    tc = out[0]["tool_calls"][0]
    assert tc["id"] == "call_42"
    assert tc["function"]["name"] == "grep"
    # ensure_ascii=False должно сохранить кириллицу.
    assert "тест" in tc["function"]["arguments"]


def test_two_tool_calls_emitted_with_null_content():
    msg = Message(
        role="assistant",
        parts=[
            ToolCallPart(id="c1", name="ls", arguments={}),
            ToolCallPart(id="c2", name="pwd", arguments={}),
        ],
    )
    out = normalize_messages_openai([msg])
    assert out[0]["content"] is None
    assert len(out[0]["tool_calls"]) == 2
