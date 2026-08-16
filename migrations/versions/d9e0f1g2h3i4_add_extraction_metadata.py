"""Add extraction metadata columns to Agreement table

Revision ID: d9e0f1g2h3i4
Revises: c8d9e0f1g2h3
Create Date: 2026-01-03 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd9e0f1g2h3i4'
down_revision = 'c8d9e0f1g2h3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add extraction metadata columns for efficient querying
    # Note: The actual metadata is also stored in parsed_content.extraction_metadata
    # These columns provide indexed access for filtering low-quality extractions

    op.add_column(
        'Agreement',
        sa.Column('extraction_method', sa.String(50), nullable=True)
    )

    op.add_column(
        'Agreement',
        sa.Column('extraction_quality_score', sa.Integer(), nullable=True, server_default='0')
    )

    op.add_column(
        'Agreement',
        sa.Column('extraction_warnings', sa.JSON(), nullable=True, server_default='[]')
    )

    op.add_column(
        'Agreement',
        sa.Column('text_character_count', sa.Integer(), nullable=True, server_default='0')
    )

    # Add index on extraction_quality_score for efficient filtering
    op.create_index(
        'ix_agreement_extraction_quality_score',
        'Agreement',
        ['extraction_quality_score']
    )


def downgrade() -> None:
    op.drop_index('ix_agreement_extraction_quality_score', table_name='Agreement')
    op.drop_column('Agreement', 'text_character_count')
    op.drop_column('Agreement', 'extraction_warnings')
    op.drop_column('Agreement', 'extraction_quality_score')
    op.drop_column('Agreement', 'extraction_method')
