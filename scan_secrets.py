"""Scan the project for secrets.

    python scan_secrets.py            # scan the working tree
    python scan_secrets.py <dir>      # scan a directory (e.g. a staged copy)

Findings are graded:

* **CRITICAL** - the file contains a byte-for-byte copy of a secret that is
  live in your `.env` right now. This is what must never be delivered.
* **WARNING** - the file contains something that *looks* like a secret. Often
  a placeholder or a test fixture, so review it rather than panic.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Patterns that look like credentials.
PATTERNS = {
    "LarpRouter key": re.compile(r"lrsk-[A-Za-z0-9]{16,}"),
    "OpenRouter key": re.compile(r"sk-or-v1-[A-Za-z0-9]{16,}"),
    "NVIDIA key": re.compile(r"nvapi-[A-Za-z0-9_\-]{16,}"),
    "Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9\-]{16,}"),
    "password in URL": re.compile(r"psycopg://[^:@/\s]+:([^@/\s]+)@"),
}

# Values that are clearly placeholders or deliberate test fixtures.
SAFE_VALUES = {
    "",
    "<PASSWORD>",
    "<YOUR-PASSWORD>",
    "PASTE_PASSWORD_HERE",
    "PASTE_NEW_PASSWORD_HERE",
    "dummy",
    "test",
    "your-key",
    "your-key-here",
    "sk-or-test",
    "nvapi-test",
    "lrsk-test",
    "stub",
    "lrsk-abcdefghijklmnop1234",
    # Documentation examples.
    "p%40ss%20word",
    "{encoded}",
}

# A password shorter than this is not worth reporting.
MIN_PASSWORD_LENGTH = 8

SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".git", "dist"}
BINARY_SUFFIXES = {".db", ".pyc", ".pyo", ".zip", ".png", ".jpg", ".ico"}


def live_secrets(env_path: Path = ROOT / ".env") -> list[str]:
    """The actual secret values configured in `.env`."""
    if not env_path.exists():
        return []
    values: list[str] = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if len(value) < 12:
            continue
        if any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            values.append(value)
        elif "://" in value and "@" in value:
            # A URL with credentials: the password is the secret part.
            password = value.split("://", 1)[1].split("@", 1)[0]
            if ":" in password:
                password = password.split(":", 1)[1]
            if len(password) >= 12:
                values.append(password)
    return values


def scan(target: Path, secrets: list[str]) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """Return (critical, warning) findings."""
    critical: list[tuple[str, str, str]] = []
    warning: list[tuple[str, str, str]] = []

    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(target)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for secret in secrets:
            if secret and secret in text:
                critical.append((str(rel), "live secret", secret[:32]))

        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                value = match.group(1) if match.groups() else match.group(0)
                value = value.strip()
                if name == "password in URL" and len(value) < MIN_PASSWORD_LENGTH:
                    continue
                if value in SAFE_VALUES or value.startswith("PASTE"):
                    continue
                if any(secret and secret in value for secret in secrets):
                    continue  # already reported as critical
                warning.append((str(rel), name, value[:32]))

    return critical, warning


def main(argv: list[str]) -> int:
    target = Path(argv[1]).resolve() if len(argv) > 1 else ROOT
    secrets = live_secrets()
    critical, warning = scan(target, secrets)

    print(f"Scanning {target}")
    print(f"  live secrets loaded from .env : {len(secrets)}")
    print()

    if critical:
        print("CRITICAL - these files contain a live secret:")
        for rel, kind, snippet in critical:
            print(f"  !! {rel:38} {kind:14} {snippet}")
    else:
        print("[ok] no live secrets found")

    if warning:
        print()
        print("WARNING - these look like credentials (review them):")
        for rel, kind, snippet in warning:
            print(f"  ?  {rel:38} {kind:14} {snippet}")
    elif not critical:
        print("[ok] nothing else that looks like a credential")

    print()
    return 1 if critical else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
