"""Add file_storage_path column to Agreement table for persistent file storage

Revision ID: e0f1g2h3i4j5
Revises: d9e0f1g2h3i4
Create Date: 2026-01-03 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e0f1g2h3i4j5'
down_revision = 'd9e0f1g2h3i4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add file_storage_path column for persistent file storage
    # This enables proper re-extraction from original files instead of cached text
    # Path format: /storage/agreements/{uuid}{ext} for local, or cloud URL for S3/GCS
    op.add_column(
        'Agreement',
        sa.Column('file_storage_path', sa.String(500), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('Agreement', 'file_storage_path')
