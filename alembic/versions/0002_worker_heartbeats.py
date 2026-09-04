"""Add worker heartbeat records.

Revision ID: 0002_worker_heartbeats
Revises: 0001_initial_schema
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0002_worker_heartbeats'
down_revision = '0001_initial_schema'
branch_labels = None
depends_on = None


def upgrade():
    if 'worker_heartbeats' not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            'worker_heartbeats',
            sa.Column('worker_id', sa.String(64), primary_key=True),
            sa.Column('heartbeat_at', sa.DateTime(
                timezone=True), nullable=False),
        )


def downgrade():
    op.drop_table('worker_heartbeats')
