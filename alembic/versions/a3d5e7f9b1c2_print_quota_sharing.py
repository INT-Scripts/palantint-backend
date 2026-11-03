"""Share followme print quota between PalantINT users."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "a3d5e7f9b1c2"
down_revision = "f1c02d3a4b56"
branch_labels = None
depends_on = None


def _user_fk(name, nullable=False, ondelete="CASCADE", **kwargs):
    return sa.Column(name, UUID, sa.ForeignKey("users.id", ondelete=ondelete), nullable=nullable, **kwargs)


def upgrade():
    op.create_table(
        "print_share_settings",
        _user_fk("user_id", primary_key=True),
        sa.Column("is_sharing", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_paused", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("pool_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("auto_approve", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("global_cap", sa.Integer),
        sa.Column("pool_per_person_cap", sa.Integer),
        sa.Column("min_balance_floor", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_balance", sa.Integer),
        sa.Column("balance_checked_at", sa.DateTime),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_table(
        "print_grants",
        sa.Column("id", sa.Uuid, primary_key=True),
        _user_fk("donor_user_id", index=True),
        _user_fk("beneficiary_user_id", index=True),
        sa.Column("amount", sa.Integer, nullable=False),
        sa.Column("expires_at", sa.DateTime),
        sa.Column("revoked_at", sa.DateTime, index=True),
        _user_fk("revoked_by_user_id", nullable=True, ondelete="SET NULL"),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index(
        "uq_print_grant_active", "print_grants", ["donor_user_id", "beneficiary_user_id"],
        unique=True, postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "print_requests",
        sa.Column("id", sa.Uuid, primary_key=True),
        _user_fk("requester_user_id", index=True),
        _user_fk("donor_user_id", index=True),
        sa.Column("grant_id", UUID, sa.ForeignKey("print_grants.id", ondelete="SET NULL"), index=True),
        sa.Column("status", sa.String, nullable=False, index=True),
        sa.Column("original_filename", sa.String, nullable=False),
        sa.Column("queued_name", sa.String, nullable=False, unique=True),
        sa.Column("page_count", sa.Integer, nullable=False),
        sa.Column("page_range", sa.String),
        sa.Column("copies", sa.Integer, nullable=False, server_default="1"),
        sa.Column("color", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("options", sa.JSON),
        sa.Column("printer_name", sa.String),
        sa.Column("estimated_cost", sa.Integer, nullable=False),
        sa.Column("actual_cost", sa.Integer),
        sa.Column("temp_path", sa.String),
        sa.Column("error", sa.Text),
        sa.Column("approval_expires_at", sa.DateTime),
        sa.Column("hold_expires_at", sa.DateTime),
        sa.Column("expiry_warned", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("released_at", sa.DateTime),
    )


def downgrade():
    op.drop_table("print_requests")
    op.drop_index("uq_print_grant_active", table_name="print_grants")
    op.drop_table("print_grants")
    op.drop_table("print_share_settings")
