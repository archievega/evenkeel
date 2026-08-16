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
