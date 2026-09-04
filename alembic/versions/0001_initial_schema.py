"""Create the job and resource lease tables.

Revision ID: 0001_initial_schema
Revises:
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0001_initial_schema'
down_revision = None
branch_labels = None
depends_on = None


JOB_COLUMNS = {
    'selected_format': sa.Column('selected_format', sa.String(64)),
    'title': sa.Column('title', sa.String(500)),
    'thumbnail_url': sa.Column('thumbnail_url', sa.Text()),
    'available_formats': sa.Column('available_formats', sa.Text()),
    'progress': sa.Column('progress', sa.Integer(), nullable=False,
                          server_default='0'),
    'total_bytes': sa.Column('total_bytes', sa.Integer()),
}


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if 'jobs' not in tables:
        op.create_table(
            'jobs',
            sa.Column('id', sa.String(64), primary_key=True),
            sa.Column('source_url', sa.Text(), nullable=False),
            sa.Column('platform', sa.String(32), nullable=False),
            sa.Column('status', sa.String(24), nullable=False),
            sa.Column('selected_format', sa.String(64)),
            sa.Column('title', sa.String(500)),
            sa.Column('thumbnail_url', sa.Text()),
            sa.Column('available_formats', sa.Text()),
            sa.Column('progress', sa.Integer(), nullable=False,
                      server_default='0'),
            sa.Column('total_bytes', sa.Integer()),
            sa.Column('filename', sa.String(255)),
            sa.Column('file_url', sa.Text()),
            sa.Column('error_message', sa.Text()),
            sa.Column('attempts', sa.Integer(), nullable=False,
                      server_default='0'),
            sa.Column('created_at', sa.DateTime(
                timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(
                timezone=True), nullable=False),
        )
        op.create_index('ix_jobs_status_created_at', 'jobs',
                        ['status', 'created_at'])
    else:
        existing = {column['name'] for column in inspector.get_columns('jobs')}
        for name, column in JOB_COLUMNS.items():
            if name not in existing:
                op.add_column('jobs', column)

    if 'resource_leases' not in tables:
        op.create_table(
            'resource_leases',
            sa.Column('job_id', sa.String(64), primary_key=True),
            sa.Column('reserved_bytes', sa.Integer(), nullable=False),
            sa.Column('acquired_at', sa.DateTime(
                timezone=True), nullable=False),
        )


def downgrade():
    op.drop_table('resource_leases')
