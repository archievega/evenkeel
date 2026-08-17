"""The dashboard and the alerts must reference metrics this code emits.

A dashboard is the artefact that rots most quietly. Renaming a metric leaves
every panel syntactically valid and permanently empty, and an empty panel looks
exactly like an idle service — so the graph nobody looks at during an incident
is the one that stopped working three months earlier.

So the queries are parsed here and checked against the exposition the
`PrometheusMetrics` adapter actually produces, the same text Prometheus scrapes.

The last test guards a mistake made while writing the scrape config rather than
the code: a `service: evenkeel` label on the target seemed like harmless
decoration and collided with the application's own `service` label, which names
the *dependency* being called. Prometheus keeps the target's value and renames
the real one to `exported_service`, so every query grouping by `service`
silently grouped by a constant — one line labelled "evenkeel" however many
providers there were. It renders as a working dashboard.
"""

import json
import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")
prometheus_client = pytest.importorskip(
    "prometheus_client", reason="requires the `metrics` extra"
)

from evenkeel.infrastructure.adapters.prometheus.metrics import (  # noqa: E402
    PrometheusMetrics,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "ops" / "grafana" / "dashboards" / "evenkeel.json"
ALERTS = ROOT / "ops" / "prometheus" / "alerts.yml"
SCRAPE = ROOT / "ops" / "prometheus" / "prometheus.yml"


# Everything the port can record, exercised once so that every metric family and
# every label appears in the exposition. A metric with no observations is absent
# from `/metrics` entirely, which would make this test vacuous.
def exposed() -> tuple[set[str], dict[str, set[str]]]:
    metrics = PrometheusMetrics()
    metrics.request_started()
    metrics.request_finished(
        method="GET", handler="/v1/wallets", status_code=200, duration_seconds=0.01
    )
    metrics.request_started()
    metrics.request_failed(
        method="GET", handler="/v1/wallets", exception_type="X", duration_seconds=0.01
    )
    metrics.observe_interactor(name="move_money", outcome="success", duration_seconds=0.1)
    metrics.observe_external_call(
        service="risk", operation="assess", outcome="success", duration_seconds=0.1
    )
    metrics.observe_lock(name="wallet_movement", outcome="acquired", wait_seconds=0.01)
    metrics.observe_rate_limit(policy="withdraw_from_wallet", decision="allowed")

    names: set[str] = set()
    labels: dict[str, set[str]] = {}
    text = prometheus_client.generate_latest(metrics.registry).decode()
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        sample = line.split()[0]
        name, _, rest = sample.partition("{")
        names.add(name)
        labels.setdefault(name, set()).update(re.findall(r"(\w+)=", rest))
    return names, labels


def referenced_metrics(expr: str) -> set[str]:
    """Metric names in a PromQL expression.

    Every bare identifier that is not a function or a keyword. Crude on purpose,
    and the crudeness has to lean towards over-collecting: an under-collecting
    version let four separate mistakes through, including a misspelled
    `evenkel_` prefix — the single likeliest rename slip, and invisible to a
    filter that only looked at names starting with `evenkeel_`.
    """
    keywords = {
        "sum",
        "rate",
        "by",
        "and",
        "or",
        "unless",
        "histogram_quantile",
        "le",
        "avg",
        "max",
        "min",
        "count",
        "increase",
        "absent",
        "on",
        "ignoring",
        "group_left",
        "group_right",
        "without",
        "topk",
        "bottomk",
        "up",
        "job",
    }
    # Quoted strings first: a label *value* like `outcome="bulkhead_full"` is an
    # identifier by shape and not a metric name, and over-collecting has to stop
    # somewhere sensible.
    bare = re.sub(r'"[^"]*"', '""', expr)
    found = set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b(?!\s*\()", bare))
    return {n for n in found if n not in keywords and not n.isdigit()}


def dashboard_exprs() -> list[tuple[str, str]]:
    """Every query in the file, including the ones inside collapsed rows.

    A collapsed row nests its panels under its own `panels` key rather than at
    the top level, so iterating `dashboard["panels"]` alone examined none of
    them — collapse a row and its queries stop being checked.
    """

    def walk(panels: list[dict[str, object]]) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for panel in panels:
            for target in panel.get("targets", []) or []:  # type: ignore[union-attr]
                found.append((str(panel.get("title", "?")), target["expr"]))
            found.extend(walk(panel.get("panels", []) or []))  # type: ignore[arg-type]
        return found

    return walk(json.loads(DASHBOARD.read_text())["panels"])


def alert_rules() -> list[tuple[str, str]]:
    rules = yaml.safe_load(ALERTS.read_text())
    return [
        (rule["alert"], rule["expr"])
        for group in rules["groups"]
        for rule in group["rules"]
    ]


def unknown_metrics(expr: str) -> set[str]:
    """Referenced names that the exposition does not contain.

    Matched against the exact sample names Prometheus scrapes, `_bucket` and
    all. The previous version stripped `_bucket` from both sides before
    comparing, which made `evenkeel_http_requests_total_bucket` — a histogram
    suffix on a counter, and a permanently empty panel — match the counter and
    pass.
    """
    names, labels = exposed()
    known = names | set(labels)
    return {n for n in referenced_metrics(expr) if n not in known and "_" in n}


@pytest.mark.parametrize(("title", "expr"), dashboard_exprs())
def test_every_panel_queries_a_metric_that_exists(title: str, expr: str) -> None:
    assert not (missing := unknown_metrics(expr)), (
        f"panel {title!r} queries metrics nothing emits: {missing}"
    )


@pytest.mark.parametrize(("title", "expr"), dashboard_exprs())
def test_every_panel_groups_by_a_label_that_exists(title: str, expr: str) -> None:
    """`sum by (handlr)` is valid PromQL and an empty panel."""
    _, labels = exposed()
    emitted = {label for names in labels.values() for label in names} | {"le"}

    grouped = {
        label.strip()
        for match in re.findall(r"by\s*\(([^)]*)\)", expr)
        for label in match.split(",")
    }

    assert grouped <= emitted, f"panel {title!r} groups by {grouped - emitted}"


@pytest.mark.parametrize(("name", "expr"), alert_rules())
def test_every_alert_watches_a_metric_that_exists(name: str, expr: str) -> None:
    assert not (missing := unknown_metrics(expr)), (
        f"alert {name!r} watches metrics nothing emits: {missing}"
    )


def test_the_job_the_alerts_name_is_the_job_the_scrape_config_defines() -> None:
    """`up{job="evenkeel"} == 0` is the rule everything else depends on: when it
    is wrong it does not fail, it simply never fires."""
    scrape = yaml.safe_load(SCRAPE.read_text())
    jobs = {c["job_name"] for c in scrape["scrape_configs"]}

    # Not `\w+`: a job name may contain a hyphen, and with that pattern a
    # renamed job matched nothing, leaving an empty set that trivially passed.
    named = set(re.findall(r'job="([^"]+)"', ALERTS.read_text()))

    assert named <= jobs, f"alerts reference undefined jobs: {named - jobs}"


def test_no_target_label_collides_with_a_label_the_application_emits() -> None:
    """Prometheus resolves a collision in the target's favour and renames the
    application's to `exported_<name>`. Nothing errors, nothing is logged at the
    query layer, and every `by (service)` becomes `by (constant)`."""
    _, labels = exposed()
    emitted = {label for names in labels.values() for label in names}

    scrape = yaml.safe_load(SCRAPE.read_text())
    configured = {
        label
        for config in scrape["scrape_configs"]
        for static in config.get("static_configs", [])
        for label in (static.get("labels") or {})
    }

    assert not configured & emitted, (
        f"scrape labels shadow application labels: {sorted(configured & emitted)}"
    )


def test_the_latency_buckets_sit_on_the_timeouts_that_shape_them() -> None:
    """A histogram interpolates linearly inside a bucket, so a bucket wider than
    the behaviour it holds reports a number nothing measured.

    That shipped. The boundaries went `0.5, 1.0, 2.5`, the entire slow tail of a
    degraded-provider run landed in the one bucket a second and a half wide, and
    the panel reported a p99 above the slowest request the load generator
    measured on the same run — an estimate above every request that happened, on
    a panel that looked perfectly reasonable.

    Slow requests pile up just under one guard or the other, so the boundaries
    belong on the guards. This fails if someone retunes the transport and leaves
    the buckets where they were.
    """
    from evenkeel.infrastructure.adapters.prometheus.metrics import _REQUEST_BUCKETS
    from evenkeel.setup.config import OutboundHttpConfig

    # `OutboundHttpConfig`, not `TransportPolicy`: the composition root builds
    # the policy from the config, so pinning the dataclass defaults left an
    # operator free to retune the real numbers with the buckets still where they
    # were, and this test green.
    config = OutboundHttpConfig()

    for name, ms in (
        ("per-attempt timeout", config.total_timeout_ms),
        ("overall budget", config.budget_ms),
    ):
        assert ms / 1000 in _REQUEST_BUCKETS, (
            f"no bucket boundary at the {name} ({ms}ms), so every request that "
            f"stops there is estimated by interpolation across the gap"
        )
