"""Semantic version bumping tool.

Usage:

    python scripts/bump_version.py patch    # 0.2.0 -> 0.2.1
    python scripts/bump_version.py minor    # 0.2.0 -> 0.3.0
    python scripts/bump_version.py major    # 0.2.0 -> 1.0.0
    python scripts/bump_version.py --set 1.2.3

Updates ``VERSION`` and ``pyproject.toml``, then optionally creates a git tag.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"
PYPROJECT = ROOT / "pyproject.toml"

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$")


def parse_version(raw: str) -> tuple[int, int, int]:
    m = SEMVER_RE.match(raw.strip())
    if not m:
        raise ValueError(f"Invalid SemVer: {raw!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def read_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def write_version(version: str) -> None:
    VERSION_FILE.write_text(version + "\n", encoding="utf-8")
    # Update pyproject.toml
    text = PYPROJECT.read_text(encoding="utf-8")
    text = re.sub(r'^version = ".*"$', f'version = "{version}"', text, flags=re.MULTILINE)
    PYPROJECT.write_text(text, encoding="utf-8")


def bump(part: str) -> str:
    major, minor, patch = parse_version(read_version())
    if part == "major":
        major += 1
        minor = 0
        patch = 0
    elif part == "minor":
        minor += 1
        patch = 0
    elif part == "patch":
        patch += 1
    else:
        raise ValueError(f"Unknown bump part: {part!r}")
    return f"{major}.{minor}.{patch}"


def tag_exists(tag: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    return result.returncode == 0


def create_tag(version: str, message: str | None = None) -> None:
    tag = f"v{version}"
    if tag_exists(tag):
        print(f"Tag {tag} already exists - skipping.")
        return
    msg = message or f"Release {version}"
    subprocess.run(["git", "tag", "-a", tag, "-m", msg], cwd=str(ROOT), check=True)
    print(f"Created tag {tag}")


def main(argv: list[str]) -> int:
    if "--set" in argv:
        idx = argv.index("--set")
        version = argv[idx + 1]
        # Validate
        parse_version(version)
    else:
        part = argv[1] if len(argv) > 1 else "patch"
        version = bump(part)

    write_version(version)
    print(f"Version bumped to {version}")

    # Optionally tag
    if "--tag" in argv:
        create_tag(version)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
