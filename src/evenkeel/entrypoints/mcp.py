"""Run the tool surface over stdio.

Separate from `web.py` on purpose: a process serving a model is not the same
deployment as a process serving the internet, and giving them one entrypoint
with a flag is how the wrong one ends up on the wrong port.

    evenkeel-mcp

The client — Claude Desktop, an IDE, an agent — starts this as a subprocess and
speaks MCP over stdin and stdout. Which is also why nothing here may print:
stdout is the protocol. Logs go to stderr, as they already do.
"""

import sys
from uuid import UUID

from evenkeel.application.ports import MetricsPort
from evenkeel.domain.value_objects.ids import OwnerId
from evenkeel.infrastructure.adapters.noop.metrics import NoopMetrics
from evenkeel.logging import setup_logging
from evenkeel.presentation.mcp.server import create_mcp_server
from evenkeel.setup.config import load_settings, production_config_problems
from evenkeel.setup.ioc.container import create_container


def main() -> int:
    settings = load_settings()

    problems = production_config_problems(settings)
    if problems and not settings.app.is_local:
        for problem in problems:
            print(f"FATAL: insecure configuration: {problem}", file=sys.stderr)
        return 1

    if not settings.mcp.owner_id:
        # Refused rather than defaulted. The owner is the entire authorisation
        # story of this transport: a server that picks one for you is a server
        # that will eventually pick somebody else's.
        print(
            "FATAL: app.mcp.owner_id is required — the MCP transport acts on "
            "behalf of exactly one owner, and it is not a tool argument",
            file=sys.stderr,
        )
        return 1

    try:
        owner = OwnerId(UUID(settings.mcp.owner_id))
    except ValueError:
        print("FATAL: app.mcp.owner_id must be a UUID", file=sys.stderr)
        return 1

    setup_logging(
        level=settings.app.logging.level,
        json_logs=settings.app.logging.json_logs,
        # Never pretty, never coloured, and never stdout. This process speaks
        # JSON-RPC over stdout, so a log line written there is handed to the
        # client as a malformed protocol message — which is exactly what the
        # first end-to-end run produced.
        pretty_console=False,
        include_timestamp=settings.app.logging.include_timestamp,
        stream=sys.stderr,
    )

    metrics: MetricsPort = NoopMetrics()
    container = create_container(settings, metrics)
    server = create_mcp_server(container, owner)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
