"""
Catalog of well-known providers and models.

Used by the Textual setup wizard to offer presets when adding a new
[providers.<id>] block. Each preset captures the data we'd otherwise ask
the user for (base url, format, env var name, default models with caps).

Model lists are seeded with real, currently-served model ids per provider
so the user gets a working setup immediately. Auto-discovery via
/v1/models still runs after key save and merges any extras.
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
            ModelPreset("gpt-5-nano", "GPT-5 nano", ("gpt5-nano",),
                        ("text", "tools"),
                        256_000, 16_000),
            ModelPreset("gpt-4.1", "GPT-4.1", ("4.1",),
                        ("text", "vision", "tools"),
                        1_048_576, 32_768),
            ModelPreset("gpt-4.1-mini", "GPT-4.1 mini", ("4.1-mini",),
                        ("text", "vision", "tools"),
                        1_048_576, 32_768),
            ModelPreset("gpt-4o", "GPT-4o", ("4o",),
                        ("text", "vision", "tools"),
                        128_000, 16_000),
            ModelPreset("gpt-4o-mini", "GPT-4o mini", ("4o-mini",),
                        ("text", "vision", "tools"),
                        128_000, 16_000),
            ModelPreset("o3", "o3", ("o3",),
                        ("text", "reasoning", "tools"),
                        200_000, 100_000),
            ModelPreset("o3-mini", "o3 mini", ("o3-mini",),
                        ("text", "reasoning", "tools"),
                        200_000, 65_536),
            ModelPreset("o1", "o1", ("o1",),
                        ("text", "reasoning", "tools"),
                        200_000, 100_000),
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
            ModelPreset("claude-3-7-sonnet-latest", "Claude 3.7 Sonnet",
                        ("3.7",),
                        ("text", "vision", "reasoning", "tools", "caching"),
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
        notes="Hundreds of models. Auto-discovery will find them all.",
        models=(
            ModelPreset("anthropic/claude-opus-4-5", "Claude Opus 4.5 (OR)",
                        ("or-opus",),
                        ("text", "vision", "reasoning", "tools"),
                        200_000, 32_000),
            ModelPreset("anthropic/claude-sonnet-4-5", "Claude Sonnet 4.5 (OR)",
                        ("or-sonnet",),
                        ("text", "vision", "reasoning", "tools"),
                        200_000, 16_000),
            ModelPreset("openai/gpt-5", "GPT-5 (OR)", ("or-gpt5",),
                        ("text", "vision", "reasoning", "tools"),
                        256_000, 64_000),
            ModelPreset("x-ai/grok-4", "Grok 4 (OR)", ("or-grok",),
                        ("text", "vision", "tools"), 256_000, 16_000),
            ModelPreset("deepseek/deepseek-v3.2", "DeepSeek V3.2",
                        ("dsv3",),
                        ("text", "tools", "reasoning"), 128_000, 16_000),
            ModelPreset("google/gemini-2.5-pro", "Gemini 2.5 Pro",
                        ("gemini",),
                        ("text", "vision", "reasoning", "tools"),
                        2_000_000, 65_536),
            ModelPreset("qwen/qwen-3-235b-a22b-instruct-2507",
                        "Qwen 3 235B Instruct (OR)", ("or-qwen3",),
                        ("text", "tools"), 128_000, 16_000),
            ModelPreset("moonshotai/kimi-k2", "Kimi K2 (OR)", ("or-kimi",),
                        ("text", "tools"), 200_000, 16_000),
        ),
    ),
    ProviderPreset(
        id="groq",
        name="Groq",
        base_url="https://api.groq.com/openai/v1",
        env_var="GROQ_API_KEY",
        notes="Fastest hosted inference. Free tier with daily token caps.",
        models=(
            ModelPreset("llama-3.3-70b-versatile", "Llama 3.3 70B",
                        ("llama-70b",), ("text", "tools"),
                        131_000, 8_000),
            ModelPreset("llama-3.1-8b-instant", "Llama 3.1 8B Instant",
                        ("llama-8b",), ("text", "tools"),
                        131_000, 8_000),
            ModelPreset("openai/gpt-oss-120b", "GPT-OSS 120B",
                        ("oss-120b",),
                        ("text", "tools", "reasoning"),
                        131_000, 32_000),
            ModelPreset("openai/gpt-oss-20b", "GPT-OSS 20B",
                        ("oss-20b",),
                        ("text", "tools", "reasoning"),
                        131_000, 32_000),
            ModelPreset("moonshotai/kimi-k2-instruct", "Kimi K2 Instruct",
                        ("groq-kimi",),
                        ("text", "tools"), 131_000, 16_000),
            ModelPreset("qwen/qwen3-32b", "Qwen 3 32B",
                        ("groq-qwen3",), ("text", "tools"),
                        131_000, 16_000),
            ModelPreset("deepseek-r1-distill-llama-70b",
                        "DeepSeek R1 Distill Llama 70B",
                        ("r1-distill",),
                        ("text", "reasoning", "tools"),
                        131_000, 16_000),
        ),
    ),
    ProviderPreset(
        id="together",
        name="Together AI",
        base_url="https://api.together.xyz/v1",
        env_var="TOGETHER_API_KEY",
        models=(
            ModelPreset("Qwen/Qwen3-235B-A22B-Instruct-2507-tput",
                        "Qwen 3 235B Instruct", ("qwen3-235",),
                        ("text", "tools"), 262_000, 32_000),
            ModelPreset("Qwen/Qwen3-Coder-480B-A35B-Instruct",
                        "Qwen 3 Coder 480B", ("qwen3-coder",),
                        ("text", "tools"), 262_000, 32_000),
            ModelPreset("deepseek-ai/DeepSeek-V3.1", "DeepSeek V3.1",
                        ("ds31",),
                        ("text", "reasoning", "tools"),
                        128_000, 16_000),
            ModelPreset("meta-llama/Llama-3.3-70B-Instruct-Turbo",
                        "Llama 3.3 70B Turbo", ("llama-70b-tg",),
                        ("text", "tools"), 131_000, 8_000),
            ModelPreset("moonshotai/Kimi-K2-Instruct",
                        "Kimi K2 Instruct (Together)",
                        ("tg-kimi",),
                        ("text", "tools"), 200_000, 16_000),
        ),
    ),
    ProviderPreset(
        id="fireworks",
        name="Fireworks AI",
        base_url="https://api.fireworks.ai/inference/v1",
        env_var="FIREWORKS_API_KEY",
        models=(
            ModelPreset("accounts/fireworks/models/qwen3-235b-a22b-instruct-2507",
                        "Qwen 3 235B Instruct (Fireworks)", ("fw-qwen3",),
                        ("text", "tools"), 262_000, 32_000),
            ModelPreset("accounts/fireworks/models/qwen3-coder-480b-a35b-instruct",
                        "Qwen 3 Coder 480B (Fireworks)", ("fw-coder",),
                        ("text", "tools"), 262_000, 32_000),
            ModelPreset("accounts/fireworks/models/deepseek-v3p1",
                        "DeepSeek V3.1 (Fireworks)", ("fw-ds31",),
                        ("text", "reasoning", "tools"),
                        128_000, 16_000),
            ModelPreset("accounts/fireworks/models/llama-v3p3-70b-instruct",
                        "Llama 3.3 70B (Fireworks)", ("fw-llama",),
                        ("text", "tools"), 131_000, 8_000),
            ModelPreset("accounts/fireworks/models/kimi-k2-instruct",
                        "Kimi K2 (Fireworks)", ("fw-kimi",),
                        ("text", "tools"), 200_000, 16_000),
        ),
    ),
    ProviderPreset(
        id="cerebras",
        name="Cerebras",
        base_url="https://api.cerebras.ai/v1",
        env_var="CEREBRAS_API_KEY",
        notes=("Wafer-scale inference, ridiculously fast (>2000 tok/s). "
               "Available models depend on your tier — Refresh after adding "
               "the key to fetch what's actually accessible."),
        models=(
            # Confirmed via live `/v1/models` curl 2026-05.
            # Auto-discovery (Refresh after key save) re-syncs this list.
            ModelPreset("llama3.1-8b", "Llama 3.1 8B",
                        ("cb-llama-8b",), ("text", "tools"),
                        128_000, 8_000),
            ModelPreset("qwen-3-235b-a22b-instruct-2507",
                        "Qwen 3 235B Instruct",
                        ("cb-qwen3-235",), ("text", "tools"),
                        128_000, 32_000),
            ModelPreset("gpt-oss-120b", "GPT-OSS 120B",
                        ("cb-oss-120b",), ("text", "tools", "reasoning"),
                        131_000, 32_000),
            ModelPreset("zai-glm-4.7", "GLM 4.7 (Cerebras)",
                        ("cb-glm",), ("text", "tools"),
                        128_000, 16_000),
        ),
    ),
    ProviderPreset(
        id="deepinfra",
        name="DeepInfra",
        base_url="https://api.deepinfra.com/v1/openai",
        env_var="DEEPINFRA_API_KEY",
        models=(
            ModelPreset("Qwen/Qwen3-235B-A22B-Instruct-2507",
                        "Qwen 3 235B Instruct (DI)", ("di-qwen3",),
                        ("text", "tools"), 262_000, 32_000),
            ModelPreset("deepseek-ai/DeepSeek-V3.1",
                        "DeepSeek V3.1 (DI)", ("di-ds31",),
                        ("text", "reasoning", "tools"),
                        128_000, 16_000),
            ModelPreset("meta-llama/Meta-Llama-3.3-70B-Instruct",
                        "Llama 3.3 70B (DI)", ("di-llama",),
                        ("text", "tools"), 131_000, 8_000),
        ),
    ),
    ProviderPreset(
        id="mistral",
        name="Mistral La Plateforme",
        base_url="https://api.mistral.ai/v1",
        env_var="MISTRAL_API_KEY",
        models=(
            ModelPreset("mistral-large-latest", "Mistral Large",
                        ("ml",), ("text", "tools"), 128_000, 8_000),
            ModelPreset("mistral-medium-latest", "Mistral Medium",
                        ("mm",), ("text", "tools"), 128_000, 8_000),
            ModelPreset("mistral-small-latest", "Mistral Small",
                        ("ms",), ("text", "tools"), 128_000, 8_000),
            ModelPreset("codestral-latest", "Codestral",
                        ("cs",), ("text", "tools"), 256_000, 16_000),
            ModelPreset("ministral-8b-latest", "Ministral 8B",
                        ("min8",), ("text", "tools"), 128_000, 8_000),
            ModelPreset("pixtral-large-latest", "Pixtral Large",
                        ("px",), ("text", "vision", "tools"),
                        128_000, 8_000),
        ),
    ),
    ProviderPreset(
        id="xai",
        name="xAI (Grok)",
        base_url="https://api.x.ai/v1",
        env_var="XAI_API_KEY",
        models=(
            ModelPreset("grok-4", "Grok 4",
                        ("xai-grok",),
                        ("text", "vision", "reasoning", "tools"),
                        256_000, 16_000),
            ModelPreset("grok-4-fast-reasoning", "Grok 4 Fast (reasoning)",
                        ("grok-fast",),
                        ("text", "reasoning", "tools"),
                        256_000, 16_000),
            ModelPreset("grok-4-fast-non-reasoning", "Grok 4 Fast",
                        ("grok-fast-nr",),
                        ("text", "tools"),
                        256_000, 16_000),
            ModelPreset("grok-3", "Grok 3",
                        ("grok3",), ("text", "tools"),
                        131_000, 16_000),
            ModelPreset("grok-3-mini", "Grok 3 mini",
                        ("grok3-mini",),
                        ("text", "reasoning", "tools"),
                        131_000, 16_000),
            ModelPreset("grok-code-fast-1", "Grok Code Fast 1",
                        ("grok-code",), ("text", "tools"),
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
