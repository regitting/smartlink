"""Opt-in PostgreSQL tests create and remove only their own random schema."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from alembic import command
from sqlalchemy import create_engine, text


@pytest.fixture
def postgres_app(app, monkeypatch):
    url = os.getenv('TEST_POSTGRES_URL')
    if not url:
        pytest.skip('TEST_POSTGRES_URL is unset; real PostgreSQL integration not run')
    from app import create_app
    from app.database import database_url, migration_config
    from app.models import db
    url = database_url(url)
    if url.get_backend_name() != 'postgresql':
        pytest.fail('TEST_POSTGRES_URL must point to PostgreSQL')
    admin = create_engine(url, connect_args={'connect_timeout': 5})
    schema = 'smartlink_test_' + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA {schema}'))
    application = None
    try:
        application = create_app({
            'TESTING': True, 'RATELIMIT_ENABLED': False, 'SQLALCHEMY_DATABASE_URI': url,
            'SQLALCHEMY_ENGINE_OPTIONS': {'pool_pre_ping': True, 'connect_args': {
                'connect_timeout': 5, 'options': f'-csearch_path={schema}',
            }},
        })
        with application.app_context(), db.engine.begin() as connection:
            config = migration_config()
            config.attributes['connection'] = connection
            command.upgrade(config, 'head')
        monkeypatch.setattr('app.main.lookup_country', lambda ip: None)
        yield application
    finally:
        if application is not None:
            with application.app_context():
                db.session.remove()
                db.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()


def test_postgresql_api_and_migrations(postgres_app):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from app.models import db
    with postgres_app.app_context(), db.engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), db.metadata) == []
    client = postgres_app.test_client()
    assert client.get('/api/ready').status_code == 200
    assert client.post('/api/links', json={'slug': 'pg', 'target': 'https://example.com'}).status_code == 201
    assert client.post('/api/links', json={'slug': 'pg', 'target': 'https://example.com'}).status_code == 409
    assert client.get('/pg').location == 'https://example.com'
    assert client.get('/api/links/pg/metrics').json == {
        'total': 1, 'by_device': {'unknown': 1}, 'by_country': {'unknown': 1},
    }
    assert client.post('/api/links', json={'slug': 'expired', 'target': 'https://example.com',
                                          'expires_at': '2000-01-01T00:00:00Z'}).status_code == 201
    assert client.get('/expired').status_code == 404
    assert client.post('/api/links', json={'slug': 'ab', 'ab_targets': ['https://example.com/a'],
                                          'expires_at': '2099-01-01T00:00:00+05:30'}).status_code == 201
    assert client.get('/ab').location == 'https://example.com/a'


@pytest.mark.parametrize('one_time', [True, False])
def test_postgresql_concurrent_redemption(postgres_app, one_time):
    from app.models import Click, Link, db
    client = postgres_app.test_client()
    assert client.post('/api/links', json={'slug': 'once', 'target': 'https://example.com',
                                          'one_time': one_time}).status_code == 201
    barrier = Barrier(6)
    def redeem(_):
        with postgres_app.test_client() as independent_client:
            barrier.wait(timeout=10)
            return independent_client.get('/once').status_code
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(redeem, range(6)))
    assert responses.count(302) == (1 if one_time else 6)
    assert responses.count(404) == (5 if one_time else 0)
    with postgres_app.app_context():
        assert Click.query.count() == (1 if one_time else 6)
        assert Link.query.one().disabled == one_time
