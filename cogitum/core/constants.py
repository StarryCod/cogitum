"""
cogitum.core.constants
~~~~~~~~~~~~~~~~~~~~~~
Project-wide constants that need a single source of truth.

Keep this file dependency-free: it is imported from low-level modules
(loader, mesh) that must not pull in tools / agent / TUI code.
"""
from __future__ import annotations


# Minimum per-turn output budget enforced for every model, regardless of
# what providers.toml or live /v1/models discovery says. Older preset
# entries shipped with 4K/8K caps which routinely get hit on long
# responses ("the model stopped mid-sentence"). Floor at 32K so even a
# pre-existing config silently uplifts on next load — no migration step
# required.
#
# The floor only applies to ModelConfig.max_output_tokens (the upper
# bound the agent is allowed to request). Per-provider [providers.X]
# max_tokens overrides in providers.toml continue to clamp the actual
# request downwards if the user wants a tighter budget.
MIN_MAX_OUTPUT_TOKENS = 32768


__all__ = ["MIN_MAX_OUTPUT_TOKENS"]
