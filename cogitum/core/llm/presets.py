"""
Catalog of well-known providers and models.

Used by the Textual setup wizard to offer presets when adding a new
[providers.<id>] block. Each preset captures the data we'd otherwise ask
the user for (base url, format, env var name, default models with caps).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True, frozen=True)
class ModelPreset:
    id: str
    display: str
    aliases: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ("text", "tools")
    context_window: int = 8192
    max_output_tokens: int = 4096


@dataclass(slots=True, frozen=True)
class ProviderPreset:
    id: str
    name: str
    format: str = "openai_compat"     # or anthropic_native
    base_url: str = ""
    auth: str = "bearer"              # bearer | x_api_key | header_custom
    env_var: str = ""                 # default env name for the API key
    extra_headers: dict[str, str] = field(default_factory=dict)
    models: tuple[ModelPreset, ...] = ()
    notes: str = ""


PROVIDER_PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        env_var="OPENAI_API_KEY",
        models=(
            ModelPreset("gpt-5", "GPT-5", ("gpt5",),
                        ("text", "vision", "reasoning", "tools"),
                        256_000, 64_000),
            ModelPreset("gpt-5-mini", "GPT-5 mini", ("gpt5-mini", "mini"),
                        ("text", "vision", "tools"),
                        256_000, 32_000),
            ModelPreset("gpt-4o", "GPT-4o", ("4o",),
                        ("text", "vision", "tools"),
                        128_000, 16_000),
        ),
    ),
    ProviderPreset(
        id="anthropic",
        name="Anthropic",
        format="anthropic_native",
        base_url="https://api.anthropic.com",
        auth="x_api_key",
        env_var="ANTHROPIC_API_KEY",
        models=(
            ModelPreset("claude-opus-4-5", "Claude Opus 4.5", ("opus",),
                        ("text", "vision", "reasoning", "tools", "caching"),
                        200_000, 32_000),
            ModelPreset("claude-sonnet-4-5", "Claude Sonnet 4.5", ("sonnet",),
                        ("text", "vision", "reasoning", "tools", "caching"),
                        200_000, 16_000),
            ModelPreset("claude-haiku-4-5", "Claude Haiku 4.5", ("haiku",),
                        ("text", "vision", "tools", "caching"),
                        200_000, 8_000),
        ),
    ),
    ProviderPreset(
        id="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        env_var="OPENROUTER_API_KEY",
        extra_headers={
            "HTTP-Referer": "https://github.com/Starred/Cogitum",
            "X-Title": "Cogitum",
        },
        models=(
            ModelPreset("anthropic/claude-opus-4-5", "Claude Opus 4.5 (OR)", ("or-opus",),
                        ("text", "vision", "reasoning", "tools"),
                        200_000, 32_000),
            ModelPreset("x-ai/grok-4", "Grok 4", ("grok",),
                        ("text", "tools"), 128_000, 16_000),
            ModelPreset("deepseek/deepseek-v3.2", "DeepSeek V3.2", ("dsv3",),
                        ("text", "tools", "reasoning"), 128_000, 16_000),
            ModelPreset("google/gemini-3-pro", "Gemini 3 Pro",
                        ("gemini", "g3p"),
                        ("text", "vision", "reasoning", "tools"),
                        2_000_000, 64_000),
        ),
    ),
    ProviderPreset(
        id="groq",
        name="Groq",
        base_url="https://api.groq.com/openai/v1",
        env_var="GROQ_API_KEY",
        models=(
            ModelPreset("llama-3.3-70b-versatile", "Llama 3.3 70B",
                        ("llama-70b",), ("text", "tools"),
                        131_000, 8_000),
            ModelPreset("openai/gpt-oss-120b", "GPT-OSS 120B",
                        ("oss-120b",),
                        ("text", "tools", "reasoning"),
                        131_000, 32_000),
        ),
        notes="Fastest hosted inference. Free tier with daily token caps.",
    ),
    ProviderPreset(
        id="together",
        name="Together AI",
        base_url="https://api.together.xyz/v1",
        env_var="TOGETHER_API_KEY",
        models=(
            ModelPreset("Qwen/Qwen3-235B-A22B-Instruct-2507-tput",
                        "Qwen3 235B Instruct", ("qwen3-235",),
                        ("text", "tools"), 262_000, 32_000),
            ModelPreset("deepseek-ai/DeepSeek-V3.1", "DeepSeek V3.1",
                        ("ds31",), ("text", "reasoning", "tools"),
                        128_000, 16_000),
        ),
    ),
    ProviderPreset(
        id="fireworks",
        name="Fireworks AI",
        base_url="https://api.fireworks.ai/inference/v1",
        env_var="FIREWORKS_API_KEY",
        models=(
            ModelPreset("accounts/fireworks/models/qwen3-235b-instruct",
                        "Qwen3 235B (Fireworks)", ("fw-qwen3",),
                        ("text", "tools"), 128_000, 32_000),
        ),
    ),
    ProviderPreset(
        id="cerebras",
        name="Cerebras",
        base_url="https://api.cerebras.ai/v1",
        env_var="CEREBRAS_API_KEY",
        models=(
            ModelPreset("llama-3.3-70b", "Llama 3.3 70B (Cerebras)",
                        ("cb-llama-70b",), ("text",), 131_000, 8_000),
        ),
        notes="Wafer-scale inference, ridiculously fast.",
    ),
    ProviderPreset(
        id="deepinfra",
        name="DeepInfra",
        base_url="https://api.deepinfra.com/v1/openai",
        env_var="DEEPINFRA_API_KEY",
        models=(),
    ),
    ProviderPreset(
        id="mistral",
        name="Mistral La Plateforme",
        base_url="https://api.mistral.ai/v1",
        env_var="MISTRAL_API_KEY",
        models=(
            ModelPreset("mistral-large-latest", "Mistral Large",
                        ("ml",), ("text", "tools"), 128_000, 8_000),
        ),
    ),
    ProviderPreset(
        id="xai",
        name="xAI (Grok)",
        base_url="https://api.x.ai/v1",
        env_var="XAI_API_KEY",
        models=(
            ModelPreset("grok-4", "Grok 4 (xAI)",
                        ("xai-grok",), ("text", "tools"),
                        256_000, 16_000),
        ),
    ),
    ProviderPreset(
        id="vllm-local",
        name="vLLM (local)",
        base_url="http://localhost:8000/v1",
        env_var="",
        notes="Empty key works. Edit base_url if your vLLM runs elsewhere.",
        models=(),
    ),
    ProviderPreset(
        id="ollama",
        name="Ollama (local)",
        base_url="http://localhost:11434/v1",
        env_var="",
        notes="OpenAI-compat layer. Empty key.",
        models=(),
    ),
)


def by_id(pid: str) -> ProviderPreset | None:
    for p in PROVIDER_PRESETS:
        if p.id == pid:
            return p
    return None


__all__ = ["ProviderPreset", "ModelPreset", "PROVIDER_PRESETS", "by_id"]
