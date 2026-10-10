"""Index the link_id filter used by every analytics aggregation."""
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_click_link_id', 'click', ['link_id'])


def downgrade():
    op.drop_index('ix_click_link_id', table_name='click')
