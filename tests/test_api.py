import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from sqlalchemy.exc import IntegrityError
from werkzeug.urls import iri_to_uri

@pytest.fixture(autouse=True)
def models(app):
    global Link, Click, db, utc_now
    from app.models import Link, Click, db, utc_now


def create(client, **fields):
    return client.post('/api/links', json={'slug': 'hello', 'target': 'https://example.com', **fields})


def test_health_and_root(client):
    assert client.get('/health').json == {'status': 'ok'}
    assert client.get('/').status_code == 200


def test_creation_duplicate_and_redirect(client, app):
    response = create(client)
    assert response.status_code == 201
    assert response.json == {'slug': 'hello'}
    assert create(client).status_code == 409
    response = client.get('/hello')
    assert response.status_code == 302
    assert response.location == 'https://example.com'
    with app.app_context():
        assert Link.query.count() == 1
        assert Click.query.count() == 1


@pytest.mark.parametrize('payload', [
    None, [], 'hello', 42, {}, {'slug': 'hello'},
    {'slug': '', 'target': 'https://example.com'},
    {'slug': 1, 'target': 'https://example.com'},
    {'slug': 'a' * 65, 'target': 'https://example.com'},
    {'slug': 'has space', 'target': 'https://example.com'},
    {'slug': 'a/b', 'target': 'https://example.com'},
    {'slug': 'health', 'target': 'https://example.com'},
    {'slug': 'hello', 'url': 'https://example.com'},
])
def test_invalid_objects(client, app, payload):
    response = client.post('/api/links', data=json.dumps(payload), content_type='application/json')
    assert response.status_code == 400
    assert 'error' in response.json
    with app.app_context():
        assert Link.query.count() == 0


@pytest.mark.parametrize('target', ['', 42, [], {}, 'ftp://example.com', 'javascript:alert(1)',
                                    '/relative', 'https://', 'https://a:invalid', 'http://[bad', 'http://exa%mple.com', 'http://-bad.com',
                                    'https://exa mple.com', 'https://example.com\n',
                                    'https://example.com\\evil', 'https://example.com/' + 'a' * 2048])
def test_invalid_destinations(client, target):
    assert create(client, target=target).status_code == 400


@pytest.mark.parametrize('expiry', ['', 42, [], 'tomorrow', '2026-01-01', '2026-99-01T00:00:00', '2026-01-01T00:00:00+99:00', '0001-01-01T00:00:00+01:00'])
def test_invalid_expiry(client, expiry):
    assert create(client, expires_at=expiry).status_code == 400


@pytest.mark.parametrize('targets', [[], 'https://example.com', {}, [None], ['https://example.com', 'ftp://bad'], [42]])
def test_invalid_ab_targets(client, targets):
    assert create(client, ab_targets=targets).status_code == 400


@pytest.mark.parametrize('value', ['false', 1, None, []])
def test_invalid_one_time(client, value):
    assert create(client, one_time=value).status_code == 400


def test_malformed_json(client):
    response = client.post('/api/links', data='{', content_type='application/json')
    assert response.status_code == 400
    assert response.json == {'error': 'invalid JSON'}


@pytest.mark.parametrize('target', ['http://localhost:8000/path?q=1#frag', 'https://example.com/path', 'https://[::1]:8000', 'https://例え.jp/path'])
def test_supported_destinations(client, target):
    assert create(client, slug='Valid_slug-1', target=target).status_code == 201
    assert client.get('/Valid_slug-1').location == iri_to_uri(target)


@pytest.mark.parametrize('with_fallback', [False, True])
def test_ab_redirects(client, monkeypatch, with_fallback):
    targets = ['https://example.com/a', 'https://example.com/b']
    payload = {'slug': 'ab', 'ab_targets': targets}
    if with_fallback:
        payload['target'] = 'https://example.com/fallback'
    assert client.post('/api/links', json=payload).status_code == 201
    for destination in targets:
        monkeypatch.setattr('app.models.random.choice', lambda values: destination)
        assert client.get('/ab').location == destination


@pytest.mark.parametrize('expired', [True, False])
@pytest.mark.parametrize('style', ['utc', 'offset', 'naive'])
def test_expiry_roundtrip(client, app, expired, style):
    instant = datetime.now(timezone.utc) + timedelta(days=-1 if expired else 1)
    if style == 'offset':
        value = instant.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()
    elif style == 'naive':
        value = instant.replace(tzinfo=None).isoformat()
    else:
        value = instant.isoformat().replace('+00:00', 'Z')
    assert create(client, expires_at=value).status_code == 201
    with app.app_context():
        stored = Link.query.one().expires_at
        assert stored.tzinfo is None
        assert stored == instant.replace(tzinfo=None)
    assert client.get('/hello').status_code == (404 if expired else 302)
    assert client.get('/api/links/hello/metrics').json['total'] == (0 if expired else 1)


def test_expiry_boundary(monkeypatch):
    now = utc_now()
    monkeypatch.setattr('app.models.utc_now', lambda: now)
    assert Link(expires_at=now).is_expired()
    assert Link(expires_at=now.replace(tzinfo=timezone.utc)).is_expired()
    assert not Link().is_expired()


def test_disabled_and_missing_links(client, app):
    assert create(client).status_code == 201
    with app.app_context():
        Link.query.one().disabled = True
        db.session.commit()
    assert client.get('/hello').status_code == 404
    assert client.get('/missing').status_code == 404
    assert client.get('/api/links/hello/metrics').json['total'] == 0
    assert client.get('/api/links/missing/metrics').status_code == 404


def test_one_time(client):
    assert create(client, one_time=True).status_code == 201
    assert client.get('/hello').status_code == 302
    assert client.get('/hello').status_code == 404
    assert client.get('/api/links/hello/metrics').json['total'] == 1


def test_qr(client):
    assert create(client).status_code == 201
    response = client.post('/api/qr/hello')
    assert response.status_code == 200
    assert response.mimetype == 'image/png'
    image = Image.open(io.BytesIO(response.data))
    assert image.format == 'PNG'
    image.verify()


def test_metrics(client, app, monkeypatch):
    monkeypatch.setattr('app.main.lookup_country', lambda ip: 'CA')
    assert create(client).status_code == 201
    assert client.get('/api/links/hello/metrics').json == {'total': 0, 'by_device': {}, 'by_country': {}}
    client.get('/hello', headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Referer': 'https://ref.example'}, environ_base={'REMOTE_ADDR': '203.0.113.1'})
    monkeypatch.setattr('app.main.lookup_country', lambda ip: None)
    client.get('/hello')
    assert client.get('/api/links/hello/metrics').json == {
        'total': 2, 'by_device': {'desktop': 1, 'unknown': 1}, 'by_country': {'CA': 1, 'unknown': 1},
    }
    with app.app_context():
        click = Click.query.filter_by(device='desktop').one()
        assert click.ip == '203.0.113.1'
        assert click.referrer == 'https://ref.example'


def test_duplicate_race_rolls_back(client, app, monkeypatch):
    def conflict():
        raise IntegrityError('INSERT', {}, Exception('unique constraint'))
    with monkeypatch.context() as patch:
        patch.setattr(db.session, 'commit', conflict)
        assert create(client).status_code == 409
    assert create(client).status_code == 201
