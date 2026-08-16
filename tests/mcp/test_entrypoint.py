"""What the MCP process refuses to do, and where it writes.

Both of these are the kind of thing that is obvious in a code review and
invisible at runtime until it matters: a server that quietly picks an owner acts
on somebody's money without being told whose, and a server that logs to stdout
corrupts the protocol it is speaking.
"""

import logging
import sys
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("mcp", reason="requires the `mcp` extra")

from evenkeel.entrypoints.mcp import main
from evenkeel.logging import setup_logging


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings read the environment, and a developer's shell must not decide
    what this test asserts."""
    monkeypatch.delenv("APP__MCP__OWNER_ID", raising=False)
    monkeypatch.setenv("APP__APP__ENVIRONMENT", "local")


def test_it_refuses_to_start_without_an_owner(capsys: pytest.CaptureFixture[str]) -> None:
    """The owner is the whole authorisation story of this transport. A default
    would be a decision nobody made about whose money the model can move."""
    assert main() == 1

    assert "owner_id is required" in capsys.readouterr().err


def test_it_refuses_an_owner_that_is_not_a_uuid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APP__MCP__OWNER_ID", "the-finance-team")

    assert main() == 1

    assert "must be a UUID" in capsys.readouterr().err


def test_it_refuses_an_insecure_production_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same guard `evenkeel-web` has. A transport that skips it is a way
    around it."""
    monkeypatch.setenv("APP__APP__ENVIRONMENT", "production")
    monkeypatch.setenv("APP__MCP__OWNER_ID", str(uuid4()))

    assert main() == 1

    assert "insecure configuration" in capsys.readouterr().err


def test_logging_can_be_kept_off_the_protocol_stream() -> None:
    """stdout carries JSON-RPC in this process.

    The first end-to-end run of `evenkeel-mcp` printed a pydantic validation
    error *at the client*, because "database engine disposed" arrived where a
    response was expected. `setup_logging` grew a `stream` for this.
    """
    setup_logging(stream=sys.stderr)

    streams = [
        handler.stream
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.StreamHandler)
    ]

    assert streams, "no stream handler was installed"
    assert all(stream is sys.stderr for stream in streams)


def teardown_module(module: Any) -> None:
    """Leave the root logger as the rest of the suite expects to find it."""
    setup_logging()
