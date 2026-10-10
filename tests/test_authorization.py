from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from test_auth import account


def create(client, headers=None, **fields):
    return client.post('/api/links', headers=headers or {}, json={
        'slug': 'owned', 'target': 'https://example.com', **fields,
    })


def test_ownership_isolation_and_public_behavior(client, app):
    from app.models import Link
    owner = account(client, 'owner@example.com')
    other = account(client, 'other@example.com')
    assert create(client, owner).json == {'slug': 'owned'}
    assert client.get('/owned').status_code == 302
    assert client.post('/api/qr/owned').mimetype == 'image/png'
    assert client.post('/api/qr/nonexistent').status_code == 200
    assert client.get('/api/links/owned/metrics', headers=owner).json['total'] == 1
    assert client.get('/api/links/owned/metrics').status_code == 404
    assert client.get('/api/links/owned', headers=owner).status_code == 200
    assert client.get('/api/links', headers=other).json['links'] == []
    for verb, suffix in [('get', ''), ('get', '/metrics'), ('patch', ''), ('delete', '')]:
        response = getattr(client, verb)('/api/links/owned' + suffix, headers=other)
        missing = getattr(client, verb)('/api/links/missing' + suffix, headers=other)
        assert response.status_code == missing.status_code == 404
        assert response.json == missing.json
    with app.app_context():
        assert Link.query.one().owner.email == 'owner@example.com'


@pytest.mark.parametrize('field', ['owner_id', 'user_id', 'owner', 'deleted_at', 'disabled', 'id'])
def test_creation_mass_assignment_rejected(client, field):
    owner = account(client)
    assert create(client, owner, **{field: 1}).status_code == 400
    assert create(client, **{field: 1}).status_code == 400


def test_anonymous_compatibility_and_no_claiming(client, app):
    from app.models import Link
    assert create(client, slug='anonymous').status_code == 201
    assert client.get('/anonymous').status_code == 302
    assert client.get('/api/links/anonymous/metrics').json['total'] == 1
    user = account(client)
    assert client.get('/api/links/anonymous/metrics', headers=user).status_code == 200
    assert client.get('/api/links', headers=user).json['links'] == []
    for verb in ('get', 'patch', 'delete'):
        assert getattr(client, verb)('/api/links/anonymous', headers=user).status_code == 404
    with app.app_context():
        assert Link.query.one().owner_id is None
    app.config['ALLOW_ANONYMOUS_LINK_CREATION'] = False
    assert create(client, slug='blocked').status_code == 401
    assert create(client, user, slug='allowed').status_code == 201
    app.config['ALLOW_ANONYMOUS_ANALYTICS'] = False
    assert client.get('/api/links/anonymous/metrics').status_code == 401
    assert client.get('/api/links/anonymous/metrics', headers=user).status_code == 404
    assert client.get('/anonymous').status_code == 302


def test_link_management_validation_and_soft_delete(client, app):
    from app.models import Click, Link
    owner = account(client)
    assert create(client, owner).status_code == 201
    assert client.patch('/api/links/owned', headers=owner, json={'target': 'https://example.com/new', 'expires_at': '2099-01-01T12:00:00+05:30'}).status_code == 200
    assert client.get('/owned').location == 'https://example.com/new'
    assert client.patch('/api/links/owned', headers=owner, json={'target': None, 'ab_targets': ['https://example.com/ab'], 'expires_at': None}).status_code == 200
    assert client.get('/owned').location == 'https://example.com/ab'
    assert client.patch('/api/links/owned', headers=owner, json={'disabled': True}).status_code == 200
    assert client.get('/owned').status_code == 404
    assert client.patch('/api/links/owned', headers=owner, json={'disabled': False}).status_code == 200
    assert client.delete('/api/links/owned', headers=owner).status_code == 204
    for suffix in ('', '/metrics'):
        assert client.get('/api/links/owned' + suffix, headers=owner).status_code == 404
    assert client.get('/owned').status_code == 404
    assert client.get('/api/links', headers=owner).json['links'] == []
    assert create(client, owner).status_code == 409
    with app.app_context():
        assert Link.query.one().deleted_at is not None
        assert Click.query.count() == 2


@pytest.mark.parametrize('body', [None, [], {}, {'slug': 'other'}, {'owner_id': 99}, {'one_time': True},
    {'disabled': 'false'}, {'target': 'ftp://bad'}, {'target': None}, {'ab_targets': []}, {'expires_at': 'bad'}])
def test_invalid_updates(client, body):
    owner = account(client)
    create(client, owner)
    assert client.patch('/api/links/owned', headers=owner, json=body).status_code == 400
    assert client.get('/owned').location == 'https://example.com'


def test_one_time_cannot_be_reenabled(client):
    owner = account(client)
    create(client, owner, one_time=True)
    assert client.get('/owned').status_code == 302
    assert client.patch('/api/links/owned', headers=owner, json={'disabled': False}).status_code == 409
    assert client.get('/owned').status_code == 404
    assert client.get('/api/links/owned/metrics', headers=owner).json['total'] == 1


def test_pagination_and_authentication(client):
    owner = account(client)
    for number in range(3):
        create(client, owner, slug=f'page{number}')
    first = client.get('/api/links?limit=2', headers=owner).json
    assert [link['slug'] for link in first['links']] == ['page2', 'page1']
    second = client.get('/api/links?limit=2&before=' + first['next_cursor'], headers=owner).json
    assert [link['slug'] for link in second['links']] == ['page0']
    assert second['next_cursor'] is None
    for query in ('limit=0', 'limit=101', 'limit=bad', 'before=bad', 'before=-1', 'before=999999999999999999999999'):
        assert client.get('/api/links?' + query, headers=owner).status_code == 400
    for verb in ('get', 'patch', 'delete'):
        assert getattr(client, verb)('/api/links/owned').status_code == 401


def test_ownership_foreign_key_constraints(client, app):
    from app.models import Link, User, db
    owner = account(client)
    create(client, owner)
    with app.app_context():
        assert db.session.execute(text('PRAGMA foreign_keys')).scalar() == 1
        db.session.add(Link(slug='invalid-fk', target='https://example.com', owner_id=999))
        with pytest.raises(IntegrityError): db.session.commit()
        db.session.rollback()
        db.session.delete(User.query.one())
        with pytest.raises(IntegrityError): db.session.commit()
        db.session.rollback()
        assert Link.query.count() == 1


def test_concurrent_owned_one_time_and_reenable(client, app):
    from app.models import Click, Link
    owner = account(client)
    create(client, owner, one_time=True)
    barrier = Barrier(6)
    def redeem(_):
        with app.test_client() as independent:
            barrier.wait(timeout=10)
            return independent.get('/owned').status_code
    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(redeem, range(6)))
    assert statuses.count(302) == 1
    assert statuses.count(404) == 5
    assert client.patch('/api/links/owned', headers=owner, json={'disabled': False}).status_code == 409
    with app.app_context():
        assert Click.query.count() == 1
        assert Link.query.one().disabled


def test_stale_update_does_not_overwrite_redemption(client, app):
    from app.models import db
    owner = account(client)
    create(client, owner, one_time=True)
    with app.app_context(): engine = db.engine
    triggered = False
    def concurrent_claim(connection, cursor, statement, parameters, context, many):
        nonlocal triggered
        if statement.startswith('UPDATE link SET') and not triggered:
            triggered = True
            # The PATCH has finished its read and released that transaction.
            with app.test_client() as independent:
                assert independent.get('/owned').status_code == 302
    event.listen(engine, 'before_cursor_execute', concurrent_claim)
    try:
        assert client.patch('/api/links/owned', headers=owner, json={'disabled': False}).status_code == 409
    finally:
        event.remove(engine, 'before_cursor_execute', concurrent_claim)
    assert client.get('/owned').status_code == 404
    assert client.get('/api/links/owned/metrics', headers=owner).json['total'] == 1


def test_management_commit_failure_rolls_back(client, app, monkeypatch):
    from app.models import Link, db
    from sqlalchemy.exc import OperationalError
    owner = account(client)
    create(client, owner)
    def unavailable():
        raise OperationalError('UPDATE', {}, RuntimeError('test commit failure'))
    with monkeypatch.context() as patch:
        patch.setattr(db.session, 'commit', unavailable)
        assert client.delete('/api/links/owned', headers=owner).status_code == 503
    with app.app_context():
        assert Link.query.one().deleted_at is None
        assert not Link.query.one().disabled
    assert client.get('/owned').status_code == 302


def test_deleted_one_time_never_claimed(client, app):
    from app.models import Click, Link
    owner = account(client)
    create(client, owner, one_time=True)
    assert client.delete('/api/links/owned', headers=owner).status_code == 204
    assert client.get('/owned').status_code == 404
    with app.app_context():
        assert Click.query.count() == 0
        assert Link.query.one().deleted_at is not None
