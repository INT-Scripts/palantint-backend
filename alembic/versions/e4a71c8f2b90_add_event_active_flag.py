"""Add an active flag so removed SI agenda events can be archived."""

from alembic import op


revision = "e4a71c8f2b90"
down_revision = "c8b9d2f3a4e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true")
    op.execute("CREATE INDEX IF NOT EXISTS ix_events_is_active ON events (is_active)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_event_attendees_person_id ON event_attendees (person_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_events_is_active")
    op.execute("DROP INDEX IF EXISTS ix_event_attendees_person_id")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS is_active")
