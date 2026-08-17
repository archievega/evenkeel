"""The published page must not be able to run whatever a CDN feels like.

`/scalar` on a local run is one thing; a page on the public internet loading an
unpinned script is another. Whoever controls that URL controls what executes in
a reader's browser, and a repository with a control matrix should not be the one
demonstrating the failure.

The hash is checked against the file that is actually served, so a version bump
without a matching hash fails here rather than as a blank page nobody notices.
"""

import base64
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from build_docs import PAGE, SCALAR_VERSION

CDN = (
    "https://cdn.jsdelivr.net/npm/@scalar/api-reference@"
    f"{SCALAR_VERSION}/dist/browser/standalone.js"
)


def test_the_script_tag_is_pinned_and_integrity_checked() -> None:
    rendered = PAGE.replace("__SCALAR_VERSION__", SCALAR_VERSION)

    assert f"api-reference@{SCALAR_VERSION}/" in rendered, "unpinned CDN reference"
    assert 'integrity="sha384-' in rendered
    # Without this the browser silently skips the integrity check on a
    # cross-origin script, which is the failure that looks like success.
    assert 'crossorigin="anonymous"' in rendered


def test_the_spec_is_referenced_with_a_content_hash(tmp_path: Path) -> None:
    """A bare `openapi.json` is served from the browser cache after a deploy,
    so the reader gets the previous contract with no way to tell. Watched it
    happen: the CDN had the new document while the page rendered the old one."""
    from build_docs import main

    sys.argv = ["build_docs.py", "--out", str(tmp_path)]
    assert main() == 0
    page = (tmp_path / "index.html").read_text()

    reference = re.search(r'data-url="openapi\.json\?v=([0-9a-f]+)"', page)
    assert reference, "the spec is referenced without a cache-busting hash"
    assert (
        reference.group(1)
        == hashlib.sha256((tmp_path / "openapi.json").read_text().encode()).hexdigest()[
            : len(reference.group(1))
        ]
    )


def test_no_other_script_is_loaded_unchecked() -> None:
    rendered = PAGE.replace("__SCALAR_VERSION__", SCALAR_VERSION)
    external = re.findall(r'<script[^>]*src="(https?://[^"]+)"[^>]*>', rendered)
    with_integrity = re.findall(
        r'<script[^>]*src="https?://[^"]+"[^>]*integrity="[^"]+"', rendered, re.S
    ) + re.findall(
        r'<script[^>]*integrity="[^"]+"[^>]*src="https?://[^"]+"', rendered, re.S
    )

    assert len(external) == len(with_integrity), external


@pytest.mark.skipif(
    os.getenv("CI") is None and os.getenv("CHECK_SRI") is None,
    reason="reaches the CDN; runs in CI or with CHECK_SRI=1",
)
def test_the_hash_matches_the_file_that_will_be_served() -> None:
    """The half a static assertion cannot make.

    A pinned tag with a stale hash is a page that loads nothing, and the way
    that gets discovered is somebody opening the link.
    """
    with urllib.request.urlopen(CDN, timeout=30) as response:
        digest = hashlib.sha384(response.read()).digest()
    expected = f"sha384-{base64.b64encode(digest).decode()}"

    assert expected in PAGE, f"hash does not match {CDN}"


def test_the_published_page_does_not_offer_a_request_it_cannot_make() -> None:
    """There is no hosted API behind the published reference.

    Scalar defaults its server to the relative entry in `servers`, which on a
    static site resolves to the documentation host — so pressing Send returned
    GitHub's own 404. Ordering localhost first was not enough, and the right
    port would not have helped either: a browser refuses the cross-origin
    request to localhost, since the application ships no CORS middleware.

    Verified in a browser against the published page, which is the only place
    it can be: both entry points — "Test Request" and "Open API Client" —
    are gone, and the client's own Send has no layout box. The same document
    served by the running app at `/scalar` keeps all of it.
    """
    configuration = json.loads(
        re.search(r"data-configuration='([^']+)'", PAGE).group(1)  # type: ignore[union-attr]
    )

    assert configuration["hideTestRequestButton"] is True
    assert configuration["hideClientButton"] is True
    assert "localhost:58000/scalar" in PAGE, "say where the buttons do work"


@pytest.mark.skipif(
    os.getenv("CI") is None and os.getenv("CHECK_SRI") is None,
    reason="reaches the CDN; runs in CI or with CHECK_SRI=1",
)
def test_every_configuration_key_is_one_the_pinned_scalar_reads() -> None:
    """A misspelled option is not an error, it is a no-op.

    `hideTestRequestButton` was set and the button stayed, because it gates the
    operation panels and not the API client — found by opening the published
    page rather than by reading the config back. A renamed option across a
    version bump fails exactly as quietly, so the names are checked against the
    bundle that will actually run.
    """
    configuration = json.loads(
        re.search(r"data-configuration='([^']+)'", PAGE).group(1)  # type: ignore[union-attr]
    )
    with urllib.request.urlopen(CDN, timeout=30) as response:
        bundle = response.read().decode("utf-8", "replace")

    unknown = [key for key in configuration if f"{key}:" not in bundle]

    assert not unknown, f"scalar {SCALAR_VERSION} does not read: {unknown}"
