"""add cas columns back

Revision ID: c8b9d2f3a4e6
Revises: b7c2d9e1f3a5
Create Date: 2026-07-25 14:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8b9d2f3a4e6'
down_revision: Union[str, Sequence[str], None] = 'b7c2d9e1f3a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    from sqlalchemy.engine.reflection import Inspector
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    columns = [col['name'] for col in inspector.get_columns('users')]
    
    if 'cas_username' not in columns:
        op.add_column('users', sa.Column('cas_username', sa.String(), nullable=True))
    if 'cas_password' not in columns:
        op.add_column('users', sa.Column('cas_password', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'cas_password')
    op.drop_column('users', 'cas_username')
