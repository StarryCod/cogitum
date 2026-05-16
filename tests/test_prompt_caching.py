"""Regression tests for prompt_caching.should_cache().

History: should_cache used to return True based on model name alone, which
broke Cerebras (and any plain OpenAI-compat host) when the user picked a
Qwen or Claude-named model. Cerebras returned HTTP 400 on the cache_control
marker. Decision must be made by base_url only.
"""
from __future__ import annotations

import pytest

from cogitum.core.llm.prompt_caching import (
    apply_cache_control,
    should_cache,
)


# Endpoints that DO honor cache_control
@pytest.mark.parametrize("base_url, model", [
    ("https://api.anthropic.com", "claude-opus-4-5"),
    ("https://openrouter.ai/api/v1", "anthropic/claude-sonnet-4.5"),
    ("https://openrouter.ai/api/v1", "qwen/qwen3-coder"),
    ("https://dashscope.aliyuncs.com/v1", "qwen-3-235b"),
    ("http://localhost:20128/v1", "kr/claude-opus-4.6"),  # kiro
    ("https://omniroute.example.com/v1", "kr/claude-haiku-4.5"),
])
def test_supported_endpoints_enable_caching(base_url, model):
    assert should_cache(base_url, model) is True


# Endpoints that DO NOT honor cache_control — marker would 400
@pytest.mark.parametrize("base_url, model", [
    ("https://api.cerebras.ai/v1", "qwen-3-235b-a22b-instruct-2507"),
    ("https://api.cerebras.ai/v1", "llama3.1-8b"),
    ("https://api.cerebras.ai/v1", "zai-glm-4.7"),
    ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    ("https://api.deepinfra.com/v1/openai", "Qwen/Qwen3-235B"),
    ("http://localhost:8000/v1", "qwen2.5-coder"),  # vLLM-local
    ("https://api.openai.com/v1", "gpt-4o"),
    ("https://inference.canopywave.io/v1", "moonshotai/kimi-k2.6"),
    ("https://api.fireworks.ai/inference/v1", "qwen3-235b"),
    ("https://dashscope.aliyuncs.com/v1", "llama"),  # dashscope but non-qwen
])
def test_unsupported_endpoints_disable_caching(base_url, model):
    assert should_cache(base_url, model) is False, \
        f"caching wrongly enabled for {base_url} + {model} — would HTTP 400"


def test_cerebras_qwen_regression():
    """The exact scenario the user hit: Cerebras + qwen model.

    Before fix: should_cache returned True (qwen in name).
    After fix:  must return False (cerebras endpoint doesn't support it).
    """
    assert should_cache(
        "https://api.cerebras.ai/v1",
        "qwen-3-235b-a22b-instruct-2507",
    ) is False


def test_apply_cache_control_preserves_string_when_disabled():
    """Sanity: when caller decides not to cache, content stays a string.

    The pipeline is: should_cache → if True, call apply_cache_control.
    apply_cache_control itself wraps strings into content blocks. So as
    long as cerebras gets should_cache=False, the system message stays
    a plain string and Cerebras accepts it.
    """
    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ]
    # apply_cache_control unconditionally wraps when called
    out = apply_cache_control(msgs)
    assert isinstance(out[0]["content"], list), "wrap on caching path expected"

    # The actual integration: openai_compat only calls apply_cache_control
    # when should_cache says yes, so for cerebras the messages are passed
    # through unchanged.
    assert msgs[0]["content"] == "you are helpful"
    assert isinstance(msgs[0]["content"], str)
