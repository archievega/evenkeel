"""The scaffolder must emit code this repository would accept.

A generator that produces code failing the project's own lint is worse than no
generator: it hands whoever ran it a pile of errors they did not write. And
unlike documentation, it rots invisibly — a convention changes, the templates do
not, and nobody notices until the next vertical.

So the generator is run for real here and its output is linted with the same
ruff configuration as the rest of the tree.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from new_vertical import camel, scaffold, singular

AREA = "orders"


@pytest.fixture
def generated(tmp_path: Path) -> Path:
    """A whole package tree in a temp directory, never the real one."""
    package = tmp_path / "src" / "evenkeel"
    scaffold(AREA, root=package, tests=tmp_path / "tests")
    return tmp_path


def test_it_writes_every_file_the_layer_map_promises(generated: Path) -> None:
    """`AGENTS.md` says an endpoint touches these four places plus a test. If
    the generator and that table disagree, one of them is lying."""
    package = generated / "src" / "evenkeel"

    assert (package / "application" / "interactors" / AREA / "__init__.py").exists()
    assert (package / "application" / "interactors" / AREA / "get_order.py").exists()
    assert (package / "presentation" / "http" / "schemas" / "v1" / f"{AREA}.py").exists()
    assert (package / "presentation" / "http" / "routers" / "v1" / f"{AREA}.py").exists()
    assert (generated / "tests" / "http" / f"test_{AREA}_api.py").exists()


def test_the_generated_code_passes_this_project_s_lint(generated: Path) -> None:
    result = subprocess.run(
        ["uv", "run", "ruff", "check", "--no-cache", str(generated)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_generated_code_is_already_formatted(generated: Path) -> None:
    """Otherwise the first thing `make check` says to whoever ran the generator
    is that their brand-new files are badly formatted."""
    result = subprocess.run(
        ["uv", "run", "ruff", "format", "--check", "--no-cache", str(generated)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_interactor_carries_the_conventions_that_are_easy_to_forget(
    generated: Path,
) -> None:
    """The three an interactor is reviewed for: a frozen command, ownership in
    the query, and a metric that is recorded even when the use case refuses."""
    source = (
        generated
        / "src"
        / "evenkeel"
        / "application"
        / "interactors"
        / AREA
        / "get_order.py"
    ).read_text()

    assert "@dataclass(frozen=True, slots=True)" in source
    assert "owner_id: OwnerId" in source
    assert "finally:" in source
    assert "observe_interactor" in source


def test_every_symbol_the_templates_import_actually_exists(generated: Path) -> None:
    """The failure ruff cannot see.

    The first version of the router template imported `CurrentOwner`, which is
    not what that dependency is called — valid-looking, lint-clean, and broken
    at import time. Templates reference the real codebase, so they rot the
    moment something is renamed; this resolves each import against the live
    modules.
    """
    import importlib

    package = generated / "src" / "evenkeel"
    imports = re.findall(
        r"^from (evenkeel\.[\w.]+) import \(?([^)\n]+)\)?",
        "\n".join(p.read_text() for p in package.rglob("*.py")),
        re.MULTILINE,
    )

    for module_name, names in imports:
        if f".{AREA}" in module_name:
            continue  # generated, and not on the import path
        module = importlib.import_module(module_name)
        for name in (n.strip() for n in names.split(",")):
            if name:
                assert hasattr(module, name), f"{module_name} has no {name}"


def test_the_generated_layer_never_imports_downwards(generated: Path) -> None:
    """The one mistake the scaffolder exists to prevent. `import-linter` would
    catch it in the real tree, but only after the author had written it."""
    interactors = (generated / "src" / "evenkeel" / "application" / "interactors").rglob(
        "*.py"
    )

    for module in interactors:
        source = module.read_text()
        for forbidden in ("infrastructure", "presentation", "sqlalchemy", "fastapi"):
            assert forbidden not in source, f"{module.name} imports {forbidden}"


def test_it_refuses_to_overwrite_by_default(
    generated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = generated / "src" / "evenkeel"
    marker = package / "application" / "interactors" / AREA / "get_order.py"
    marker.write_text("# written by a human\n")

    scaffold(AREA, root=package, tests=generated / "tests")

    assert marker.read_text() == "# written by a human\n"
    assert "skip" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("area", "expected"),
    [("orders", "order"), ("payments", "payment"), ("audit", "audit")],
)
def test_names_are_singularised_the_way_the_templates_assume(
    area: str, expected: str
) -> None:
    assert singular(area) == expected
    assert camel(singular(area)) == expected.capitalize()
