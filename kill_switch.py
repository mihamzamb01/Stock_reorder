"""Runtime kill switch for the stock-reorder pipeline.

A kill switch lets operators halt the pipeline immediately without changing
code. It is checked at key points:

* Before `main.py` starts (fast-fail exit code 3)
* Inside each agent node (via ``AgentRuntime.killed()``)

Activation methods (checked in order):

1. ``STOCK_REORDER_KILL_SWITCH=1`` environment variable
2. ``kill_switch.flag`` file in the project root or ``RUNS_DIR``
3. ``KILL_SWITCH_REASON`` - optional human-readable reason

Example:

    # Activate
    export STOCK_REORDER_KILL_SWITCH=1
    export KILL_SWITCH_REASON="emergency: supplier API outage"

    # Deactivate
    unset STOCK_REORDER_KILL_SWITCH
    rm -f kill_switch.flag
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Env var names
KILL_SWITCH_ENV = "STOCK_REORDER_KILL_SWITCH"
KILL_SWITCH_REASON_ENV = "KILL_SWITCH_REASON"

# Flag file locations (first existing wins)
KILL_SWITCH_FLAG_FILES = [
    ROOT / "kill_switch.flag",
]


def is_killed() -> bool:
    """Return True if the kill switch is active."""
    if os.getenv(KILL_SWITCH_ENV, "").strip() in {"1", "true", "yes", "on"}:
        return True
    for flag in KILL_SWITCH_FLAG_FILES:
        if flag.exists():
            return True
    return False


def reason() -> str | None:
    """Return the configured kill-switch reason, if any."""
    return os.getenv(KILL_SWITCH_REASON_ENV) or None


def flag_path() -> Path | None:
    """Return the path of the active kill-switch flag file, if any."""
    for flag in KILL_SWITCH_FLAG_FILES:
        if flag.exists():
            return flag
    return None


def activate(reason: str | None = None, flag_file: Path | None = None) -> None:
    """Activate the kill switch by writing a flag file.

    Args:
        reason: Human-readable reason stored in KILL_SWITCH_REASON env (for
            the current process) and written to the flag file.
        flag_file: Optional path for the flag file (defaults to the first
            location in ``KILL_SWITCH_FLAG_FILES``).
    """
    target = flag_file or KILL_SWITCH_FLAG_FILES[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["kill_switch=1"]
    if reason:
        lines.append(f"reason={reason}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[KILL_SWITCH_ENV] = "1"
    if reason:
        os.environ[KILL_SWITCH_REASON_ENV] = reason


def deactivate() -> None:
    """Deactivate the kill switch by removing all flag files and env vars."""
    for flag in KILL_SWITCH_FLAG_FILES:
        if flag.exists():
            flag.unlink()
    os.environ.pop(KILL_SWITCH_ENV, None)
    os.environ.pop(KILL_SWITCH_REASON_ENV, None)


def check_or_raise() -> None:
    """Raise ``RuntimeError`` if the kill switch is active."""
    if is_killed():
        msg = "Pipeline killed by kill switch"
        r = reason()
        if r:
            msg += f": {r}"
        raise RuntimeError(msg)
