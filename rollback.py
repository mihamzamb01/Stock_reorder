"""Rollback scenarios and helpers for the stock-reorder pipeline.

Covers:

* Database rollback (re-seed to last known good state)
* Version rollback (revert to previous tagged release)
* Kill switch activation (emergency halt)
* Configuration rollback (restore last known good .env / settings)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "VERSION"


# ---------------------------------------------------------------------------
# Database rollback
# ---------------------------------------------------------------------------

def reseed_database(db_path: Path) -> int:
    """Re-seed the database from scratch (rollback to clean state).

    Returns the subprocess return code.
    """
    from db.setup import seed_database

    seed_database(db_path)
    return 0


def restore_database_from_backup(backup_path: Path, target_path: Path) -> None:
    """Restore a database file from a backup copy."""
    shutil.copy2(backup_path, target_path)


# ---------------------------------------------------------------------------
# Version rollback
# ---------------------------------------------------------------------------

def read_current_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def rollback_version(previous_version: str) -> None:
    """Roll back the project version to a previous SemVer.

    Updates VERSION and pyproject.toml. Does not create a git tag.
    """
    import re

    semver_re = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")
    if not semver_re.match(previous_version.strip()):
        raise ValueError(f"Invalid SemVer: {previous_version!r}")

    VERSION_FILE.write_text(previous_version.strip() + "\n", encoding="utf-8")

    pyproject = ROOT / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    text = re.sub(
        r'^version = ".*"$',
        f'version = "{previous_version.strip()}"',
        text,
        flags=re.MULTILINE,
    )
    pyproject.write_text(text, encoding="utf-8")


def list_git_tags() -> list[str]:
    """Return all git version tags sorted newest first."""
    result = subprocess.run(
        ["git", "tag", "--sort=-version:refname", "v*"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        return []
    tags = [t for t in result.stdout.splitlines() if t.startswith("v")]
    return tags


def previous_tag() -> str | None:
    """Return the previous version tag (excluding the current one)."""
    current = read_current_version()
    tags = list_git_tags()
    current_tag = f"v{current}"
    for tag in tags:
        if tag != current_tag:
            return tag[1:]  # strip leading 'v'
    return None


# ---------------------------------------------------------------------------
# Kill switch (emergency halt)
# ---------------------------------------------------------------------------

def activate_kill_switch(reason: str) -> None:
    """Activate the kill switch to halt all pipeline runs."""
    from kill_switch import activate

    activate(reason=reason)


def deactivate_kill_switch() -> None:
    """Deactivate the kill switch to allow pipeline runs again."""
    from kill_switch import deactivate

    deactivate()


# ---------------------------------------------------------------------------
# Configuration rollback
# ---------------------------------------------------------------------------

def rollback_env(env_backup: Path) -> None:
    """Restore .env from a backup file."""
    target = ROOT / ".env"
    if env_backup.exists():
        shutil.copy2(env_backup, target)
    else:
        raise FileNotFoundError(f"Env backup not found: {env_backup}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    if not argv:
        print("Usage: python rollback.py <command> [args]")
        print("Commands:")
        print("  reseed-db <db_path>              Re-seed database to clean state")
        print("  version-rollback <version>       Roll back project version")
        print("  previous-tag                     Show previous git version tag")
        print("  kill-switch <reason>             Activate kill switch")
        print("  unkill                           Deactivate kill switch")
        return 1

    command = argv[0]

    try:
        if command == "reseed-db":
            db_path = Path(argv[1])
            reseed_database(db_path)
            print(f"Database re-seeded: {db_path}")
        elif command == "version-rollback":
            rollback_version(argv[1])
            print(f"Version rolled back to {argv[1]}")
        elif command == "previous-tag":
            tag = previous_tag()
            if tag:
                print(tag)
            else:
                print("No previous tag found")
        elif command == "kill-switch":
            activate_kill_switch(" ".join(argv[1:]) or "manual rollback")
            print("Kill switch activated")
        elif command == "unkill":
            deactivate_kill_switch()
            print("Kill switch deactivated")
        else:
            print(f"Unknown command: {command}")
            return 1
    except Exception as exc:  # noqa: BLE001 - CLI top-level
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
