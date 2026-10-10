import importlib
import secrets

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    # Importing app also constructs its public WSGI app; isolate that database too.
    monkeypatch.setenv('JWT_SIGNING_KEY', secrets.token_hex(32))
    monkeypatch.setenv('AUTH_RATE_LIMIT_MODE', 'local')
    monkeypatch.setenv('APP_ENV', 'development')
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "startup.db"}')
    module = importlib.import_module('app')
    application = module.create_app({
        'TESTING': True,
        'RATELIMIT_ENABLED': False,
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{tmp_path / "test.db"}',
    })
    from alembic import command
    from app.database import migration_config
    with application.app_context(), module.db.engine.begin() as connection:
        config = migration_config()
        config.attributes['connection'] = connection
        command.upgrade(config, 'head')
    monkeypatch.setattr('app.main.lookup_country', lambda ip: None)
    yield application
    with application.app_context():
        module.db.session.remove()
        module.db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


from test_postgresql import postgres_app  # Shared opt-in, random-schema fixture.
