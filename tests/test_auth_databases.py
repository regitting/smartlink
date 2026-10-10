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


def test_concurrent_password_rehash(backend_app):
    from argon2 import PasswordHasher
    from app.models import User, db
    from test_auth import PASSWORD
    client = backend_app.test_client()
    assert register(client).status_code == 201
    old = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PASSWORD)
    with backend_app.app_context():
        User.query.one().password_hash = old
        db.session.commit()
        engine = db.engine
    barrier = Barrier(2)
    def synchronize(connection, cursor, statement, parameters, context, many):
        if statement.upper().startswith('UPDATE APP_USER'):
            barrier.wait(timeout=15)
    event.listen(engine, 'before_cursor_execute', synchronize)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: login(backend_app.test_client()), range(2)))
    finally:
        event.remove(engine, 'before_cursor_execute', synchronize)
    assert [r.status_code for r in responses] == [200, 200]
    for response in responses:
        assert client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + response.json['access_token']}).status_code == 200
    with backend_app.app_context():
        assert not backend_app.extensions['password_hasher'].check_needs_rehash(User.query.one().password_hash)


@pytest.mark.parametrize('operation', ['redeem', 'delete'])
def test_patch_race_on_both_databases(backend_app, operation):
    from app.models import db, Click, Link
    from threading import Event
    client = backend_app.test_client()
    headers = account(client)
    assert client.post('/api/links', headers=headers, json={'slug': 'race', 'target': 'https://example.com', 'one_time': True}).status_code == 201
    with backend_app.app_context(): engine = db.engine
    read_finished, competitor_finished = Event(), Event()
    def pause_patch(connection, cursor, statement, parameters, context, many):
        if statement.startswith('UPDATE link SET') and 'link.target =' in statement:
            read_finished.set()
            assert competitor_finished.wait(15)
    event.listen(engine, 'before_cursor_execute', pause_patch)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(lambda: backend_app.test_client().patch('/api/links/race', headers=headers, json={'disabled': False}))
            assert read_finished.wait(15)
            try:
                response = client.get('/race') if operation == 'redeem' else client.delete('/api/links/race', headers=headers)
                assert response.status_code == (302 if operation == 'redeem' else 204)
            finally:
                competitor_finished.set()
            assert pending.result(timeout=15).status_code == (409 if operation == 'redeem' else 404)
    finally:
        event.remove(engine, 'before_cursor_execute', pause_patch)
    assert client.get('/race').status_code == 404
    with backend_app.app_context():
        assert Link.query.one().disabled
        assert Click.query.count() == (1 if operation == 'redeem' else 0)


def test_interrupted_sqlite_migration_rolls_back_and_retries(app):
    from sqlalchemy import inspect
    from app.models import db
    migrate(app, 'downgrade', '0002')
    with app.app_context():
        db.session.execute(text("INSERT INTO link (id,slug,target) VALUES (17,'kept','https://example.com')"))
        db.session.execute(text('INSERT INTO click (id,link_id) VALUES (19,17)'))
        db.session.commit()
        engine = db.engine
    def interrupt(connection, cursor, statement, parameters, context, many):
        if statement.startswith('CREATE INDEX ix_link_owner_id_id'):
            raise RuntimeError('injected migration interruption')
    event.listen(engine, 'before_cursor_execute', interrupt)
    try:
        with pytest.raises(RuntimeError, match='injected migration interruption'):
            migrate(app)
    finally:
        event.remove(engine, 'before_cursor_execute', interrupt)
    with engine.connect() as connection:
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalar() == '0002'
        assert 'app_user' not in inspect(connection).get_table_names()
        assert not {'owner_id','deleted_at'} & {c['name'] for c in inspect(connection).get_columns('link')}
        assert connection.execute(text('SELECT id,link_id FROM click')).one() == (19,17)
        assert connection.execute(text('PRAGMA foreign_keys')).scalar() == 1
    migrate(app)
    with engine.connect() as connection:
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalar() == '0003'
        assert connection.execute(text('SELECT id,owner_id,deleted_at FROM link')).one() == (17,None,None)
        assert connection.execute(text('SELECT id,link_id FROM click')).one() == (19,17)


@pytest.mark.parametrize('change', ['password', 'inactive', 'revoked'])
def test_login_rechecks_concurrently_changed_account(backend_app, monkeypatch, change):
    from app.models import User, db
    from sqlalchemy import update
    from test_auth import PASSWORD
    client = backend_app.test_client()
    assert register(client).status_code == 201
    hasher = backend_app.extensions['password_hasher']
    original_verify = hasher.verify
    changed = False
    def verify_then_change(stored, password):
        nonlocal changed
        result = original_verify(stored, password)
        if not changed:
            changed = True
            values = {'password_hash': hasher.hash('a different valid password')} if change == 'password' else ({'is_active': False} if change == 'inactive' else {'token_version': User.token_version + 1})
            with backend_app.app_context():
                db.session.execute(update(User).values(**values))
                db.session.commit()
        return result
    from types import SimpleNamespace
    monkeypatch.setitem(backend_app.extensions, 'password_hasher', SimpleNamespace(
        verify=verify_then_change, hash=hasher.hash, check_needs_rehash=hasher.check_needs_rehash))
    response = login(client)
    assert response.status_code == 401
    assert response.json['code'] == 'invalid_credentials'
