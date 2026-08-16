#!/usr/bin/env python3
"""Write the OpenAPI document to a file, deterministically.

`openapi.json` is committed and CI regenerates it to check for drift, exactly
as `alembic check` does for the database schema. The reasoning is the same in
both cases: the artefact other people build against should be visible in review,
and a change to it should be a diff someone approves rather than a side effect
nobody noticed.

Determinism matters here. Keys are sorted and the version is pinned to the
default rather than read from the environment, or the file would differ between
a laptop and a tagged build and the check would cry drift on every release.

    python tools/dump_openapi.py            # write openapi.json
    python tools/dump_openapi.py --check    # exit 1 if it would change
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = ROOT / "openapi.json"


def render() -> str:
    # Pinned before the app is imported: `create_app` reads APP_VERSION for
    # `info.version`, and a tagged CI build would otherwise produce a document
    # that differs from the committed one for a reason nobody cares about.
    os.environ["APP_VERSION"] = "dev"

    from dishka import make_async_container
    from dishka.integrations.fastapi import FastapiProvider

    from evenkeel.setup.app_factory import create_app
    from evenkeel.setup.config import AppConfig, Settings

    app = create_app(
        settings=Settings(app=AppConfig(environment="local")),
        container=make_async_container(FastapiProvider()),
    )
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail instead of writing")
    parser.add_argument("--out", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args()

    rendered = render()

    if not args.check:
        args.out.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.out.relative_to(ROOT)}")
        return 0

    if not args.out.exists():
        print(f"{args.out.relative_to(ROOT)} is missing — run tools/dump_openapi.py")
        return 1
    if args.out.read_text(encoding="utf-8") != rendered:
        print(
            f"{args.out.relative_to(ROOT)} is out of date.\n"
            "The API changed without the published contract changing with it. "
            "Run `make schema` and review the diff — that diff is the change "
            "your consumers will see."
        )
        return 1

    print(f"{args.out.relative_to(ROOT)} matches the application")
    return 0


if __name__ == "__main__":
    sys.exit(main())
