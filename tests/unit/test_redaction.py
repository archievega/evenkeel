import logging

import pytest
from pydantic import SecretStr

from evenkeel.logging import REDACTED, allowlisted, redact_sensitive, setup_logging
from evenkeel.setup.config import (
    DatabaseConfig,
    Settings,
    production_config_problems,
)


class TestDenylistForAuthoredKeys:
    @pytest.mark.cwe(532)
    def test_a_credential_is_stripped(self) -> None:
        assert redact_sensitive({"password": "hunter2"})["password"] == REDACTED

    @pytest.mark.cwe(532)
    def test_anything_ending_in_token_is_stripped(self) -> None:
        redacted = redact_sensitive({"refresh_token": "x", "device_token": "y"})

        assert redacted == {"refresh_token": REDACTED, "device_token": REDACTED}

    @pytest.mark.cwe(532)
    def test_nested_structures_are_walked(self) -> None:
        redacted = redact_sensitive({"outer": [{"api_key": "k", "page": 2}]})

        assert redacted == {"outer": [{"api_key": REDACTED, "page": 2}]}

    def test_ordinary_fields_survive(self) -> None:
        assert redact_sensitive({"wallet_id": "w-1"}) == {"wallet_id": "w-1"}

    def test_an_unanticipated_credential_name_leaks(self) -> None:
        """Why the denylist is not enough on its own.

        A provider that names its field `sso_assertion` is published verbatim,
        because a denylist can only strip what it was told to expect. This is
        the failure mode `allowlisted` exists to invert.
        """
        assert redact_sensitive({"sso_assertion": "secret"}) == {
            "sso_assertion": "secret"
        }


class TestAllowlistForForeignPayloads:
    def test_permitted_keys_keep_their_values(self) -> None:
        result = allowlisted({"loc": ["body"], "msg": "bad"}, allow={"loc", "msg"})

        assert result == {"loc": ["body"], "msg": "bad"}

    @pytest.mark.cwe(532)
    def test_everything_else_is_redacted(self) -> None:
        result = allowlisted({"msg": "bad", "input": "4111111111111111"}, allow={"msg"})

        assert result == {"msg": "bad", "input": REDACTED}

    @pytest.mark.cwe(532)
    def test_a_new_field_is_redacted_without_anyone_updating_a_list(self) -> None:
        """The property the polarity buys.

        A library adds a field in a minor release; the allowlist has never heard
        of it; it does not reach the log.
        """
        result = allowlisted({"msg": "bad", "ctx": {"secret": "s"}}, allow={"msg"})

        assert result["ctx"] == REDACTED

    def test_keys_are_preserved_so_the_shape_stays_visible(self) -> None:
        result = allowlisted({"a": 1, "b": 2}, allow=set())

        assert set(result) == {"a", "b"}

    def test_an_oversized_payload_is_truncated(self) -> None:
        result = allowlisted({str(i): i for i in range(30)}, allow=set(), max_items=5)

        assert len(result) == 6
        assert result["..."] == "25 more keys"

    def test_an_empty_payload_is_empty(self) -> None:
        assert allowlisted(None, allow={"loc"}) == {}


@pytest.mark.cwe(532)
def test_a_record_from_a_library_is_redacted_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Gap 7 in docs/SECURITY_CONTROLS.md.

    `basicConfig` formatted structlog's output and left every other logger's
    alone — uvicorn, SQLAlchemy, anything using `logging` — so those records
    never met the redaction processor at all. They go through
    `ProcessorFormatter` now, with the same chain.
    """
    setup_logging(level="INFO", json_logs=True)

    logging.getLogger("some.library").info("connecting with password=hunter2")

    output = capsys.readouterr().out
    assert "hunter2" not in output
    assert REDACTED in output


@pytest.mark.cwe(532)
def test_a_secret_inside_a_message_is_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """By the time a library renders its line the secret is a substring, which
    no amount of walking the event dict will reach."""
    setup_logging(level="INFO", json_logs=True)

    logging.getLogger("some.library").warning(
        "GET /x failed, Authorization: Bearer abc.def.ghi"
    )

    assert "abc.def.ghi" not in capsys.readouterr().out


@pytest.mark.cwe(532)
def test_the_boot_guard_refuses_sql_echo() -> None:
    """The one this cannot cover: SQLAlchemy prints bound parameters
    positionally, so there is no key to match and nothing to redact. The honest
    control is refusing the flag, not claiming a filter that cannot work."""
    settings = Settings(database=DatabaseConfig(echo=True, password=SecretStr("real")))
    settings.app.identity_provider = "jwt"
    settings.app.secret_key = SecretStr("a-real-secret")

    assert any(
        "database.echo" in problem for problem in production_config_problems(settings)
    )
