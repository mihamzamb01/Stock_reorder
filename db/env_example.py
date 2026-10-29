"""Regenerate `.env.example` from `.env`, with every secret blanked.

    python db/setup.py --emit-env-example

Copies `.env` to `.env.example` but replaces the value of every key-looking
variable with an empty string, and redacts any password embedded in a URL, so
the real API keys and database passwords never end up in a file that might be
committed.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"

# Variables whose values are secret.
SECRET_NAMES = (
    "API_KEY",
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
)

# A variable assignment: NAME=value (value may be empty or quoted).
_ASSIGNMENT = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")

# user:password@host inside a URL.
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[A-Za-z0-9+.\-]+://)(?P<user>[^:@/\s]+):(?P<password>[^@/\s]*)@"
)

PLACEHOLDER = "PASTE_PASSWORD_HERE"


def is_secret(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in SECRET_NAMES)


def redact_url_credentials(value: str) -> str:
    """Replace the password in any URL inside the value."""
    return _URL_CREDENTIALS.sub(
        lambda m: f"{m.group('scheme')}{m.group('user')}:{PLACEHOLDER}@", value
    )


def blank_secrets(text: str) -> str:
    """Replace the value of every secret-looking assignment with nothing.

    URL-valued variables keep their shape, but the password inside them is
    replaced with a placeholder.
    """
    out: list[str] = []
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if match and is_secret(match.group("name")):
            name = match.group("name")
            out.append(f"{name}=")
        elif match:
            value = match.group("value")
            if "://" in value:
                out.append(f"{match.group('name')}={redact_url_credentials(value)}")
            else:
                out.append(line)
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def emit_env_example(env_path: Path = ENV_PATH, example_path: Path = EXAMPLE_PATH) -> Path:
    """Write the redacted example file. Returns its path."""
    if not env_path.exists():
        raise FileNotFoundError(f"{env_path} does not exist - create it first.")
    example_path.write_text(blank_secrets(env_path.read_text(encoding="utf-8")), encoding="utf-8")
    return example_path


if __name__ == "__main__":
    path = emit_env_example()
    print(f"Wrote {path} (secrets blanked)")
