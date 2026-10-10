"""Baseline existing Link and Click schema; safe stamping point for legacy DBs."""
from alembic import op
import sqlalchemy as sa

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'link',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('slug', sa.String(64), nullable=False),
        sa.Column('target', sa.String(2048)),
        sa.Column('ab_targets_json', sa.Text()),
        sa.Column('created_at', sa.DateTime()),
        sa.Column('expires_at', sa.DateTime()),
        sa.Column('one_time', sa.Boolean()),
        sa.Column('disabled', sa.Boolean()),
        sa.UniqueConstraint('slug'),
    )
    op.create_table(
        'click',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('link_id', sa.Integer(), sa.ForeignKey('link.id'), nullable=False),
        sa.Column('ts', sa.DateTime()),
        sa.Column('ip', sa.String(64)),
        sa.Column('referrer', sa.String(2048)),
        sa.Column('country', sa.String(64)),
        sa.Column('device', sa.String(64)),
    )


def downgrade():
    op.drop_table('click')
    op.drop_table('link')
