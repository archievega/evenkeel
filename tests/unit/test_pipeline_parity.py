"""The two pipelines must run the same checks.

`.gitlab-ci.yml` opens by calling itself a mirror of the GitHub workflow. It was
not one: no schema-drift check, no secret scan, no image scan, no smoke boot, no
contract diff, and a `pip-audit --strict` that fails here by construction — so
the file's first line was its least accurate.

Nothing noticed, because nothing could. Two hand-maintained copies of a pipeline
drift the moment one of them is edited, and the drift is invisible unless
somebody diffs two YAML dialects by eye.

This is that diff, as a test. It matches on the command each check runs rather
than on job names, since the two systems group jobs differently and neither
grouping is wrong.
"""

import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _commands(path: pathlib.Path) -> str:
    """The pipeline with its comment lines removed.

    The first version matched against the whole file and failed on a header
    comment that *describes* the bad invocation it forbids. Same mistake as the
    conformance check made the same day: a prose mention satisfying a search for
    a command. Prose is not a pipeline step.
    """
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


GITHUB = _commands(ROOT / ".github" / "workflows" / "ci.yml")
GITLAB = _commands(ROOT / ".gitlab-ci.yml")
GITLAB_SOURCE = (ROOT / ".gitlab-ci.yml").read_text()

# The check, and the string that proves each pipeline runs it. Both sides
# invoke the Makefile where a target exists, which is why most markers are
# identical — that is the point of routing them through it.
CHECKS = {
    "format and lint": ("make lint", "make lint"),
    "layer contracts": ("make arch", "make arch"),
    "type check": ("make types", "make types"),
    "openapi drift": ("make schema-check", "make schema-check"),
    "tests with coverage gate": ("--cov-fail-under=80", "--cov-fail-under=80"),
    "integration tests reach a database": ("TEST_DATABASE_URL", "TEST_DATABASE_URL"),
    "migration round-trip": ("alembic downgrade base", "alembic downgrade base"),
    "migration drift": ("alembic check", "alembic check"),
    "dependency audit": ("pip-audit", "pip-audit"),
    "static analysis": ("bandit", "bandit"),
    "secret scan over full history": ("gitleaks", "gitleaks"),
    # The one pair that genuinely differs: GitHub builds through the action so
    # it gets layer caching and provenance, GitLab through the CLI in dind.
    "image build": ("docker/build-push-action", "docker build"),
    "image vulnerability scan": ("trivy", "trivy"),
    "the image actually boots": ("docker compose up", "docker compose up"),
    "breaking-change diff": ("oasdiff", "oasdiff"),
}


@pytest.mark.parametrize(("check", "markers"), sorted(CHECKS.items()))
def test_both_pipelines_run_the_check(check: str, markers: tuple[str, str]) -> None:
    github_marker, gitlab_marker = markers

    assert github_marker in GITHUB, f"GitHub does not run: {check}"
    assert gitlab_marker in GITLAB, f"GitLab does not run: {check}"


def test_pip_audit_is_not_invoked_in_the_form_that_always_fails() -> None:
    """`--strict` treats the editable install of this project as unresolvable,
    so the job fails on every run and the finding it would report never gets
    read. Documented in both pipelines; asserted here."""
    for pipeline, name in ((GITHUB, "GitHub"), (GITLAB, "GitLab")):
        assert "pip-audit --strict" not in pipeline, name
        assert "pip-audit --skip-editable" in pipeline, name


def test_the_mirror_claim_is_the_thing_under_test() -> None:
    """If the header ever stops claiming a mirror, this file should stop
    claiming to enforce one."""
    assert "Mirror of .github/workflows/ci.yml" in GITLAB_SOURCE


def test_the_smoke_jobs_probe_the_port_compose_publishes() -> None:
    """Moving the published port broke both smoke jobs and neither test noticed.

    `make check` does not run them — they need Docker — so the first sign was
    three red checks on `main`. The port lives in `compose.yml`; the two
    pipelines quote it as a literal, which is the kind of duplication that only
    a check like this one keeps honest.
    """
    published = re.search(r"API_PORT:-(\d+)\}:8000", (ROOT / "compose.yml").read_text())
    assert published, "compose.yml no longer publishes the api on a default port"
    port = published.group(1)

    for name, pipeline in (("github", GITHUB), ("gitlab", GITLAB)):
        probes = set(re.findall(r"(?:localhost|127\.0\.0\.1):(\d+)/ready", pipeline))
        assert probes, f"{name} no longer probes readiness"
        assert probes == {port}, f"{name} probes {probes}, compose publishes {port}"

    # The fourth place the number lives, and the one a reader copies a curl
    # from. It kept `8000` through the move and nothing noticed.
    spec = json.loads((ROOT / "openapi.json").read_text())
    local = [s["url"] for s in spec["servers"] if "localhost" in s["url"]]
    assert local == [f"http://localhost:{port}"], (
        f"openapi.json advertises {local}, compose publishes {port}"
    )
