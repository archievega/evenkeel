#!/usr/bin/env python3
"""Build the static API reference published to GitHub Pages.

    python tools/build_docs.py [--out site]

The page is the same Scalar renderer the application serves at `/scalar` on a
local run, over the same committed `openapi.json`. The difference is who can see
it: `/scalar` requires cloning the repository and starting the stack, and a link
does not.

The spec is regenerated rather than copied, so a page that builds is also proof
the document matches the application. If they have drifted, this fails here
exactly as `make schema-check` fails in CI.
"""

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

# Pinned, with the hash of that exact file. Bumping the version means
# recomputing this — `openssl dgst -sha384 -binary <file> | openssl base64 -A` —
# and the test in tests/unit/test_docs_page.py fails when they disagree.
SCALAR_VERSION = "1.65.1"

PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>evenkeel API</title>
    <meta
      name="description"
      content="Wallet ledger API. Every failure is an RFC 9457 problem document; branch on code, never on title."
    />
    <link rel="icon" href="data:," />
    <style>
      body { margin: 0; }
      .banner {
        font: 14px/1.5 ui-sans-serif, system-ui, sans-serif;
        padding: 10px 16px; background: #1a1a1a; color: #d4d4d4;
        border-bottom: 1px solid #333;
      }
      .banner a { color: #8ab4f8; }
    </style>
  </head>
  <body>
    <div class="banner">
      Generated from <code>openapi.json</code> at
      <a href="https://github.com/archievega/evenkeel">archievega/evenkeel</a>.
      Errors are <a href="https://www.rfc-editor.org/rfc/rfc9457">RFC 9457</a>
      problem documents &mdash; branch on <code>code</code>, never on
      <code>title</code>.
    </div>
    <script
      id="api-reference"
      data-url="openapi.json"
      data-configuration='{"theme":"purple","darkMode":true,"hideDownloadButton":false}'
    ></script>
    <!-- Pinned and integrity-checked. An unpinned CDN tag on a public page is
         a standing invitation: whoever controls that URL controls what runs in
         a reader's browser. Renovate moves the version and the hash together;
         `tools/build_docs.py` is where both live. -->
    <script
      src="https://cdn.jsdelivr.net/npm/@scalar/api-reference@__SCALAR_VERSION__/dist/browser/standalone.js"
      integrity="sha384-G6dkutu2k5IYVyNESLoFIpgaHx38IJTZ/HhrwN0fecTle9te75y8Kru3rJEJ0ZJV"
      crossorigin="anonymous"
    ></script>
  </body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "site")
    args = parser.parse_args()

    from dump_openapi import DEFAULT_TARGET, render

    rendered = render()
    committed = DEFAULT_TARGET.read_text(encoding="utf-8")
    if rendered != committed:
        # Publishing a document that does not describe the application is worse
        # than publishing nothing: a consumer has no way to tell.
        print(
            "openapi.json does not match the application — run `make schema` "
            "and review the diff before publishing",
            file=sys.stderr,
        )
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(
        PAGE.replace("__SCALAR_VERSION__", SCALAR_VERSION), encoding="utf-8"
    )
    shutil.copyfile(DEFAULT_TARGET, args.out / "openapi.json")
    print(f"wrote {args.out}/index.html and {args.out}/openapi.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
