"""Add shared rate-limit buckets.

Revision ID: 0003_rate_limit_buckets
Revises: 0002_worker_heartbeats
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0003_rate_limit_buckets'
down_revision = '0002_worker_heartbeats'
branch_labels = None
depends_on = None


def upgrade():
    if 'rate_limit_buckets' not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            'rate_limit_buckets',
            sa.Column('key', sa.String(255), primary_key=True),
            sa.Column('tokens', sa.Float(), nullable=False),
            sa.Column('updated_at', sa.DateTime(
                timezone=True), nullable=False),
        )


def downgrade():
    op.drop_table('rate_limit_buckets')
