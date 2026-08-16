"""The boot guard, gap 5 in docs/SECURITY_CONTROLS.md.

`production_config_problems` is the reason a misconfigured deployment stops
instead of serving. Until now it was a function nothing exercised — the class of
control that is most tempting to weaken during an incident precisely because
nothing fails when you do.
"""

from pydantic import SecretStr

from evenkeel.setup.config import (
    AppConfig,
    DatabaseConfig,
    Settings,
    production_config_problems,
)


def hardened() -> Settings:
    return Settings(
        app=AppConfig(debug=False, secret_key=SecretStr("a-real-secret-from-the-vault")),
        database=DatabaseConfig(password=SecretStr("a-real-database-password")),
    )


def test_a_correctly_configured_deployment_has_no_problems() -> None:
    assert production_config_problems(hardened()) == []


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


def test_debug_mode_is_refused() -> None:
    settings = hardened()
    settings.app.debug = True

    assert any("debug" in problem for problem in production_config_problems(settings))


def test_every_problem_is_reported_not_just_the_first() -> None:
    """One boot, one complete list.

    Reporting problems one at a time turns a misconfiguration into several
    deploy cycles, which is exactly when someone starts disabling the check.
    """
    settings = Settings(
        app=AppConfig(debug=True, secret_key=SecretStr("change-me")),
        database=DatabaseConfig(password=SecretStr("postgres")),
    )

    assert len(production_config_problems(settings)) == 3


def test_the_default_settings_are_not_production_ready() -> None:
    """The out-of-the-box configuration must fail this check.

    A template whose defaults pass a production guard has a guard that means
    nothing.
    """
    assert production_config_problems(Settings()) != []
