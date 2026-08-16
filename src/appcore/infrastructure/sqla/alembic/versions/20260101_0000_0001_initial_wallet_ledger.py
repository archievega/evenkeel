"""initial wallet ledger

Revision ID: 0001
Revises:
Create Date: 2026-01-01 00:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wallets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("balance_amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # The application refuses to overdraw, and so does the database. An
        # invariant enforced in only one of the two is an invariant that a
        # background job, a migration or a psql session can violate.
        sa.CheckConstraint("balance_amount >= 0", name="ck_wallets_balance_non_negative"),
        sa.PrimaryKeyConstraint("id", name="pk_wallets"),
    )
    # Composite (owner_id, id): every wallet listing is scoped by owner and
    # ordered by id, so one index serves both the filter and the cursor.
    op.create_index("ix_wallets_owner_id_id", "wallets", ["owner_id", "id"])

    op.create_table(
        "ledger_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("balance_after", sa.Numeric(20, 2), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_ledger_entries_amount_positive"),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["wallets.id"],
            name="fk_ledger_entries_wallet_id_wallets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_entries"),
    )
    op.create_index(
        "ix_ledger_entries_wallet_id_id", "ledger_entries", ["wallet_id", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_ledger_entries_wallet_id_id", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_index("ix_wallets_owner_id_id", table_name="wallets")
    op.drop_table("wallets")
