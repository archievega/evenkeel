from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID

from evenkeel.infrastructure.sqla.types import TzAwareDateTime

# Explicit naming convention: without it Alembic emits unnamed constraints, and
# a later migration cannot drop what it cannot name. Set this on day one --
# retrofitting it means rewriting constraints on a live database.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

wallets_table = Table(
    "wallets",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("owner_id", UUID(as_uuid=True), nullable=False),
    Column("balance_amount", Numeric(20, 2), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("status", String(16), nullable=False),
    Column("version", Integer, nullable=False, server_default="0"),
    Column("created_at", TzAwareDateTime, nullable=False),
    Column("updated_at", TzAwareDateTime, nullable=False),
    CheckConstraint("balance_amount >= 0", name="balance_non_negative"),
    Index("ix_wallets_owner_id_id", "owner_id", "id"),
)

ledger_entries_table = Table(
    "ledger_entries",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "wallet_id",
        UUID(as_uuid=True),
        ForeignKey("wallets.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("direction", String(8), nullable=False),
    Column("amount", Numeric(20, 2), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("balance_after", Numeric(20, 2), nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("occurred_at", TzAwareDateTime, nullable=False),
    CheckConstraint("amount > 0", name="amount_positive"),
    Index("ix_ledger_entries_wallet_id_id", "wallet_id", "id"),
)
