"""remove_cas_credentials

Revision ID: a8f9c1e2b4d5
Revises: dfe953f88e66
Create Date: 2026-05-28 11:42:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a8f9c1e2b4d5'
down_revision: Union[str, Sequence[str], None] = 'dfe953f88e66'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('users', 'cas_username')
    op.drop_column('users', 'encrypted_cas_password')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('users', sa.Column('cas_username', sa.VARCHAR(), autoincrement=False, nullable=True))
    op.add_column('users', sa.Column('encrypted_cas_password', sa.VARCHAR(), autoincrement=False, nullable=True))
