"""add_prelaunch_signup_table

Revision ID: 41502fcb74c7
Revises: b2c3d4e5f6g7
Create Date: 2025-11-24 22:58:12.059168

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '41502fcb74c7'
down_revision = 'b2c3d4e5f6g7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'PreLaunchSignup',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('ip_address', sa.String(), nullable=True),
        sa.Column('user_agent', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_PreLaunchSignup_id'), 'PreLaunchSignup', ['id'], unique=False)
    op.create_index(op.f('ix_PreLaunchSignup_email'), 'PreLaunchSignup', ['email'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_PreLaunchSignup_email'), table_name='PreLaunchSignup')
    op.drop_index(op.f('ix_PreLaunchSignup_id'), table_name='PreLaunchSignup')
    op.drop_table('PreLaunchSignup')
