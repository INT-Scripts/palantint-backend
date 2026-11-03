"""Track the source of automatically imported geographical locations."""

from alembic import op

revision = "f1c02d3a4b56"
down_revision = "e4a71c8f2b90"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE locations ADD COLUMN IF NOT EXISTS source_id uuid REFERENCES data_sources(id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_locations_source_id ON locations (source_id)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_locations_source_id")
    op.execute("ALTER TABLE locations DROP COLUMN IF EXISTS source_id")
