"""The controls matrix and the tests must agree about what is proven.

`docs/SECURITY_CONTROLS.md` is a table of claims with a test named against each
one. That is only worth something while the names are real and the tests still
exist — and one of them already was not: the matrix cited
`test_docs_are_absent_outside_debug` long after it had been renamed, so the
receipt pointed at nothing and nobody noticed, which is the exact failure mode
the matrix exists to prevent.

So the matrix is parsed here and checked in both directions:

* every test it names exists, and carries the CWE the matrix assigns it;
* every `@pytest.mark.cwe` test appears in the matrix.

Both directions matter. The first catches a rename; the second catches a test
that quietly stops being cited when a row is rewritten.
"""

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
MATRIX = ROOT / "docs" / "SECURITY_CONTROLS.md"
TESTS = ROOT / "tests"


def matrix_claims() -> dict[str, str]:
    """Test name → the CWE the row assigns it."""
    claims: dict[str, str] = {}
    for line in MATRIX.read_text().splitlines():
        if not line.startswith("| ") or "---" in line or line.count("|") < 5:
            continue
        control, _enforced, proof, cwe = (c.strip() for c in line.split("|")[1:5])
        if control == "Control":
            continue
        ids = [a or b for a, b in re.findall(r"definitions/(\d+)|^(\d+)$", cwe)]
        if not ids:
            continue
        for name in re.findall(r"`(test_[a-z_0-9]+)`", proof):
            claims[name] = ids[0]
    return claims


def tagged_tests() -> dict[str, set[str]]:
    """Test name → the CWEs its markers declare."""
    tagged: dict[str, set[str]] = {}
    for path in TESTS.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                target = decorator.func
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "cwe"
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                ):
                    tagged.setdefault(node.name, set()).add(str(decorator.args[0].value))
    return tagged


def test_the_matrix_names_tests_that_exist() -> None:
    existing = {
        node.name
        for path in TESTS.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    missing = sorted(set(matrix_claims()) - existing)

    assert not missing, f"the matrix cites tests that do not exist: {missing}"


def test_every_cited_test_carries_its_cwe() -> None:
    tagged = tagged_tests()

    untagged = sorted(
        f"{name} (CWE-{cwe})"
        for name, cwe in matrix_claims().items()
        if cwe not in tagged.get(name, set())
    )

    assert not untagged, untagged


def test_every_tagged_test_is_cited_by_the_matrix() -> None:
    """A tag with no row is a control nobody wrote down."""
    claims = matrix_claims()

    orphans = sorted(name for name in tagged_tests() if name not in claims)

    assert not orphans, orphans


def test_the_threat_model_cites_nothing_the_matrix_does_not_carry() -> None:
    """The two documents are one argument split in half: the model says what is
    defended and why, the matrix says where and by which test. A CWE in the
    first with no row in the second is a defence nobody wrote down."""
    threat_model = (ROOT / "docs" / "THREAT_MODEL.md").read_text()
    matrix = MATRIX.read_text()

    cited = set(re.findall(r"CWE-(\d+)", threat_model))
    carried = {a or b for a, b in re.findall(r"definitions/(\d+)|\| (\d+) \|", matrix)}

    assert cited <= carried, sorted(cited - carried)


@pytest.mark.parametrize("cwe", sorted(set(matrix_claims().values())))
def test_each_cwe_in_the_matrix_has_at_least_one_receipt(cwe: str) -> None:
    tagged = tagged_tests()

    assert any(cwe in cwes for cwes in tagged.values()), f"CWE-{cwe} has no tagged test"
