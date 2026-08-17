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
    IdentityConfig,
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
        identity=IdentityConfig(
            issuer="https://issuer.example",
            audience="evenkeel",
            jwks_url="https://issuer.example/.well-known/jwks.json",
        ),
    )


def test_a_correctly_configured_deployment_has_no_problems() -> None:
    assert production_config_problems(hardened()) == []


@pytest.mark.cwe(1188)
@pytest.mark.parametrize("field", ["issuer", "audience", "jwks_url"])
def test_a_token_verifier_missing_its_own_identity_is_refused(field: str) -> None:
    """A verifier with no issuer or audience does not fail closed — it fails
    *open*, for anything the wrong issuer signed. Caught at boot rather than on
    the first request, because the first request may well be the attack."""
    settings = hardened()
    setattr(settings.identity, field, "")

    problems = production_config_problems(settings)

    assert any(field in problem for problem in problems)


@pytest.mark.cwe(1188)
@pytest.mark.parametrize(
    "url",
    [
        "http://issuer.example/.well-known/jwks.json",
        "issuer.example/.well-known/jwks.json",
    ],
)
def test_a_key_set_url_that_is_not_https_is_refused(url: str) -> None:
    """Over http the signing keys are whatever the network says they are, and a
    scheme-less URL is a permanent 503 discovered on the first request."""
    settings = hardened()
    settings.identity.jwks_url = url

    problems = production_config_problems(settings)

    assert any("https" in problem for problem in problems)


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


@pytest.mark.cwe(1188)
@pytest.mark.parametrize("value", ["JWT", "Jwt", "jwks", "oidc", ""])
def test_an_identity_provider_that_is_not_a_known_one_is_refused(value: str) -> None:
    """The selection used to be `!= "jwt"` means dev.

    So `IDENTITY_PROVIDER=JWT` — the capital-letter version of the edit
    `.env.example` tells an operator to make — silently chose the adapter that
    treats the bearer token as the owner id, in production, with the boot guard
    saying nothing. Every caller was whatever UUID they sent.
    """
    settings = hardened()
    settings.app.identity_provider = value

    problems = production_config_problems(settings)

    assert any("identity_provider" in problem for problem in problems)
