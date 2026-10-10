from alembic import context
from app.models import db

config = context.config


def configure(connection=None, url=None):
    context.configure(
        connection=connection, url=url, target_metadata=db.metadata,
        compare_type=True, render_as_batch=True,
        literal_binds=connection is None,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    from app import app
    with app.app_context():
        configure(url=db.engine.url)
else:
    # Tests and callers can supply a connection to an isolated database.
    connection = config.attributes.get('connection')
    if connection is not None:
        configure(connection=connection)
    else:
        from app import app
        with app.app_context(), db.engine.connect() as connection:
            configure(connection=connection)
