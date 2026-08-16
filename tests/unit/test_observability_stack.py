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

    Deliberately crude — every bare identifier that is not a function or a
    keyword. Over-collecting would make the test fail on something real; the
    risk is under-collecting, so the exclusion list stays short.
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
    found = set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b(?!\s*\()", expr))
    return {n for n in found if n not in keywords and not n.isdigit()}


def dashboard_exprs() -> list[tuple[str, str]]:
    dashboard = json.loads(DASHBOARD.read_text())
    return [
        (panel["title"], target["expr"])
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    ]


def alert_rules() -> list[tuple[str, str]]:
    rules = yaml.safe_load(ALERTS.read_text())
    return [
        (rule["alert"], rule["expr"])
        for group in rules["groups"]
        for rule in group["rules"]
    ]


@pytest.mark.parametrize(("title", "expr"), dashboard_exprs())
def test_every_panel_queries_a_metric_that_exists(title: str, expr: str) -> None:
    names, _ = exposed()
    referenced = referenced_metrics(expr)

    missing = {
        n
        for n in referenced
        if n.startswith("evenkeel_")
        and n not in names
        and n.removesuffix("_bucket") not in {m.removesuffix("_bucket") for m in names}
    }

    assert not missing, f"panel {title!r} queries metrics nothing emits: {missing}"


@pytest.mark.parametrize(("name", "expr"), alert_rules())
def test_every_alert_watches_a_metric_that_exists(name: str, expr: str) -> None:
    names, _ = exposed()
    referenced = referenced_metrics(expr)

    missing = {
        n
        for n in referenced
        if n.startswith("evenkeel_")
        and n not in names
        and n.removesuffix("_bucket") not in {m.removesuffix("_bucket") for m in names}
    }

    assert not missing, f"alert {name!r} watches metrics nothing emits: {missing}"


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
