"""Audit C2-а: MCP пустой content list не должен молча выглядеть как успех.

Раньше ``_call_tool_result_to_text`` для CallToolResult с
``content=[]`` и ``isError=False`` возвращал пустую строку. Модель
видела ``ToolResultPart(content="")`` и думала, что вызов прошёл
успешно, просто без вывода. Кроме того пустая строка ломала
Anthropic-провайдер, который реджектит пустой ``tool_result.content``
с HTTP 400.

Теперь:
  * empty content + isError=False → ``"(no output)"``
  * empty content + isError=True  → ``"ERROR: tool returned an error
    with no message"``

Не-пустой content на любой ветке работает как раньше.
"""
from __future__ import annotations

from types import SimpleNamespace

from cogitum.core.mcp.client import _call_tool_result_to_text


def _result(content_items, is_error=False):
    return SimpleNamespace(content=list(content_items), isError=is_error)


def test_empty_content_success_returns_no_output_placeholder():
    out = _call_tool_result_to_text(_result([], is_error=False))
    assert out == "(no output)"
    # Главное: НЕ пустая строка, чтобы Anthropic не получил пустой
    # tool_result block и провайдер-валидация модели тоже не сбилась.
    assert out != ""


def test_empty_content_with_error_returns_explicit_error_placeholder():
    out = _call_tool_result_to_text(_result([], is_error=True))
    assert out.startswith("ERROR:")
    assert "no message" in out


def test_none_content_attribute_treated_as_empty_success():
    # Server SDK иногда возвращает result без атрибута content вовсе.
    result = SimpleNamespace(content=None, isError=False)
    out = _call_tool_result_to_text(result)
    assert out == "(no output)"


def test_none_content_with_error_returns_error_placeholder():
    result = SimpleNamespace(content=None, isError=True)
    out = _call_tool_result_to_text(result)
    assert out.startswith("ERROR:")


def test_non_empty_text_content_success_unchanged():
    item = SimpleNamespace(type="text", text="hello world")
    out = _call_tool_result_to_text(_result([item]))
    assert out == "hello world"


def test_non_empty_text_content_with_error_prefixed_error():
    item = SimpleNamespace(type="text", text="boom")
    out = _call_tool_result_to_text(_result([item], is_error=True))
    assert out == "ERROR: boom"


def test_result_is_none_returns_empty_string():
    # Консервативно: явный None всё ещё допустимо вернуть "" —
    # это не "пустой success", а "вообще нет результата" (например
    # вызов до того как сервер ответил). Поведение сохранено.
    assert _call_tool_result_to_text(None) == ""
