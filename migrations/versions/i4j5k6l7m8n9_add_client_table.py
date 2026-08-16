"""add client table and client_id columns

Revision ID: i4j5k6l7m8n9
Revises: h3i4j5k6l7m8
Create Date: 2025-02-04 17:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'i4j5k6l7m8n9'
down_revision = 'h3i4j5k6l7m8'
branch_labels = None
depends_on = None


def upgrade():
    # Create Client table
    op.create_table(
        'Client',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('User.id'), nullable=False, index=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('avatar_url', sa.String(), nullable=True),
        sa.Column('color', sa.String(), nullable=True, default='#3b82f6'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # Add client_id to ACRCloudScan
    op.add_column('ACRCloudScan', sa.Column('client_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_acrcloudscan_client_id', 'ACRCloudScan', 'Client',
        ['client_id'], ['id'], ondelete='SET NULL'
    )
    op.create_index('ix_acrcloudscan_client_id', 'ACRCloudScan', ['client_id'])

    # Add client_id to BatchUpload
    op.add_column('BatchUpload', sa.Column('client_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_batchupload_client_id', 'BatchUpload', 'Client',
        ['client_id'], ['id'], ondelete='SET NULL'
    )

    # Add client_id to UserCatalog
    op.add_column('UserCatalog', sa.Column('client_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_usercatalog_client_id', 'UserCatalog', 'Client',
        ['client_id'], ['id'], ondelete='SET NULL'
    )
    op.create_index('ix_usercatalog_client_id', 'UserCatalog', ['client_id'])

    # Add client_id to RevenueStatement
    op.add_column('RevenueStatement', sa.Column('client_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_revenuestatement_client_id', 'RevenueStatement', 'Client',
        ['client_id'], ['id'], ondelete='SET NULL'
    )
    op.create_index('ix_revenuestatement_client_id', 'RevenueStatement', ['client_id'])


def downgrade():
    # Remove client_id from RevenueStatement
    op.drop_index('ix_revenuestatement_client_id', 'RevenueStatement')
    op.drop_constraint('fk_revenuestatement_client_id', 'RevenueStatement', type_='foreignkey')
    op.drop_column('RevenueStatement', 'client_id')

    # Remove client_id from UserCatalog
    op.drop_index('ix_usercatalog_client_id', 'UserCatalog')
    op.drop_constraint('fk_usercatalog_client_id', 'UserCatalog', type_='foreignkey')
    op.drop_column('UserCatalog', 'client_id')

    # Remove client_id from BatchUpload
    op.drop_constraint('fk_batchupload_client_id', 'BatchUpload', type_='foreignkey')
    op.drop_column('BatchUpload', 'client_id')

    # Remove client_id from ACRCloudScan
    op.drop_index('ix_acrcloudscan_client_id', 'ACRCloudScan')
    op.drop_constraint('fk_acrcloudscan_client_id', 'ACRCloudScan', type_='foreignkey')
    op.drop_column('ACRCloudScan', 'client_id')

    # Drop Client table
    op.drop_table('Client')
