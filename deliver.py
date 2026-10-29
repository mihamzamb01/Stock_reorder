"""Package the project for delivery, with every secret removed.

    python deliver.py

Builds `dist/stock-reorder-agents.zip` containing the source, the tests and a
redacted `.env.example`, and nothing else: no `.env`, no virtual environment,
no caches, no local database.

Before the archive is written, the staged copy is scanned for your live
credentials. If anything is found the build aborts.

    python deliver.py --out dist/my-copy.zip
    python deliver.py --include-db     # also ship the local SQLite demo database
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from scan_secrets import scan  # noqa: E402

# Never copied into the delivery.
EXCLUDE_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".git",
    "dist",
    ".idea",
    ".vscode",
    "runs",
    "node_modules",
}
EXCLUDE_FILES = {
    ".env",  # holds the real keys
    "report.json",
    ".DS_Store",
    "dashboard.html",  # observability output, regenerable
}
EXCLUDE_SUFFIXES = {".db", ".db-journal", ".pyc", ".pyo", ".log"}

# Files that are worth shipping even though they are usually excluded.
KEEP_FILES = {".env.example"}


def _ignore(_directory: str, names: list[str]) -> set[str]:
    ignored = set()
    current = Path(_directory)
    for name in names:
        if name in EXCLUDE_DIRS:
            # Exception: built React dashboard ships with the delivery so the
            # recipient does not need Node - only the ROOT dist/ is excluded.
            if name == "dist" and current == ROOT / "dashboard":
                continue
            ignored.add(name)
        elif name in EXCLUDE_FILES:
            ignored.add(name)
        elif Path(name).suffix.lower() in EXCLUDE_SUFFIXES:
            ignored.add(name)
        elif name in KEEP_FILES:
            continue
    return ignored


def stage(target: Path, include_db: bool = False) -> None:
    """Copy the project into `target`, minus everything that must not ship."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    shutil.copytree(
        ROOT,
        target,
        ignore=_ignore,
        ignore_dangling_symlinks=True,
        dirs_exist_ok=True,
    )

    if not include_db:
        for db_file in target.glob("*.db"):
            db_file.unlink()

    # The staged .env.example must be the redacted one, always.
    example = ROOT / ".env.example"
    if example.exists():
        shutil.copy2(example, target / ".env.example")


def build_zip(stage_dir: Path, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stage_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(stage_dir))
    return out_path

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Package the project for delivery")
    parser.add_argument(
        "--out",
        default="dist/stock-reorder-agents.zip",
        help="path of the zip to write",
    )
    parser.add_argument(
        "--include-db",
        action="store_true",
        help="also include the local SQLite demo database",
    )
    parser.add_argument(
        "--keep-folder",
        action="store_true",
        help="also leave the extracted folder next to the zip (for Drive browsing)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="delete dist/ afterwards (once you have uploaded the package)",
    )
    args = parser.parse_args(argv)

    out_path = (ROOT / args.out).resolve()
    stage_dir = ROOT / "dist" / "_staged"

    print("Staging a clean copy...")
    stage(stage_dir, include_db=args.include_db)

    print("Scanning the staged copy for secrets...")
    from scan_secrets import live_secrets

    critical, warning = scan(stage_dir, live_secrets())
    if critical:
        print()
        print("ABORTED - the staged copy still contains live secrets:")
        for rel, kind, snippet in critical:
            print(f"  !! {rel:38} {kind:14} {snippet}")
        shutil.rmtree(stage_dir, ignore_errors=True)
        return 1

    for rel, kind, snippet in warning:
        print(f"  ?  {rel:38} {kind:14} {snippet}  (reviewed, not a live secret)")

    archive = build_zip(stage_dir, out_path)

    folder = None
    if args.keep_folder:
        folder = ROOT / "dist" / "stock-reorder-agents"
        if folder.exists():
            shutil.rmtree(folder)
        shutil.copytree(stage_dir, folder)

    shutil.rmtree(stage_dir, ignore_errors=True)

    files = list(zipfile.ZipFile(archive).namelist())
    size_kb = archive.stat().st_size / 1024

    print()
    print(f"Built    : {archive}")
    print(f"Files    : {len(files)}")
    print(f"Size     : {size_kb:.0f} KB")
    if folder:
        print(f"Folder   : {folder}  (same contents, unzipped)")
    print()
    print("The archive contains:")
    for name in sorted(files):
        if "/" not in name:
            print(f"  {name}")
    print()
    print("It does NOT contain:")
    print("  .env              (your API keys and database password)")
    print("  .venv/            (virtual environment)")
    print("  *.db              (local database)")
    print("  __pycache__/ etc  (caches)")
    print()
    print("The recipient copies .env.example to .env and fills in their own keys.")
    if args.clean:
        shutil.rmtree(ROOT / "dist", ignore_errors=True)
        print()
        print("dist/ removed - safe to delete it from your Drive upload source.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
