"""Application settings: environment loading and per-role model routing."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

PROVIDERS = ("openrouter", "nvidia", "larprouter", "custom")

PROVIDER_ENDPOINTS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "larprouter": "https://larprouter.com/v1",
    "custom": "",  # must come from the environment
}

PROVIDER_KEY_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "larprouter": "LARP_ROUTER_API_KEY",
    "custom": "LLM_API_KEY",
}

# Short env-var prefixes per role, so MODEL_REORDER_NAME and
# MODEL_REORDER_ANALYST_NAME both work.
ROLE_ENV_PREFIX = {
    "reorder_analyst": "REORDER",
    "supplier_scout": "SUPPLIER",
    "cost_optimizer": "COST",
}


@dataclass(frozen=True)
class ModelSpec:
    """Which model backs one agent role."""

    role: str
    provider: str
    model: str
    temperature: float | None = None
    max_tokens: int = 4096
    reasoning_effort: str | None = None
    reasoning_body: dict[str, Any] | None = None
    description: str = ""
    # Resolved from the environment - see load_routing().
    base_url: str = ""
    key_env: str = ""

    @property
    def label(self) -> str:
        return f"{self.role}: {self.provider}/{self.model}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "key_env": self.key_env,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
        }


# Default routing. Each role is overridable via MODEL_<ROLE>_* env vars.
#
# These point at LarpRouter's free-tier models. Any OpenAI-compatible platform
# can be used instead - see the README for the model tables and for how to
# repoint a role at a different endpoint.
DEFAULT_ROUTING: dict[str, ModelSpec] = {
    "reorder_analyst": ModelSpec(
        role="reorder_analyst",
        provider="larprouter",
        model="deepseek-v4.1-flash",
        temperature=0.1,
        max_tokens=2048,
        description="One agent per short SKU: urgency and order quantity.",
    ),
    "supplier_scout": ModelSpec(
        role="supplier_scout",
        provider="larprouter",
        model="glm-5.3-flash",
        temperature=0.1,
        max_tokens=8192,
        description="Checks the DB for supplier offers and screens them.",
    ),
    "cost_optimizer": ModelSpec(
        role="cost_optimizer",
        provider="larprouter",
        model="glm-5.3-flash",
        max_tokens=8192,
        description="Computes landed cost and splits volume to minimise spend.",
    ),
}


def _env_setting(prefixes: list[str], suffix: str) -> tuple[bool, str | None]:
    """Look up ``MODEL_<PREFIX>_<SUFFIX>`` for each prefix in order.

    Returns ``(was_set, value)``. A variable that is present but blank counts
    as set with a value of ``None`` - that is how you say "use the provider
    default" for something like temperature.
    """
    for prefix in prefixes:
        raw = os.getenv(f"MODEL_{prefix}_{suffix}")
        if raw is not None:
            stripped = raw.strip()
            return True, stripped or None
    return False, None


def _as_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _env_float(name: str) -> float | None:
    """Read a float from the environment, ignoring blanks and bad values."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return _as_float(raw.strip())


def _as_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _as_json(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def load_routing() -> dict[str, ModelSpec]:
    """Merge DEFAULT_ROUTING with MODEL_<ROLE>_* environment overrides.

    Each role resolves its endpoint and its API key independently, so any
    OpenAI-compatible platform can back any role:

    base URL   MODEL_<ROLE>_BASE_URL  >  PROVIDER_BASE_URL  >  provider default
    key var    MODEL_<ROLE>_KEY_ENV   >  provider default
    key value  MODEL_<ROLE>_API_KEY   >  the key variable named above
               >  LLM_API_KEY (global fallback)

    Both the short alias (MODEL_COST_NAME) and the full role name
    (MODEL_COST_OPTIMIZER_NAME) are accepted; the alias wins.
    """
    routing: dict[str, ModelSpec] = {}
    global_base = (os.getenv("PROVIDER_BASE_URL") or "").strip()

    for role, spec in DEFAULT_ROUTING.items():
        prefixes = [ROLE_ENV_PREFIX.get(role, role.upper()), role.upper()]

        _, raw_provider = _env_setting(prefixes, "PROVIDER")
        provider = (raw_provider or spec.provider).strip().lower()
        if provider not in PROVIDERS:
            provider = spec.provider

        _, raw_model = _env_setting(prefixes, "NAME")
        model = (raw_model or spec.model).strip() or spec.model

        # --- endpoint ----------------------------------------------------
        _, raw_base = _env_setting(prefixes, "BASE_URL")
        base_url = (
            raw_base or global_base or PROVIDER_ENDPOINTS.get(provider, "")
        ).strip()

        # --- which env var holds the key ---------------------------------
        _, raw_key_env = _env_setting(prefixes, "KEY_ENV")
        key_env = (
            (raw_key_env or "").strip()
            or PROVIDER_KEY_ENV.get(provider, "LLM_API_KEY")
        )

        was_set, raw_temperature = _env_setting(prefixes, "TEMPERATURE")
        temperature = _as_float(raw_temperature) if was_set else spec.temperature

        _, raw_max_tokens = _env_setting(prefixes, "MAX_TOKENS")
        max_tokens = _as_int(raw_max_tokens, spec.max_tokens)

        _, raw_reasoning = _env_setting(prefixes, "REASONING")
        reasoning = raw_reasoning or spec.reasoning_effort

        _, raw_reasoning_body = _env_setting(prefixes, "REASONING_BODY")
        reasoning_body = _as_json(raw_reasoning_body)

        routing[role] = replace(
            spec,
            provider=provider,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning,
            reasoning_body=reasoning_body,
            base_url=base_url,
            key_env=key_env,
        )
    return routing


@dataclass(frozen=True)
class Settings:
    db_path: Path | None = None
    warehouse: str | None = None
    max_items: int = 5
    budget_cap: float | None = None
    max_cost_attempts: int = 2
    verbose: bool = True

    @property
    def routing(self) -> dict[str, ModelSpec]:
        return load_routing()


def config_freshness() -> str | None:
    """Warn when `.env` looks stale relative to the code that reads it.

    A common trap: the file is edited outside the editor (or by a script), but
    the editor still holds the old buffer and writes it back on auto-save.
    The result is a `.env` that is older than the settings module and quietly
    configures the wrong models.
    """
    from datetime import datetime

    env_path = ROOT / ".env"
    code_path = ROOT / "config" / "settings.py"
    if not env_path.exists() or not code_path.exists():
        return None
    if env_path.stat().st_mtime >= code_path.stat().st_mtime:
        return None

    env_time = datetime.fromtimestamp(env_path.stat().st_mtime)
    code_time = datetime.fromtimestamp(code_path.stat().st_mtime)
    return (
        f"{env_path.name} looks stale: it was last written {env_time:%Y-%m-%d %H:%M} "
        f"but config/settings.py was changed at {code_time:%Y-%m-%d %H:%M}.\n"
        "  The .env may have been overwritten by a stale editor buffer.\n"
        "  Open it, choose 'File -> Revert File' (or press Ctrl+Shift+P ->\n"
        "  'Developer: Reload Window'), then re-paste your API key."
    )


def env_file_values() -> dict[str, str]:
    """The values in `.env`, read directly from the file."""
    from dotenv import dotenv_values

    env_path = ROOT / ".env"
    if not env_path.exists():
        return {}
    return {k: v for k, v in dotenv_values(env_path).items() if v is not None}


def overridden_by_environment(names: Iterable[str]) -> list[str]:
    """Which of these variables the process environment overrides.

    `load_dotenv()` never replaces a variable that is already set, and VS Code's
    `python.terminal.useEnvFile` copies `.env` into the terminal when it
    starts. A terminal opened while `.env` was stale therefore keeps serving
    the old values, and the file on disk looks ignored.
    """
    file_values = env_file_values()
    out: list[str] = []
    for name in names:
        current = os.environ.get(name)
        if current is None:
            continue
        if name in file_values and file_values[name] == current:
            continue  # same value, so no conflict
        out.append(name)
    return out


def api_key_for(spec: ModelSpec) -> str:
    """The API key for one agent role.

    Looks in the role's own key variable first, then the variable the role's
    provider normally uses, then the global LLM_API_KEY.
    """
    candidates = [
        f"MODEL_{ROLE_ENV_PREFIX.get(spec.role, spec.role.upper())}_API_KEY",
        spec.key_env,
        "LLM_API_KEY",
    ]
    for env_name in candidates:
        if not env_name:
            continue
        key = os.getenv(env_name, "").strip()
        if key:
            return key

    raise RuntimeError(
        f"No API key for the '{spec.role}' role. Tried: "
        + ", ".join(c for c in candidates if c)
        + f". Add one of them to .env (the provider is '{spec.provider}')."
    )


def missing_keys(routing: dict[str, ModelSpec] | None = None) -> list[str]:
    """Which key variables are absent (used for a friendly pre-flight check)."""
    routing = routing if routing is not None else load_routing()
    out: list[str] = []
    seen: set[str] = set()
    for spec in routing.values():
        candidates = [
            f"MODEL_{ROLE_ENV_PREFIX.get(spec.role, spec.role.upper())}_API_KEY",
            spec.key_env,
            "LLM_API_KEY",
        ]
        if any(os.getenv(name, "").strip() for name in candidates if name):
            continue
        name = spec.key_env or "LLM_API_KEY"
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


# ---------------------------------------------------------------------------
# Supplier quality floors
# ---------------------------------------------------------------------------
# These are objective business rules, not judgement calls, so they are applied
# deterministically before the scout agent ranks what is left. A supplier that
# misses them is never considered, no matter how cheap it is.
DEFAULT_MAX_DEFECT_RATE_PCT = 5.0
DEFAULT_MIN_ON_TIME_PCT = 75.0


def quality_thresholds() -> tuple[float, float]:
    """(max defect rate %, min on-time %) - override with env vars."""
    max_defect = _env_float("MAX_DEFECT_RATE_PCT")
    min_on_time = _env_float("MIN_ON_TIME_PCT")
    return (
        DEFAULT_MAX_DEFECT_RATE_PCT if max_defect is None else max_defect,
        DEFAULT_MIN_ON_TIME_PCT if min_on_time is None else min_on_time,
    )


def interpreter_hint() -> str | None:
    """Warn when the project venv exists but a different interpreter is running.

    Mixing interpreters is the usual cause of "ModuleNotFoundError" for a
    package that is plainly listed in requirements.txt.
    """
    import sys

    venv = ROOT / ".venv"
    if not venv.exists():
        return None
    if Path(sys.prefix).resolve() == venv.resolve():
        return None
    return (
        "You are not running the project virtual environment.\n"
        f"  running : {sys.executable}\n"
        f"  prefer  : {venv / 'Scripts' / 'python.exe'}\n"
        "  either activate it, or run the command with that interpreter directly."
    )
