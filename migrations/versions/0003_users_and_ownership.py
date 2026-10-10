"""Users, nullable legacy ownership, and soft deletion without rebuilding links."""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    # Refuse pre-existing corruption instead of silently dropping legacy rows.
    connection = op.get_bind()
    if not op.get_context().as_sql:
        orphan = connection.execute(sa.text(
            'SELECT click.id FROM click LEFT JOIN link ON click.link_id = link.id '
            'WHERE link.id IS NULL LIMIT 1'
        )).first()
        if orphan:
            raise RuntimeError('Orphaned clicks found; repair on a verified backup before migrating')
    op.create_table(
        'app_user',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('email', sa.String(254), nullable=False),
        sa.Column('password_hash', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'),
        sa.UniqueConstraint('email'),
        sa.CheckConstraint('token_version >= 0', name='ck_user_token_version'),
    )
    if connection.dialect.name == 'sqlite':
        # SQLite supports a nullable REFERENCES column directly. Avoid batch
        # table replacement because click already references link.
        op.execute('ALTER TABLE link ADD COLUMN owner_id INTEGER '
                   'CONSTRAINT fk_link_owner REFERENCES app_user(id)')
    else:
        op.add_column('link', sa.Column('owner_id', sa.Integer(), nullable=True))
        op.create_foreign_key('fk_link_owner', 'link', 'app_user', ['owner_id'], ['id'])
    op.add_column('link', sa.Column('deleted_at', sa.DateTime(), nullable=True))
    op.create_index('ix_link_owner_id_id', 'link', ['owner_id', 'id'])


def downgrade():
    # SQLite DROP COLUMN requires SQLite >= 3.35. Downgrade discards user data.
    op.drop_index('ix_link_owner_id_id', table_name='link')
    if op.get_bind().dialect.name != 'sqlite':
        op.drop_constraint('fk_link_owner', 'link', type_='foreignkey')
    op.drop_column('link', 'deleted_at')
    op.drop_column('link', 'owner_id')
    op.drop_table('app_user')
