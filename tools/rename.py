#!/usr/bin/env python3
"""Rename the package this template ships with.

A template you cannot rename in one command is an example, not a template.
Run this once, immediately after cloning, before writing any code of your own:

    python tools/rename.py --to myservice
    python tools/rename.py --to myservice --dry-run   # see the damage first

What it does NOT touch, on purpose:

* The ``APP__`` environment-variable prefix. It is deliberately independent of
  the package name so that renaming does not invalidate every deployment
  config, secret store entry and CI variable that already exists.
* Anything under ``.git``. History keeps the old name; that is what history is
  for.
* Migration files. They are dated artefacts that must keep behaving the way
  they did when they were written -- but they never import application code
  (see ``env.py``'s ``render_item``), so there is nothing in them to rename.
"""

import argparse
import keyword
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD = "evenkeel"

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".import_linter_cache",
    "htmlcov",
    "dist",
    "build",
    "node_modules",
}

# Everything else is treated as binary and left alone. An allowlist rather than
# a denylist: a stray .png or .whl silently corrupted by a text substitution is
# far worse than a file this misses, which you would notice immediately.
TEXT_SUFFIXES = {
    ".py",
    ".toml",
    ".cfg",
    ".ini",
    ".md",
    ".yml",
    ".yaml",
    ".json",
    ".txt",
    ".tape",
    ".sh",
    ".mako",
    ".importlinter",
    ".example",  # .env.example — skipped once, and the miss was silent
    "",  # extensionless: Dockerfile, Makefile, LICENSE
}

NAMED_FILES = {
    "Dockerfile",
    "Makefile",
    "LICENSE",
    ".importlinter",
    ".dockerignore",
    ".gitignore",
    ".env.example",
}


def validate(name: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        sys.exit(
            f"'{name}' is not a usable package name: use lowercase letters, "
            "digits and underscores, starting with a letter."
        )
    if keyword.iskeyword(name):
        sys.exit(f"'{name}' is a Python keyword.")
    if name == OLD:
        sys.exit(f"The package is already called '{name}'.")


def is_text(path: Path) -> bool:
    return path.name in NAMED_FILES or path.suffix in TEXT_SUFFIXES


def walk() -> list[Path]:
    found: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and is_text(path):
            found.append(path)
    return sorted(found)


def substitute(text: str, new: str) -> str:
    """Replace the package token in every casing this repo actually uses."""
    return (
        text.replace(OLD, new)
        .replace(OLD.capitalize(), new.capitalize())
        .replace(OLD.upper(), new.upper())
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True, help="new package name, e.g. myservice")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing anything",
    )
    args = parser.parse_args()

    new = args.to
    validate(new)

    package_dir = ROOT / "src" / OLD
    if not package_dir.is_dir():
        sys.exit(f"{package_dir} does not exist — is this the template root?")

    changed: list[tuple[Path, int]] = []
    for path in walk():
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if OLD not in original.lower():
            continue
        updated = substitute(original, new)
        if updated == original:
            continue
        hits = original.lower().count(OLD)
        changed.append((path, hits))
        if not args.dry_run:
            path.write_text(updated, encoding="utf-8")

    for path, hits in changed:
        print(f"  {hits:>4}  {path.relative_to(ROOT)}")

    total = sum(hits for _, hits in changed)
    print(f"\n{len(changed)} files, {total} occurrences")

    target = ROOT / "src" / new
    if args.dry_run:
        print(f"would move   src/{OLD} -> src/{new}")
        print("\nDry run: nothing was written.")
        return 0

    package_dir.rename(target)
    print(f"moved        src/{OLD} -> src/{new}")

    print(
        "\nDone. Now:\n"
        f"  1. rename the repository directory itself to '{new}' if you want it to match\n"
        "  2. uv sync\n"
        "  3. make check\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
