from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError


def create(client, **fields):
    return client.post('/api/links', json={
        'slug': 'secure', 'target': 'https://example.com', **fields,
    })


def test_metrics_over_1000(client, app):
    from app.models import Click, Link, db
    assert create(client).status_code == 201
    with app.app_context():
        link = Link.query.one()
        db.session.add_all([
            Click(link_id=link.id, device='desktop', country='CA') for _ in range(1101)
        ] + [
            Click(link_id=link.id, device='mobile', country='US') for _ in range(200)
        ] + [
            Click(link_id=link.id, device=None, country='') for _ in range(3)
        ] + [
            Click(link_id=link.id, device='', country=None) for _ in range(2)
        ] + [Click(link_id=link.id, device='unknown', country='unknown')])
        # An unrelated link's clicks must not enter the aggregation.
        other = Link(slug='other', target='https://example.com')
        db.session.add(other)
        db.session.flush()
        db.session.add(Click(link_id=other.id, device='tablet', country='GB'))
        db.session.commit()
    assert client.get('/api/links/secure/metrics').json == {
        'total': 1307,
        'by_device': {'desktop': 1101, 'mobile': 200, 'unknown': 6},
        'by_country': {'CA': 1101, 'US': 200, 'unknown': 6},
    }


@pytest.mark.parametrize('journal_mode', ['DELETE', 'WAL'])
@pytest.mark.parametrize('one_time', [True, False])
def test_concurrent_redirects(app, client, journal_mode, one_time):
    from app.models import Click, Link, db
    assert create(client, one_time=one_time).status_code == 201
    with app.app_context():
        engine = db.engine
        with engine.connect() as connection:
            assert connection.exec_driver_sql(f'PRAGMA journal_mode={journal_mode}').scalar().upper() == journal_mode
    workers = 6
    barrier = Barrier(workers)

    def synchronize_updates(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('UPDATE LINK '):
            # Synchronize actual independent DB connections before claiming.
            barrier.wait(timeout=10)

    event.listen(engine, 'before_cursor_execute', synchronize_updates)
    try:
        def redeem(_):
            with app.test_client() as thread_client:
                response = thread_client.get('/secure')
                return response.status_code, response.location
        with ThreadPoolExecutor(max_workers=workers) as pool:
            responses = list(pool.map(redeem, range(workers)))
    finally:
        event.remove(engine, 'before_cursor_execute', synchronize_updates)
    assert sum(status == 302 for status, _ in responses) == (1 if one_time else workers)
    assert sum(status == 404 for status, _ in responses) == (workers - 1 if one_time else 0)
    assert all(location == 'https://example.com' for status, location in responses if status == 302)
    with app.app_context():
        assert Click.query.count() == (1 if one_time else workers)
        assert Link.query.one().disabled == one_time


def test_click_insert_failure_rolls_back_claim(client, app):
    from app.models import Click, Link, db
    assert create(client, one_time=True).status_code == 201
    with app.app_context():
        db.session.execute(text("CREATE TRIGGER reject_click BEFORE INSERT ON click BEGIN SELECT RAISE(FAIL, 'test insert failure'); END"))
        db.session.commit()
    with pytest.raises(IntegrityError):
        client.get('/secure')
    with app.app_context():
        assert not Link.query.one().disabled
        assert Click.query.count() == 0
        db.session.execute(text('DROP TRIGGER reject_click'))
        db.session.commit()
    assert client.get('/secure').status_code == 302
    assert client.get('/secure').status_code == 404
    assert client.get('/api/links/secure/metrics').json['total'] == 1


@pytest.mark.parametrize('one_time', [True, False])
def test_sqlite_lock_timeout_returns_retryable_response(client, app, one_time):
    from app.models import Click, Link, db
    assert create(client, one_time=one_time).status_code == 201
    with app.app_context():
        engine = db.engine
        path = engine.url.database

    def short_timeout(connection, record, proxy):
        connection.execute('PRAGMA busy_timeout=10')

    event.listen(engine, 'checkout', short_timeout)
    blocker = sqlite3.connect(path)
    try:
        blocker.execute('BEGIN IMMEDIATE')
        response = client.get('/secure')
        assert response.status_code == 503
        assert response.headers['Retry-After'] == '1'
        assert response.json == {'error': 'database busy; retry later'}
    finally:
        blocker.rollback()
        blocker.close()
        event.remove(engine, 'checkout', short_timeout)
    with app.app_context():
        assert Click.query.count() == 0
        assert not Link.query.one().disabled
    assert client.get('/secure').status_code == 302


@pytest.mark.parametrize('state', ['expired', 'disabled'])
def test_ineligible_one_time_link_has_no_click(client, app, state):
    from app.models import Click, Link, db
    fields = {'one_time': True}
    if state == 'expired':
        fields['expires_at'] = '2000-01-01T00:00:00Z'
    assert create(client, **fields).status_code == 201
    if state == 'disabled':
        with app.app_context():
            Link.query.one().disabled = True
            db.session.commit()
    assert client.get('/secure').status_code == 404
    with app.app_context():
        assert Click.query.count() == 0


@pytest.mark.parametrize('hops, forwarded, expected', [
    (0, '198.51.100.99', '192.0.2.10'),
    (0, '198.51.100.99, 203.0.113.7', '192.0.2.10'),
    (1, '198.51.100.99, 203.0.113.7', '203.0.113.7'),
    (2, '198.51.100.99, 203.0.113.7, 192.0.2.20', '203.0.113.7'),
    (2, '198.51.100.99', '192.0.2.10'),
    (1, '', '192.0.2.10'),
])
def test_proxy_ip_configuration(app, tmp_path, monkeypatch, hops, forwarded, expected):
    from app import create_app
    from app.models import Click, db
    proxy_app = create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{tmp_path / "proxy.db"}',
        'TRUSTED_PROXY_HOPS': hops,
        'ENABLE_DEBUG_IP': True,
    })
    from alembic import command
    from app.database import migration_config
    with proxy_app.app_context(), db.engine.begin() as connection:
        config = migration_config()
        config.attributes['connection'] = connection
        command.upgrade(config, 'head')
    from flask import request
    @proxy_app.before_request
    def observe_origin():
        assert request.host == 'localhost'
        assert request.scheme == 'http'

    ips = []
    monkeypatch.setattr('app.main.lookup_country', lambda ip: ips.append(ip) or None)
    headers = {'X-Forwarded-For': forwarded, 'X-Forwarded-Host': 'evil.example',
               'X-Forwarded-Proto': 'https', 'X-Forwarded-Port': '443'}
    try:
        with proxy_app.test_client() as proxy_client:
            assert create(proxy_client).status_code == 201
            assert proxy_client.get('/secure', headers=headers, environ_base={'REMOTE_ADDR': '192.0.2.10'}).status_code == 302
            response = proxy_client.get('/_debug/ip', headers=headers, environ_base={'REMOTE_ADDR': '192.0.2.10'})
            assert response.json['ip'] == expected
        assert ips == [expected, expected]
        with proxy_app.app_context():
            assert Click.query.one().ip == expected
    finally:
        with proxy_app.app_context():
            db.session.remove()
            db.engine.dispose()


def test_debug_ip_disabled_by_default(client, monkeypatch):
    def unexpected_lookup(ip):
        pytest.fail('disabled debug endpoint must not perform GeoIP lookup')
    monkeypatch.setattr('app.main.lookup_country', unexpected_lookup)
    assert client.get('/_debug/ip').status_code == 404


@pytest.mark.parametrize('hops', [-1, True, '1', 1.5])
def test_invalid_proxy_config(app, hops):
    from app import create_app
    with pytest.raises(ValueError, match='TRUSTED_PROXY_HOPS'):
        create_app({'TRUSTED_PROXY_HOPS': hops})


def test_proxy_environment_configuration(app, monkeypatch, tmp_path):
    from app import create_app
    from app.models import db
    monkeypatch.setenv('TRUSTED_PROXY_HOPS', '1')
    monkeypatch.setenv('ENABLE_DEBUG_IP', '1')
    configured = create_app({'SQLALCHEMY_DATABASE_URI': f'sqlite:///{tmp_path / "env.db"}'})
    try:
        response = configured.test_client().get('/_debug/ip', headers={'X-Forwarded-For': '203.0.113.7'})
        assert response.json['ip'] == '203.0.113.7'
    finally:
        with configured.app_context():
            db.session.remove()
            db.engine.dispose()
