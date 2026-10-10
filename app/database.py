from pathlib import Path

import sqlite3
import click
from sqlalchemy import event
from sqlalchemy.engine import Engine
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError


MIGRATIONS = Path(__file__).resolve().parent.parent / 'migrations'


def database_url(value):
    """Use psycopg 3 for PostgreSQL URLs, retaining SQLite URLs unchanged."""
    url = make_url(value)
    if url.drivername in ('postgres', 'postgresql'):
        url = url.set(drivername='postgresql+psycopg')
    return url


def migration_config():
    config = Config(str(MIGRATIONS.parent / 'alembic.ini'))
    config.set_main_option('script_location', str(MIGRATIONS))
    return config


def check_database():
    """Read-only check: connection and revision must both be ready."""
    from .models import db
    expected = set(ScriptDirectory.from_config(migration_config()).get_heads())
    with db.engine.connect() as connection:
        connection.execute(text('SELECT 1'))
        actual = set(MigrationContext.configure(connection).get_current_heads())
        if actual != expected:
            raise RuntimeError('database schema is not current; run alembic upgrade head')


def register_database_commands(app):
    @app.cli.command('db-check')
    def db_check():
        """Check connectivity and schema readiness without applying migrations."""
        try:
            check_database()
        except SQLAlchemyError:
            raise click.ClickException('database unavailable; check DATABASE_URL and database readiness') from None
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from None
        click.echo('Database is ready')


@event.listens_for(Engine, 'connect')
def enable_sqlite_foreign_keys(connection, record):
    if isinstance(connection, sqlite3.Connection):
        connection.execute('PRAGMA foreign_keys=ON')
