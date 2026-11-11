"""Places of the campus map bookmarked by each user."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "b4e6f8a0c2d3"
down_revision = "a3d5e7f9b1c2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "place_bookmarks",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), index=True),
        sa.Column("feature_id", sa.String, nullable=False),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("building_id", sa.String),
        sa.Column("level", sa.Float),
        sa.Column("location_id", sa.Uuid),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("user_id", "feature_id", name="uq_place_bookmark"),
    )


def downgrade():
    op.drop_table("place_bookmarks")
