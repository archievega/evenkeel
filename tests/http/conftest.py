from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from evenkeel.domain.value_objects.ids import OwnerId
from evenkeel.setup.app_factory import create_app
from evenkeel.setup.config import Settings
from tests.fakes.container import build_container
from tests.fakes.repositories import FakeLedgerRepository, FakeWalletRepository
from tests.fakes.system import ScriptedRiskAssessment


@pytest.fixture
def wallets() -> FakeWalletRepository:
    return FakeWalletRepository()


@pytest.fixture
def ledger() -> FakeLedgerRepository:
    return FakeLedgerRepository()


@pytest.fixture
def risk() -> ScriptedRiskAssessment:
    """Allows everything unless a test replaces the decision."""
    return ScriptedRiskAssessment()


@pytest.fixture
def owner_id() -> OwnerId:
    return OwnerId(uuid4())


@pytest.fixture
async def client(
    wallets: FakeWalletRepository,
    ledger: FakeLedgerRepository,
    risk: ScriptedRiskAssessment,
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        settings=Settings(), container=build_container(wallets, ledger, risk)
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http_client,
    ):
        yield http_client
