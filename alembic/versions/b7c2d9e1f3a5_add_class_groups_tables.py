"""add_class_groups_tables

Revision ID: b7c2d9e1f3a5
Revises: a8f9c1e2b4d5
Create Date: 2026-05-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c2d9e1f3a5'
down_revision: Union[str, Sequence[str], None] = 'a8f9c1e2b4d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    from sqlalchemy.engine.reflection import Inspector
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    tables = inspector.get_table_names()

    if 'class_groups' not in tables:
        op.create_table(
            'class_groups',
            sa.Column('id', sa.UUID(), nullable=False),
            sa.Column('name', sa.VARCHAR(), nullable=False),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_class_groups_name'), 'class_groups', ['name'], unique=True)

    if 'student_class_groups' not in tables:
        op.create_table(
            'student_class_groups',
            sa.Column('student_id', sa.UUID(), nullable=False),
            sa.Column('class_group_id', sa.UUID(), nullable=False),
            sa.Column('role', sa.VARCHAR(), nullable=False),
            sa.ForeignKeyConstraint(['class_group_id'], ['class_groups.id'], ),
            sa.ForeignKeyConstraint(['student_id'], ['students.id'], ),
            sa.PrimaryKeyConstraint('student_id', 'class_group_id')
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('student_class_groups')
    op.drop_index(op.f('ix_class_groups_name'), table_name='class_groups')
    op.drop_table('class_groups')
