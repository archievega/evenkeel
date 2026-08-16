"""The boot guard, gap 5 in docs/SECURITY_CONTROLS.md.

`production_config_problems` is the reason a misconfigured deployment stops
instead of serving. Until now it was a function nothing exercised — the class of
control that is most tempting to weaken during an incident precisely because
nothing fails when you do.
"""

import pytest
from pydantic import SecretStr

from evenkeel.setup.config import (
    AppConfig,
    DatabaseConfig,
    Settings,
    production_config_problems,
)


def hardened() -> Settings:
    return Settings(
        app=AppConfig(
            identity_provider="jwt",
            secret_key=SecretStr("a-real-secret-from-the-vault"),
        ),
        database=DatabaseConfig(password=SecretStr("a-real-database-password")),
    )


def test_a_correctly_configured_deployment_has_no_problems() -> None:
    assert production_config_problems(hardened()) == []


@pytest.mark.cwe(1188)
def test_the_default_secret_is_refused() -> None:
    settings = hardened()
    settings.app.secret_key = SecretStr("change-me")

    problems = production_config_problems(settings)

    assert any("secret_key" in problem for problem in problems)


def test_an_empty_secret_is_refused() -> None:
    settings = hardened()
    settings.app.secret_key = SecretStr("")

    assert any(
        "secret_key" in problem for problem in production_config_problems(settings)
    )


def test_a_well_known_database_password_is_refused() -> None:
    """The compose default must not survive into production.

    It is the password every reader of this repository already knows.
    """
    settings = hardened()
    settings.database.password = SecretStr("evenkeel")

    problems = production_config_problems(settings)

    assert any("database.password" in problem for problem in problems)


@pytest.mark.cwe(1188)
def test_the_placeholder_identity_provider_is_refused() -> None:
    """The claim three documents used to make and the code did not.

    `DevIdentityProvider` authenticates whoever supplies an owner id. A
    deployment that satisfies every other check and still has it wired in has
    no authentication at all, so it belongs on the refusal list.
    """
    settings = hardened()
    settings.app.identity_provider = "dev"

    problems = production_config_problems(settings)

    assert any("identity_provider" in problem for problem in problems)


def test_every_problem_is_reported_not_just_the_first() -> None:
    """One boot, one complete list.

    Reporting problems one at a time turns a misconfiguration into several
    deploy cycles, which is exactly when someone starts disabling the check.
    """
    settings = Settings(
        app=AppConfig(identity_provider="dev", secret_key=SecretStr("change-me")),
        database=DatabaseConfig(password=SecretStr("postgres")),
    )

    problems = " | ".join(production_config_problems(settings))

    # Asserting the substrings rather than a count, so adding a sixth check
    # later does not fail a test whose subject is "all of them, at once".
    assert "identity_provider" in problems
    assert "secret_key" in problems
    assert "database.password" in problems


@pytest.mark.cwe(1188)
def test_the_default_settings_are_not_production_ready() -> None:
    """The out-of-the-box configuration must fail this check.

    A template whose defaults pass a production guard has a guard that means
    nothing.
    """
    assert production_config_problems(Settings()) != []
