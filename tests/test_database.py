import os
import sqlite3
import subprocess

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError


def migrate(application, action='upgrade', revision='head'):
    from app.database import migration_config
    from app.models import db
    with application.app_context(), db.engine.begin() as connection:
        config = migration_config()
        config.attributes['connection'] = connection
        getattr(command, action)(config, revision)


def test_fresh_migrations_match_models(app):
    from app.models import db
    with app.app_context(), db.engine.connect() as connection:
        inspector = inspect(connection)
        assert set(inspector.get_table_names()) == {'link', 'click', 'alembic_version'}
        assert {'name': 'ix_click_link_id', 'column_names': ['link_id'], 'unique': 0, 'dialect_options': {}} in inspector.get_indexes('click')
        assert compare_metadata(MigrationContext.configure(connection), db.metadata) == []
    assert app.test_cli_runner().invoke(args=['db-check']).exit_code == 0
    assert app.test_client().get('/api/ready').status_code == 200
    migrate(app)  # Upgrade is repeatable.


def test_app_creation_does_not_create_database(app, tmp_path):
    from app import create_app
    from app.models import db
    path = tmp_path / 'uninitialized.db'
    uninitialized = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{path}'})
    assert not path.exists()
    assert uninitialized.test_client().get('/health').status_code == 200
    assert uninitialized.test_client().get('/api/ready').status_code == 503
    result = uninitialized.test_cli_runner().invoke(args=['db-check'])
    assert result.exit_code != 0
    assert 'alembic upgrade head' in result.output
    migrate(uninitialized)
    assert uninitialized.test_client().get('/api/ready').status_code == 200
    with uninitialized.app_context():
        db.session.remove()
        db.engine.dispose()


def test_unavailable_database_is_reported_clearly(app, tmp_path):
    from app import create_app
    unavailable = create_app({'SQLALCHEMY_DATABASE_URI': f'sqlite:///{tmp_path / "missing" / "db.sqlite"}'})
    result = unavailable.test_cli_runner().invoke(args=['db-check'])
    assert result.exit_code != 0
    assert 'database unavailable' in result.output
    assert unavailable.test_client().get('/api/ready').status_code == 503


@pytest.mark.parametrize('prefix', ['postgres://', 'postgresql://', 'postgresql+psycopg://'])
def test_postgres_url_configuration_without_connecting(app, prefix):
    from app import create_app
    from app.models import db
    configured = create_app({'SQLALCHEMY_DATABASE_URI': prefix + 'localhost/smartlink'})
    with configured.app_context():
        assert db.engine.url.drivername == 'postgresql+psycopg'
        assert db.engine.dialect.name == 'postgresql'
        assert db.engine.dialect.driver == 'psycopg'
        assert db.engine.pool._pre_ping
        db.engine.dispose()


def test_environment_database_configuration(app, monkeypatch, tmp_path):
    from app import create_app
    from app.models import db
    path = tmp_path / 'environment.db'
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{path}')
    configured = create_app()
    with configured.app_context():
        assert db.engine.url.database == str(path)
        db.engine.dispose()
    assert not path.exists()


def test_legacy_sqlite_stamp_preserves_data(app, tmp_path):
    from app import create_app
    from app.models import db
    path = tmp_path / 'legacy.db'
    # Actual pre-migration table definitions, without the new analytics index.
    with sqlite3.connect(path) as connection:
        connection.executescript('''
            CREATE TABLE link (id INTEGER PRIMARY KEY NOT NULL, slug VARCHAR(64) NOT NULL UNIQUE,
                target VARCHAR(2048), ab_targets_json TEXT, created_at DATETIME, expires_at DATETIME,
                one_time BOOLEAN, disabled BOOLEAN);
            CREATE TABLE click (id INTEGER PRIMARY KEY NOT NULL, link_id INTEGER NOT NULL REFERENCES link(id),
                ts DATETIME, ip VARCHAR(64), referrer VARCHAR(2048), country VARCHAR(64), device VARCHAR(64));
            INSERT INTO link VALUES (7, 'legacy', 'https://example.com', NULL,
                '2020-01-01 00:00:00', '2099-01-01 00:00:00', 0, 0);
            INSERT INTO click VALUES (9, 7, '2020-01-01 00:01:00', '192.0.2.1', NULL, 'CA', 'desktop');
        ''')
        before = [connection.execute(f'SELECT * FROM {table}').fetchall() for table in ('link', 'click')]
    legacy = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{path}'})
    # A blind upgrade must refuse existing tables, not replace them.
    with pytest.raises(OperationalError, match='already exists'):
        migrate(legacy)
    migrate(legacy, action='stamp', revision='0001')
    migrate(legacy)
    with sqlite3.connect(path) as connection:
        assert before == [connection.execute(f'SELECT * FROM {table}').fetchall() for table in ('link', 'click')]
        assert connection.execute('SELECT version_num FROM alembic_version').fetchone() == ('0002',)
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_click_link_id'").fetchone()
    assert legacy.test_client().get('/legacy').status_code == 302
    assert legacy.test_client().get('/api/links/legacy/metrics').json['total'] == 2
    with legacy.app_context():
        db.session.remove()
        db.engine.dispose()


def test_index_downgrade_preserves_rows(app, client):
    from app.models import db, Link
    assert client.post('/api/links', json={'slug': 'keep', 'target': 'https://example.com'}).status_code == 201
    migrate(app, action='downgrade', revision='0001')
    assert app.test_client().get('/api/ready').status_code == 503
    with app.app_context(), db.engine.connect() as connection:
        assert inspect(connection).get_indexes('click') == []
        assert Link.query.one().slug == 'keep'
    migrate(app)
    assert app.test_client().get('/keep').status_code == 302


def test_postgresql_offline_migration_sql(app):
    environment = dict(os.environ, DATABASE_URL='postgresql+psycopg://localhost/smartlink')
    result = subprocess.run([os.sys.executable, '-m', 'alembic', 'upgrade', 'head', '--sql'],
                            env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'CREATE TABLE link' in result.stdout
    assert 'SERIAL' in result.stdout
    assert 'CREATE INDEX ix_click_link_id ON click (link_id)' in result.stdout


def test_real_cli_migration_workflow(app, tmp_path):
    path = tmp_path / 'cli.db'
    environment = dict(os.environ, DATABASE_URL=f'sqlite:///{path}')
    def run(*arguments):
        return subprocess.run([os.sys.executable, *arguments], env=environment,
                              capture_output=True, text=True)
    before = run('-m', 'flask', '--app', 'wsgi:app', 'db-check')
    assert before.returncode != 0
    assert 'alembic upgrade head' in before.stderr
    upgraded = run('-m', 'alembic', 'upgrade', 'head')
    assert upgraded.returncode == 0, upgraded.stderr
    assert run('-m', 'flask', '--app', 'wsgi:app', 'db-check').returncode == 0
    assert run('-m', 'alembic', 'check').returncode == 0
    smoke = run('-c', '''
from wsgi import app
client = app.test_client()
assert client.get('/api/ready').status_code == 200
assert client.post('/api/links', json={'slug': 'cli', 'target': 'https://example.com', 'one_time': True}).status_code == 201
assert client.get('/cli').status_code == 302
assert client.get('/cli').status_code == 404
assert client.get('/api/links/cli/metrics').json['total'] == 1
''')
    assert smoke.returncode == 0, smoke.stderr
