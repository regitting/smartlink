from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from test_auth import account, login, register
from test_database import migrate


@pytest.fixture(params=['sqlite', 'postgresql'])
def backend_app(request, app):
    return app if request.param == 'sqlite' else request.getfixturevalue('postgres_app')


def test_auth_and_ownership_on_both_databases(backend_app):
    from app.models import User, Link, db
    client = backend_app.test_client()
    owner = account(client, 'owner@example.com')
    other = account(client, 'other@example.com')
    assert register(client, 'OWNER@EXAMPLE.COM').status_code == 409
    assert login(client, 'missing@example.com').status_code == 401
    assert client.post('/api/links', headers=owner, json={'slug': 'private', 'target': 'https://example.com'}).status_code == 201
    assert client.get('/private').status_code == 302
    assert client.get('/api/links/private/metrics', headers=owner).json['total'] == 1
    assert client.get('/api/links/private/metrics', headers=other).status_code == 404
    assert client.patch('/api/links/private', headers=other, json={'target': 'https://evil.example'}).status_code == 404
    assert client.delete('/api/links/private', headers=other).status_code == 404
    assert client.patch('/api/links/private', headers=owner, json={'target': 'https://example.com/new'}).status_code == 200
    assert client.get('/private').location == 'https://example.com/new'
    assert client.delete('/api/links/private', headers=owner).status_code == 204
    assert client.get('/private').status_code == 404
    assert client.get('/api/links', headers=owner).json['links'] == []
    with backend_app.app_context():
        user = User.query.filter_by(email='owner@example.com').one()
        db.session.delete(user)
        with pytest.raises(IntegrityError): db.session.commit()
        db.session.rollback()
        assert Link.query.one().deleted_at is not None
    assert client.post('/api/auth/logout', headers=owner).status_code == 204
    assert client.get('/api/auth/me', headers=owner).status_code == 401
    assert client.get('/api/auth/me', headers=other).status_code == 200


@pytest.mark.parametrize('operation', ['register', 'logout', 'redeem'])
def test_atomic_auth_operations_both_databases(backend_app, operation):
    from app.models import User, Link, Click, db
    client = backend_app.test_client()
    if operation != 'register':
        headers = account(client)
    if operation == 'redeem':
        assert client.post('/api/links', headers=headers, json={
            'slug': 'once', 'target': 'https://example.com', 'one_time': True,
        }).status_code == 201
    with backend_app.app_context(): engine = db.engine
    workers = 4
    barrier = Barrier(workers)
    prefix = {'register': 'INSERT INTO APP_USER', 'logout': 'UPDATE APP_USER', 'redeem': 'UPDATE LINK'}[operation]
    def synchronize(connection, cursor, statement, parameters, context, many):
        if statement.upper().startswith(prefix):
            barrier.wait(timeout=10)
    event.listen(engine, 'before_cursor_execute', synchronize)
    try:
        def attempt(_):
            with backend_app.test_client() as independent:
                if operation == 'register': return register(independent, 'race@example.com').status_code
                if operation == 'logout': return independent.post('/api/auth/logout', headers=headers).status_code
                return independent.get('/once').status_code
        with ThreadPoolExecutor(max_workers=workers) as pool:
            statuses = list(pool.map(attempt, range(workers)))
    finally:
        event.remove(engine, 'before_cursor_execute', synchronize)
    if operation == 'register':
        assert sorted(statuses) == [201, 409, 409, 409]
        with backend_app.app_context(): assert User.query.count() == 1
    elif operation == 'logout':
        assert statuses == [204] * workers
        with backend_app.app_context(): assert User.query.one().token_version == workers
        assert client.get('/api/auth/me', headers=headers).status_code == 401
        assert login(client).status_code == 200
    else:
        assert sorted(statuses) == [302, 404, 404, 404]
        with backend_app.app_context():
            assert Click.query.count() == 1
            assert Link.query.one().disabled


def test_revision_0002_upgrade_preserves_existing_links_and_clicks(backend_app):
    from app.models import Link, Click, db
    migrate(backend_app, 'downgrade', '0002')
    with backend_app.app_context():
        db.session.execute(text("INSERT INTO link (id, slug, target, created_at, one_time, disabled) VALUES (17, 'legacy', 'https://example.com', '2020-01-01 00:00:00', false, false)"))
        db.session.execute(text("INSERT INTO click (id, link_id, ts, ip, country, device) VALUES (19, 17, '2020-01-01 00:01:00', '192.0.2.1', 'CA', 'desktop')"))
        db.session.commit()
    migrate(backend_app)
    client = backend_app.test_client()
    with backend_app.app_context():
        link = Link.query.one()
        click = Click.query.one()
        assert link.id == 17 and link.owner_id is None and link.deleted_at is None
        assert click.id == 19 and click.link_id == 17 and click.country == 'CA'
    assert client.get('/legacy').status_code == 302
    assert client.get('/api/links/legacy/metrics').json['total'] == 2
    headers = account(client)
    assert client.patch('/api/links/legacy', headers=headers, json={'disabled': True}).status_code == 404


def test_migration_refuses_orphans_on_sqlite(app, tmp_path):
    import sqlite3
    from app import create_app
    from app.models import db
    path = tmp_path / 'orphan.db'
    configured = create_app({'TESTING': True, 'RATELIMIT_ENABLED': False, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{path}'})
    migrate(configured, 'upgrade', '0002')
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO click (link_id) VALUES (999)")
    with pytest.raises(RuntimeError, match='Orphaned clicks'):
        migrate(configured)
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT link_id FROM click').fetchone() == (999,)
        assert connection.execute('SELECT version_num FROM alembic_version').fetchone() == ('0002',)
    with configured.app_context(): db.engine.dispose()
