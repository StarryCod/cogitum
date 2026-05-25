"""
cogitum.core.tools
~~~~~~~~~~~~~~~~~~
Tool registry: @tool decorator, JSON-schema generation from type hints,
and a global registry that the agent loop queries.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any, Callable, get_type_hints

# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

_PY_TO_JSON: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _py_type_to_json(tp: Any) -> dict[str, Any]:
    """Convert a Python type annotation to a JSON Schema fragment."""
    import typing

    origin = getattr(tp, "__origin__", None)

    # Optional[X] → {"oneOf": [X_schema, {"type": "null"}]}
    if origin is typing.Union:
        args = [a for a in tp.__args__ if a is not type(None)]
        nullable = type(None) in tp.__args__
        if len(args) == 1:
            schema = _py_type_to_json(args[0])
            if nullable:
                schema = {"anyOf": [schema, {"type": "null"}]}
            return schema
        return {"anyOf": [_py_type_to_json(a) for a in args]}

    # list[X]
    if origin is list:
        item_args = getattr(tp, "__args__", None)
        schema: dict[str, Any] = {"type": "array"}
        if item_args:
            schema["items"] = _py_type_to_json(item_args[0])
        return schema

    # dict[K, V]
    if origin is dict:
        return {"type": "object"}

    # Literal["a", "b"]
    if origin is typing.Literal:  # type: ignore[attr-defined]
        return {"enum": list(tp.__args__)}

    # Plain types
    json_type = _PY_TO_JSON.get(tp)
    if json_type:
        return {"type": json_type}

    # Fallback
    return {}


def _build_schema(fn: Callable) -> dict[str, Any]:
    """Build an OpenAI-style function schema from a Python callable."""
    sig = inspect.signature(fn)
    hints = get_type_hints(fn)

    properties: dict[str, Any] = {}
    required: list[str] = []

    doc = fn.__doc__ or ""

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue

        annotation = hints.get(name, Any)
        prop: dict[str, Any] = _py_type_to_json(annotation)

        # Pull description from param docstring convention: "param: description"
        # Also support enum hints:  "param: description (enum: a|b|c)"
        for line in doc.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{name}:"):
                desc = stripped[len(name) + 1:].strip()
                # extract trailing "(enum: a|b|c)" if present
                m = re.search(r"\(enum:\s*([^)]+)\)\s*$", desc)
                if m:
                    values = [v.strip() for v in m.group(1).split("|") if v.strip()]
                    if values:
                        prop["enum"] = values
                    desc = desc[: m.start()].strip()
                if desc:
                    prop["description"] = desc
                break

        properties[name] = prop

        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _build_description(fn: Callable) -> str:
    """Build a rich tool description from the full docstring.

    Strips per-parameter lines (``param: description (enum: …)``) since those
    are emitted as JSON-schema fields, but keeps the full multi-line summary
    so the LLM sees actions, examples, and pitfalls — not just the first line.
    """
    raw = fn.__doc__ or ""
    if not raw.strip():
        return fn.__name__

    sig = inspect.signature(fn)
    param_names = {
        n for n in sig.parameters if n not in ("self", "cls")
    }

    out: list[str] = []
    for line in textwrap.dedent(raw).splitlines():
        stripped = line.strip()
        # Skip lines that are pure parameter docs (consumed by _build_schema).
        if any(stripped.startswith(f"{p}:") for p in param_names):
            continue
        out.append(line.rstrip())

    # Trim leading/trailing blank lines, collapse runs of >1 blank.
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()

    cleaned: list[str] = []
    blank = False
    for line in out:
        if not line.strip():
            if blank:
                continue
            blank = True
        else:
            blank = False
        cleaned.append(line)

    return "\n".join(cleaned).strip() or fn.__name__


# ---------------------------------------------------------------------------
# ToolSpec
# ---------------------------------------------------------------------------

@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]          # JSON Schema object
    fn: Callable                         # sync or async
    tags: list[str] = field(default_factory=list)

    def to_openai(self) -> dict[str, Any]:
        """Return OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic(self) -> dict[str, Any]:
        """Return Anthropic tool format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    async def call(self, **kwargs: Any) -> Any:
        """Invoke the tool, handling both sync and async callables.

        Bug fix (audit C1): a sync function may *return* a coroutine or other
        awaitable (e.g. ``functools.partial`` over an async fn, decorators that
        wrap an async coro in a sync return, etc.). ``iscoroutinefunction``
        only detects ``async def`` declarations — it returns ``False`` for
        these wrappers. Without the post-call ``isawaitable`` check we'd
        ``str()`` the coroutine object into the tool result and the model
        would receive ``<coroutine object foo at 0x...>`` instead of the real
        output. That's the original "tool output stops reaching the model"
        symptom. Always check the *result* and await it if needed.
        """
        if asyncio.iscoroutinefunction(self.fn):
            result = await self.fn(**kwargs)
        else:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, lambda: self.fn(**kwargs))
        # Sync fns can still return awaitables — await them too.
        if inspect.isawaitable(result):
            result = await result
        return result


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Global registry of available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self, tags: list[str] | None = None) -> list[ToolSpec]:
        tools = list(self._tools.values())
        if tags:
            tools = [t for t in tools if any(tag in t.tags for tag in tags)]
        return tools

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def to_openai(self, tags: list[str] | None = None) -> list[dict]:
        return [t.to_openai() for t in self.all(tags)]

    def to_anthropic(self, tags: list[str] | None = None) -> list[dict]:
        return [t.to_anthropic() for t in self.all(tags)]

    def tool(
        self,
        name: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> Callable:
        """Decorator: @registry.tool() or @registry.tool(name='x', tags=['fs'])"""
        def decorator(fn: Callable) -> Callable:
            _name = name or fn.__name__
            _desc = description or _build_description(fn)
            spec = ToolSpec(
                name=_name,
                description=_desc,
                parameters=_build_schema(fn),
                fn=fn,
                tags=tags or [],
            )
            self.register(spec)
            return fn
        return decorator

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        """Execute a tool by name with parsed arguments dict."""
        spec = self.get(name)
        if spec is None:
            raise KeyError(f"Unknown tool: {name!r}")
        return await spec.call(**arguments)


# ---------------------------------------------------------------------------
# Global default registry + convenience decorator
# ---------------------------------------------------------------------------

REGISTRY = ToolRegistry()


def tool(
    name: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> Callable:
    """Module-level @tool decorator that registers into the global REGISTRY."""
    return REGISTRY.tool(name=name, description=description, tags=tags)
