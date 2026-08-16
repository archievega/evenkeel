from dishka import AsyncContainer, make_async_container
from dishka.integrations.fastapi import FastapiProvider

from appcore.application.ports import MetricsPort
from appcore.setup.config import Settings
from appcore.setup.ioc.providers.core import (
    ConfigProvider,
    DatabaseProvider,
    InfrastructureProvider,
)
from appcore.setup.ioc.providers.wallets import WalletProvider


def create_container(settings: Settings, metrics: MetricsPort) -> AsyncContainer:
    """Assemble the object graph.

    ``metrics`` arrives through the context rather than being constructed here
    because the HTTP middleware needs the same instance before any request
    scope exists, and two Prometheus registries would double-count.
    """
    return make_async_container(
        ConfigProvider(),
        DatabaseProvider(),
        InfrastructureProvider(),
        WalletProvider(),
        FastapiProvider(),
        context={Settings: settings, MetricsPort: metrics},
    )
