import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import jwt
import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

PASSWORD = 'correct horse battery staple'


def register(client, email='user@example.com', password=PASSWORD):
    return client.post('/api/auth/register', json={'email': email, 'password': password})


def login(client, email='user@example.com', password=PASSWORD):
    return client.post('/api/auth/login', json={'email': email, 'password': password})


def account(client, email='user@example.com'):
    assert register(client, email).status_code == 201
    response = login(client, email)
    assert response.status_code == 200
    return {'Authorization': 'Bearer ' + response.json['access_token']}


def test_registration_hashes_normalizes_and_duplicate(client, app):
    from app.models import User, db
    response = register(client, ' User@EXAMPLE.COM ')
    assert response.status_code == 201
    assert response.json['user']['email'] == 'user@example.com'
    assert set(response.json['user']) == {'id', 'email', 'created_at'}
    assert response.headers['Cache-Control'] == 'no-store'
    with app.app_context():
        stored = User.query.one().password_hash
        assert stored != PASSWORD
        assert stored.startswith('$argon2id$')
        assert PasswordHasher().verify(stored, PASSWORD)
    assert register(client, 'USER@example.com', 'a different valid password').status_code == 409
    assert login(client).status_code == 200
    assert login(client, password='a different valid password').status_code == 401
    with app.app_context():
        assert User.query.count() == 1
        assert User.query.one().password_hash == stored


@pytest.mark.parametrize('body', [None, [], {}, {'email': 'bad', 'password': PASSWORD},
    {'email': 4, 'password': PASSWORD}, {'email': 'user@example.com', 'password': False},
    {'email': 'user@example.com', 'password': 'short'}, {'email': 'user@example.com', 'password': 'a' * 129},
    {'email': 'user@example.com', 'password': PASSWORD, 'is_active': True},
    {'email': 'üser@example.com', 'password': PASSWORD}])
def test_invalid_registration(client, body):
    import json
    assert client.post('/api/auth/register', data=json.dumps(body), content_type='application/json').status_code == 400


def test_invalid_json(client):
    for path in ('register', 'login'):
        response = client.post('/api/auth/' + path, data='{', content_type='application/json')
        assert response.status_code == 400
        assert response.json['code'] == 'invalid_request'


def test_login_generic_errors_and_hash_salts(client, app):
    from app.models import User, db
    register(client)
    unknown = login(client, 'missing@example.com')
    wrong = login(client, password='incorrect valid length password')
    with app.app_context():
        User.query.one().is_active = False
        db.session.commit()
    inactive = login(client)
    assert unknown.status_code == wrong.status_code == inactive.status_code == 401
    assert unknown.json == wrong.json == inactive.json == {'error': 'Invalid email or password', 'code': 'invalid_credentials'}
    assert wrong.headers['WWW-Authenticate'] == 'Bearer'
    register(client, 'other@example.com')
    with app.app_context():
        hashes = [user.password_hash for user in User.query.all()]
        assert hashes[0] != hashes[1]


def test_login_token_and_logout_all(client, app):
    headers = account(client)
    other_token = login(client).json['access_token']
    token = headers['Authorization'].split()[1]
    claims = jwt.decode(token, app.config['JWT_SIGNING_KEY'], algorithms=['HS256'],
                        issuer='smartlink', audience='smartlink-api')
    assert claims['exp'] - claims['iat'] == 3600
    assert set(claims) == {'sub', 'iss', 'aud', 'iat', 'nbf', 'exp', 'jti', 'token_version'}
    assert client.get('/api/auth/me', headers=headers).json['user']['email'] == 'user@example.com'
    assert client.post('/api/auth/logout', headers=headers).status_code == 204
    assert client.get('/api/auth/me', headers=headers).status_code == 401
    assert client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + other_token}).status_code == 401
    fresh = login(client)
    assert fresh.json['expires_in'] == 3600
    assert client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + fresh.json['access_token']}).status_code == 200


@pytest.mark.parametrize('change', ['expired', 'future', 'issuer', 'audience', 'subject', 'version',
    'missing_exp', 'missing_jti', 'bool_exp', 'long_lifetime', 'bad_jti', 'deleted_user', 'inactive_user', 'wrong_key', 'algorithm', 'unsigned', 'tampered'])
def test_invalid_tokens(client, app, change):
    from app.models import User, db
    headers = account(client)
    claims = jwt.decode(headers['Authorization'].split()[1], app.config['JWT_SIGNING_KEY'],
                        algorithms=['HS256'], audience='smartlink-api')
    if change == 'expired':
        claims.update(iat=int(time.time()) - 7200, nbf=int(time.time()) - 7200, exp=int(time.time()) - 3600)
    elif change == 'future':
        claims.update(iat=int(time.time()) + 300, nbf=int(time.time()) + 300, exp=int(time.time()) + 3900)
    elif change == 'issuer': claims['iss'] = 'wrong'
    elif change == 'audience': claims['aud'] = 'wrong'
    elif change == 'subject': claims['sub'] = '999999999999999999999999999'
    elif change == 'version': claims['token_version'] += 1
    elif change == 'missing_exp': del claims['exp']
    elif change == 'missing_jti': del claims['jti']
    elif change == 'bool_exp': claims['exp'] = True
    elif change == 'long_lifetime': claims['exp'] += 10000
    elif change == 'bad_jti': claims['jti'] = 'bad'
    elif change in ('deleted_user', 'inactive_user'):
        with app.app_context():
            user = User.query.one()
            if change == 'deleted_user': db.session.delete(user)
            else: user.is_active = False
            db.session.commit()
    key = secrets.token_hex(32) if change == 'wrong_key' else app.config['JWT_SIGNING_KEY']
    algorithm = 'HS384' if change == 'algorithm' else 'HS256'
    if change == 'unsigned': key, algorithm = '', 'none'
    token = jwt.encode(claims, key, algorithm=algorithm)
    if change == 'tampered':
        head, payload, signature = token.split('.')
        token = '.'.join((head, payload, ('A' if signature[0] != 'A' else 'B') + signature[1:]))
    response = client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + token})
    assert response.status_code == 401
    assert response.json == {'error': 'Invalid access token', 'code': 'invalid_token'}
    assert response.headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('header', [None, '', 'Basic something', 'Bearer', 'Bearer one two', 'Bearer garbage'])
def test_missing_or_malformed_credentials(client, header):
    headers = {} if header is None else {'Authorization': header}
    assert client.get('/api/auth/me', headers=headers).status_code == 401
    assert client.get('/api/links', headers=headers).status_code == 401
    if header is not None:
        assert client.post('/api/links', headers=headers, json={'slug': 'bad', 'target': 'https://example.com'}).status_code == 401


def test_password_rehash(client, app):
    from app.models import User, db
    register(client)
    with app.app_context():
        old = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID).hash(PASSWORD)
        User.query.one().password_hash = old
        db.session.commit()
    assert login(client).status_code == 200
    with app.app_context():
        stored = User.query.one().password_hash
        assert stored != old
        assert not app.extensions['password_hasher'].check_needs_rehash(stored)


@pytest.mark.parametrize('key', [None, '', 'short'])
def test_missing_signing_key_fails_configuration(app, key):
    from app import create_app
    with pytest.raises(ValueError, match='JWT_SIGNING_KEY'):
        create_app({'JWT_SIGNING_KEY': key, 'TESTING': True})


@pytest.mark.parametrize('configuration', [
    {'APP_ENV': 'production', 'AUTH_RATE_LIMIT_MODE': 'local'},
    {'APP_ENV': 'production', 'AUTH_RATE_LIMIT_MODE': 'shared', 'RATELIMIT_STORAGE_URI': 'memory://'},
    {'AUTH_RATE_LIMIT_MODE': 'ingress', 'INGRESS_RATE_LIMITS_VERIFIED': False},
    {'AUTH_RATE_LIMIT_MODE': 'nonsense'},
    {'APP_ENV': 'production', 'AUTH_RATE_LIMIT_MODE': 'shared', 'RATELIMIT_STORAGE_URI': 'redis://localhost:6379', 'RATELIMIT_ENABLED': False},
])
def test_unsafe_limiter_configuration_rejected(app, configuration):
    from app import create_app
    with pytest.raises(ValueError):
        create_app({'TESTING': True, **configuration})


def test_verified_ingress_configuration(app):
    from app import create_app
    configured = create_app({'TESTING': True, 'APP_ENV': 'production',
                             'AUTH_RATE_LIMIT_MODE': 'ingress', 'INGRESS_RATE_LIMITS_VERIFIED': True})
    assert not configured.config['RATELIMIT_ENABLED']


def test_local_rate_limits(client, app):
    from app import create_app
    from app.models import db
    configured = create_app({
        'TESTING': True, 'SQLALCHEMY_DATABASE_URI': app.config['SQLALCHEMY_DATABASE_URI'],
        'RATELIMIT_ENABLED': True, 'LOGIN_ACCOUNT_LIMIT': '2/minute', 'LOGIN_IP_LIMIT': '100/minute',
        'REGISTER_IP_LIMIT': '2/minute', 'REGISTER_ACCOUNT_LIMIT': '100/hour',
        'ANONYMOUS_CREATE_LIMIT': '2/minute',
    })
    try:
        limited = configured.test_client()
        assert login(limited, 'missing@example.com').status_code == 401
        assert login(limited, 'missing@example.com').status_code == 401
        response = limited.post('/api/auth/login', json={'email': 'MISSING@EXAMPLE.COM', 'password': PASSWORD}, environ_base={'REMOTE_ADDR': '192.0.2.2'})
        assert response.status_code == 429
        assert response.json['code'] == 'rate_limited'
        assert 'Retry-After' in response.headers
        assert register(limited, 'one@example.com').status_code == 201
        assert register(limited, 'two@example.com').status_code == 201
        assert register(limited, 'three@example.com').status_code == 429
        for number in range(3):
            response = limited.post('/api/links', json={'slug': f'limited{number}', 'target': 'https://example.com'})
            assert response.status_code == (201 if number < 2 else 429)
    finally:
        with configured.app_context():
            db.session.remove()
            db.engine.dispose()


def test_concurrent_registration_unique(client, app):
    from app.models import User
    barrier = Barrier(4)
    def attempt(_):
        with app.test_client() as independent:
            barrier.wait(timeout=10)
            return register(independent, 'RACE@example.com').status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(attempt, range(4)))
    assert statuses.count(201) == 1
    assert statuses.count(409) == 3
    with app.app_context():
        assert User.query.count() == 1


def test_limiter_storage_failure_is_closed(client, app, monkeypatch):
    from app import create_app
    from app.models import db
    from limits.errors import StorageError
    configured = create_app({'TESTING': True, 'RATELIMIT_ENABLED': True,
                             'SQLALCHEMY_DATABASE_URI': app.config['SQLALCHEMY_DATABASE_URI']})
    def unavailable(*args, **kwargs):
        raise StorageError(RuntimeError('test backend unavailable'))
    try:
        monkeypatch.setattr(configured.extensions['smartlink_limiter'].limiter, 'hit', unavailable)
        response = login(configured.test_client(), 'missing@example.com')
        assert response.status_code == 503
        assert response.json['code'] == 'temporarily_unavailable'
    finally:
        with configured.app_context():
            db.session.remove()
            db.engine.dispose()


@pytest.mark.parametrize('setting, value', [('LOGIN_IP_LIMIT', 'bad'), ('REGISTER_ACCOUNT_LIMIT', '0/minute'), ('APP_ENV', 'prod')])
def test_invalid_security_settings(app, setting, value):
    from app import create_app
    with pytest.raises(ValueError):
        create_app({'TESTING': True, setting: value})


def test_real_shared_redis_counters(app):
    import os
    uri = os.getenv('TEST_REDIS_URL')
    if not uri:
        pytest.skip('TEST_REDIS_URL unset; real shared Redis limiter not run')
    from app import create_app
    from app.models import db
    options = {
        'TESTING': True, 'APP_ENV': 'production', 'AUTH_RATE_LIMIT_MODE': 'shared',
        'RATELIMIT_STORAGE_URI': uri, 'RATELIMIT_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': app.config['SQLALCHEMY_DATABASE_URI'],
        'LOGIN_ACCOUNT_LIMIT': '2/minute', 'LOGIN_IP_LIMIT': '100/minute',
    }
    apps = [create_app(options), create_app(options)]
    try:
        apps[0].extensions['smartlink_limiter'].reset()
        clients = [application.test_client() for application in apps]
        assert login(clients[0], 'shared@example.com').status_code == 401
        assert login(clients[1], 'shared@example.com').status_code == 401
        assert login(clients[0], 'SHARED@example.com').status_code == 429
    finally:
        apps[0].extensions['smartlink_limiter'].reset()
        for application in apps:
            with application.app_context():
                db.session.remove()
                db.engine.dispose()
