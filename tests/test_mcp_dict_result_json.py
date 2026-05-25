"""Regression test for P1-2 (audit_tools_history.md) / F6/F7 PIPELINE.

MCP tools may return structured data (dict / list) rather than text.
The dispatcher used to render those with plain ``str(result)`` which
emits a Python repr — single-quoted, ``True``/``False``/``None``
keywords, not valid JSON. The model then tried to parse it as JSON
and either bailed or hallucinated. The fix routes every tool return
value through ``_format_tool_result_for_model`` which:

* JSON-encodes dicts / lists / tuples with ``ensure_ascii=False``
* Decodes bytes safely
* Wraps exceptions into ``ERROR: ...`` strings
* Substitutes ``"(no output)"`` for None / blank strings

This test pins the contract for each of those branches.
"""

from __future__ import annotations

import json

from cogitum.core.agent import _format_tool_result_for_model


def test_dict_result_renders_as_indented_json() -> None:
    """A dict from an MCP tool must round-trip through json.dumps,
    not Python repr (single quotes break model-side JSON parsers)."""
    result = {"status": "ok", "files": ["a.txt", "b.txt"], "count": 2}
    rendered = _format_tool_result_for_model(result)

    # The renderer must produce valid JSON.
    reparsed = json.loads(rendered)
    assert reparsed == result
    # Single quotes are the smoking-gun for ``str(dict)``.
    assert "'" not in rendered, (
        "dict was rendered with str() instead of json.dumps; "
        f"got: {rendered!r}"
    )


def test_list_result_renders_as_json_array() -> None:
    """Top-level lists must serialise as a JSON array, not a Python
    list repr."""
    result = [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]
    rendered = _format_tool_result_for_model(result)
    assert json.loads(rendered) == result


def test_unicode_preserved_no_escape_sequences() -> None:
    """``ensure_ascii=False`` is required so cyrillic / emoji / CJK
    survive without ``\\uXXXX`` escapes that confuse small models."""
    result = {"city": "Москва", "emoji": "🚀", "kanji": "東京"}
    rendered = _format_tool_result_for_model(result)
    assert "Москва" in rendered
    assert "🚀" in rendered
    assert "東京" in rendered
    # No \u-escapes for those characters.
    assert "\\u041c" not in rendered  # М
    assert "\\ud83d" not in rendered  # 🚀 high surrogate


def test_nested_structure_serialises_as_json() -> None:
    """Nested dicts / lists must remain valid JSON, not Python
    repr that the model would reject."""
    result = {
        "outer": {
            "inner": [1, 2, {"deep": "value"}],
            "flag": True,
            "missing": None,
        }
    }
    rendered = _format_tool_result_for_model(result)
    reparsed = json.loads(rendered)
    assert reparsed == result
    # Python repr would emit True/None — JSON must use true/null.
    assert "True" not in rendered
    assert "None" not in rendered


def test_non_serialisable_object_in_dict_falls_back_to_str() -> None:
    """If the dict carries a non-JSON-serialisable value, ``default=str``
    keeps the dump from crashing rather than raising into the agent
    loop."""

    class _CustomObj:
        def __repr__(self) -> str:
            return "<CustomObj>"

    result = {"obj": _CustomObj(), "n": 1}
    rendered = _format_tool_result_for_model(result)
    # Should be a single coherent string (whether full JSON or fallback
    # repr) — never raise.
    assert isinstance(rendered, str)
    assert rendered.strip()


def test_string_result_passes_through_unchanged() -> None:
    """Plain-string tool output must NOT be re-encoded — the model
    sees terminal stdout / file content verbatim."""
    out = "hello world\nline two"
    assert _format_tool_result_for_model(out) == out


def test_empty_string_becomes_no_output_marker() -> None:
    """Empty / whitespace-only strings collapse to a sentinel so the
    model has an unambiguous "tool succeeded with no output" signal."""
    assert _format_tool_result_for_model("") == "(no output)"
    assert _format_tool_result_for_model("   \n\t") == "(no output)"


def test_none_result_becomes_no_output_marker() -> None:
    assert _format_tool_result_for_model(None) == "(no output)"


def test_empty_dict_still_renders_as_json() -> None:
    """An empty dict is a valid structured response — render it as
    JSON ``{}`` rather than collapsing to ``(no output)``."""
    rendered = _format_tool_result_for_model({})
    assert json.loads(rendered) == {}
