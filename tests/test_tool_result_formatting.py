"""Audit F6 + F7: ``_format_tool_result_for_model`` нормализует
любой возврат тулы в строку, которую модель сможет понять.

До фикса:
  * ``None`` → ``"None"`` (четыре буквы, модель читает как «ничего не
    вернулось» и зачастую перевызывает тулу).
  * ``dict`` / ``list`` → ``str(d)`` использует Python-repr, кавычки
    одинарные — наивный парсер модели спотыкается.
  * Пустая строка ``""`` → блок ``tool_result`` с пустым content. На
    Anthropic это раньше было 400, на OpenAI — модель видит «команда
    упала молча» и перевызывает.
  * ``bytes`` / ``bytearray`` → ``b'...'`` literal, не декодируется.
  * ``Exception`` → может прорваться без traceback (registry иногда
    возвращает объект исключения вместо его рейза).

Хелпер живёт в ``cogitum.core.agent`` и применяется в
``_execute_tool`` сразу после ``await registry.execute(...)``.
"""
from __future__ import annotations

import json

from cogitum.core.agent import _format_tool_result_for_model


def test_none_becomes_no_output_marker():
    assert _format_tool_result_for_model(None) == "(no output)"


def test_empty_string_becomes_no_output_marker():
    assert _format_tool_result_for_model("") == "(no output)"


def test_whitespace_only_string_becomes_no_output_marker():
    assert _format_tool_result_for_model("   \n\t ") == "(no output)"


def test_non_empty_string_passes_through():
    assert _format_tool_result_for_model("hello") == "hello"


def test_string_with_real_content_and_trailing_newline_preserved():
    # Не trim'аем настоящие данные — только полностью пустые/whitespace.
    assert _format_tool_result_for_model("data\n") == "data\n"


def test_dict_serialized_as_json_with_double_quotes():
    out = _format_tool_result_for_model({"key": "value", "n": 1})
    parsed = json.loads(out)
    assert parsed == {"key": "value", "n": 1}
    # Главное условие F6: должны быть двойные кавычки JSON, не Python-repr.
    assert "'key'" not in out
    assert '"key"' in out


def test_dict_with_unicode_preserved():
    out = _format_tool_result_for_model({"имя": "файл"})
    assert "имя" in out
    assert "файл" in out
    assert json.loads(out) == {"имя": "файл"}


def test_list_serialized_as_json():
    out = _format_tool_result_for_model([1, 2, "three"])
    assert json.loads(out) == [1, 2, "three"]


def test_nested_structure_serialized():
    payload = {"items": [{"id": 1}, {"id": 2}], "count": 2}
    out = _format_tool_result_for_model(payload)
    assert json.loads(out) == payload


def test_dict_with_non_serializable_falls_back_to_str_default():
    # default=str позволит сериализовать, например, объекты с __str__.
    class _Custom:
        def __str__(self):
            return "<custom-obj>"

    out = _format_tool_result_for_model({"obj": _Custom()})
    parsed = json.loads(out)
    assert parsed == {"obj": "<custom-obj>"}


def test_bytes_utf8_decoded():
    assert _format_tool_result_for_model("привет".encode("utf-8")) == "привет"


def test_bytes_invalid_utf8_base64_encoded():
    raw = b"\xff\xfe\xfd\x00binary"
    out = _format_tool_result_for_model(raw)
    # base64-выход по определению ASCII и без \x escape'ов.
    assert all(ord(c) < 128 for c in out)
    import base64
    assert base64.b64decode(out) == raw


def test_bytearray_handled():
    assert _format_tool_result_for_model(bytearray(b"hi")) == "hi"


def test_empty_bytes_normalised():
    # Пустые bytes тоже являются "no output".
    assert _format_tool_result_for_model(b"") == "(no output)"


def test_exception_formatted_with_type_and_message():
    exc = ValueError("bad input")
    out = _format_tool_result_for_model(exc)
    assert out.startswith("ERROR: ValueError: bad input")


def test_exception_with_traceback_includes_traceback():
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        out = _format_tool_result_for_model(e)
    assert out.startswith("ERROR: RuntimeError: boom")
    # traceback присутствует
    assert "Traceback" in out


def test_int_and_float_str_fallback():
    assert _format_tool_result_for_model(42) == "42"
    assert _format_tool_result_for_model(3.14) == "3.14"


def test_bool_str_fallback():
    assert _format_tool_result_for_model(True) == "True"


def test_tuple_serialised_as_json_array():
    out = _format_tool_result_for_model(("a", "b"))
    assert json.loads(out) == ["a", "b"]
