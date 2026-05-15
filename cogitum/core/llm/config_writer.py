"""
Mutating providers.toml with tomlkit so user comments and formatting survive.

The plain `_dump_toml` in loader.py is used only for fresh writes (settings,
seed). For any modification of an existing user-edited file we go through
`ConfigWriter` so we don't trash hand-tuned blocks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomlkit
from tomlkit import TOMLDocument, comment, document, dumps, inline_table, nl, parse, table
from tomlkit.items import Table

from .loader import _PROVIDERS_PATH, seed_default_config


_DEFAULT_BETAS_NOTE = "secret_ref schemes: env:VAR / keyring:svc:user / vault:id / oauth:<id> / plain:<v>"


class ConfigWriter:
    """Read, mutate and persist providers.toml without losing comments."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _PROVIDERS_PATH
        if not self.path.exists():
            seed_default_config(self.path)
        self.doc: TOMLDocument = parse(self.path.read_text(encoding="utf-8"))

    # ---- read ---------------------------------------------------------

    def providers(self) -> dict[str, Any]:
        return dict(self.doc.get("providers") or {})

    def provider(self, pid: str) -> Any | None:
        return (self.doc.get("providers") or {}).get(pid)

    def has_provider(self, pid: str) -> bool:
        return self.provider(pid) is not None

    # ---- mutate -------------------------------------------------------

    def set_enabled(self, pid: str, enabled: bool) -> None:
        p = self._provider_table(pid)
        p["enabled"] = enabled

    def set_key(
        self,
        pid: str,
        key_id: str,
        secret_ref: str,
        *,
        weight: float | None = None,
        rpm_limit: int | None = None,
        notes: str = "",
    ) -> None:
        p = self._provider_table(pid)
        keys = p.get("keys")
        if not isinstance(keys, Table):
            keys = table()
            p["keys"] = keys
        if key_id in keys:
            entry = keys[key_id]
        else:
            entry = table()
            keys[key_id] = entry
        entry["secret_ref"] = secret_ref
        if weight is not None:
            entry["weight"] = float(weight)
        if rpm_limit is not None:
            entry["rpm_limit"] = int(rpm_limit)
        if notes:
            entry["notes"] = notes

    def remove_key(self, pid: str, key_id: str) -> None:
        p = self.provider(pid)
        if not p:
            return
        keys = p.get("keys")
        if isinstance(keys, Table) and key_id in keys:
            del keys[key_id]

    def add_provider(
        self,
        pid: str,
        *,
        name: str,
        format: str,
        base_url: str,
        auth: str = "bearer",
        enabled: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if "providers" not in self.doc:
            self.doc["providers"] = table()
        providers = self.doc["providers"]
        if pid in providers:
            return  # do not clobber
        block = table()
        block.add(comment(f"Added by `cog setup` — {name}"))
        block["name"] = name
        block["format"] = format
        block["base_url"] = base_url
        block["auth"] = auth
        block["enabled"] = enabled
        if extra_headers:
            extra = table()
            headers = table()
            for k, v in extra_headers.items():
                headers[k] = v
            extra["headers"] = headers
            block["extra"] = extra
        block["keys"] = table()
        block["models"] = table()
        providers[pid] = block

    def add_model(
        self,
        pid: str,
        model_id: str,
        *,
        display: str,
        aliases: list[str] | None = None,
        capabilities: list[str] | None = None,
        context_window: int = 8192,
        max_output_tokens: int = 4096,
    ) -> None:
        p = self._provider_table(pid)
        models = p.get("models")
        if not isinstance(models, Table):
            models = table()
            p["models"] = models
        if model_id in models:
            return
        m = table()
        m["display"] = display
        if aliases:
            m["aliases"] = aliases
        m["capabilities"] = capabilities or ["text", "tools"]
        m["context_window"] = int(context_window)
        m["max_output_tokens"] = int(max_output_tokens)
        models[model_id] = m

    # ---- save ---------------------------------------------------------

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text(dumps(self.doc), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    # ---- helpers ------------------------------------------------------

    def _provider_table(self, pid: str) -> Table:
        providers = self.doc.get("providers")
        if not isinstance(providers, Table):
            providers = table()
            self.doc["providers"] = providers
        if pid not in providers:
            raise KeyError(f"provider not found: {pid}")
        return providers[pid]


__all__ = ["ConfigWriter"]


# silence unused
_ = (document, dumps, inline_table, nl, _DEFAULT_BETAS_NOTE, tomlkit)
