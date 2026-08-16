"""Every adapter must be named in `conformance.py`, and this is what says so.

`conformance.py` is the file that turns "the DI container binds this class to
that port" into something `mypy` can check. It only works for adapters somebody
remembered to add — and three written in a single afternoon were all missing
from it, which is what a convention with no enforcement looks like after a busy
day.

So the convention is now a test. It matters more than it looks: this is the kind
of rule an agent editing the repository violates by default, because nothing in
the diff it is writing mentions the file it forgot.

Only abstract ports are enumerated. `__subclasses__()` is exact for those — no
name matching, no heuristics about what counts as an adapter. Ports declared as
`Protocol` are structural and have no subclass list; those are covered by the
conformance functions alone, which is a gap this test cannot close and does not
pretend to.
"""

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import evenkeel.infrastructure.adapters as adapters_package
from evenkeel.application.ports import (
    BulkheadPort,
    IdempotencyStore,
    MetricsPort,
    RiskAssessmentPort,
)
from evenkeel.application.ports.identity import IdentityProvider

ABSTRACT_PORTS = (
    BulkheadPort,
    IdempotencyStore,
    MetricsPort,
    RiskAssessmentPort,
    IdentityProvider,
)

CONFORMANCE = (
    Path(adapters_package.__file__).parent / "conformance.py"  # type: ignore[arg-type]
)


def _import_every_adapter() -> None:
    """`__subclasses__()` only knows about classes that have been imported.

    Adapters behind an extra are imported lazily by their provider, so without
    this walk the test would quietly check a subset and pass.
    """
    for module in pkgutil.walk_packages(
        adapters_package.__path__, prefix=f"{adapters_package.__name__}."
    ):
        importlib.import_module(module.name)


def _asserted_adapters() -> set[str]:
    """Names that appear as the parameter of an assertion function.

    Parsed, not searched for. The first version of this test did a substring
    match over the file and passed while the entry it was looking for had been
    deleted — the import at the top and a mention in the docstring were enough
    to satisfy it. A test that cannot fail is worse than no test, because it
    also stops anyone from writing one.
    """
    module = ast.parse(CONFORMANCE.read_text())
    names: set[str] = set()
    for node in module.body:
        if not isinstance(node, ast.FunctionDef) or not node.args.args:
            continue
        annotation = node.args.args[0].annotation
        if isinstance(annotation, ast.Name):
            names.add(annotation.id)
        elif isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            # A forward reference, used for adapters behind an extra.
            names.add(annotation.value)
    return names


def _implementations(port: type) -> set[type]:
    found: set[type] = set()
    for subclass in port.__subclasses__():
        found |= _implementations(subclass)
        if not inspect.isabstract(subclass) and not subclass.__name__.startswith("_"):
            found.add(subclass)
    return found


def test_every_adapter_of_an_abstract_port_is_conformance_checked() -> None:
    _import_every_adapter()
    registered = _asserted_adapters()

    missing = {
        f"{adapter.__module__}.{adapter.__name__}"
        for port in ABSTRACT_PORTS
        for adapter in _implementations(port)
        if adapter.__module__.startswith("evenkeel.")
        and adapter.__name__ not in registered
    }

    assert not missing, (
        "not named in infrastructure/adapters/conformance.py: "
        + ", ".join(sorted(missing))
        + ". Add `def _assert_x(adapter: TheAdapter) -> ThePort: return adapter`, "
        "which is what makes mypy check the binding the DI container does at "
        "runtime."
    )


def test_the_check_itself_would_notice_an_absence() -> None:
    """A test asserting "nothing is missing" passes just as happily when it is
    looking in the wrong place."""
    _import_every_adapter()

    assert _implementations(MetricsPort), "no adapters discovered at all"
    assert "SqlaWalletRepository" in _asserted_adapters(), "nothing parsed"


def test_an_import_alone_does_not_satisfy_the_check() -> None:
    """The bug the first version of this file had. `conformance.py` imports
    every adapter it asserts on, so a substring search over the source is
    satisfied by the import and never notices the missing assertion."""
    imported_only = ast.parse(
        "from evenkeel.x import Ghost\n\n"
        "def _assert_real(adapter: Real) -> Port:\n    return adapter\n"
    )
    names = {
        node.args.args[0].annotation.id  # type: ignore[union-attr]
        for node in imported_only.body
        if isinstance(node, ast.FunctionDef)
    }

    assert names == {"Real"}
