from flask import Blueprint, request, redirect, jsonify, abort, send_file, current_app, g
from .models import db, Link, Click, utc_now
from .auth import identity, protected, limited, error
from .utils import lookup_country, parse_device, get_client_ip
from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError, OperationalError
from werkzeug.exceptions import BadRequest
import qrcode, io

bp = Blueprint('main', __name__)

@bp.post('/api/links')
def create_link():
    identity(required=False)
    if g.user_id is None:
        if not current_app.config['ALLOW_ANONYMOUS_LINK_CREATION']:
            return error('Authentication required', 'authentication_required', 401)
        return create_anonymous_link()
    return save_link()


@limited('anonymous-create', 'ANONYMOUS_CREATE_LIMIT')
def create_anonymous_link():
    return save_link()


def save_link():
    try:
        data = request.get_json(force=True)
        if isinstance(data, dict) and any(key in data for key in ('owner_id', 'user_id', 'owner', 'deleted_at', 'disabled', 'id')):
            raise ValueError('ownership and internal fields cannot be supplied')
        link = Link.from_json(data)
    except (BadRequest, ValueError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else 'invalid JSON'
        return jsonify({'error': message}), 400
    if Link.query.filter_by(slug=link.slug).first():
        return jsonify({'error': 'slug already exists'}), 409
    link.owner_id = g.user_id
    db.session.rollback()
    db.session.add(link)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'slug already exists'}), 409
    return jsonify({'slug': link.slug}), 201

@bp.get('/<slug>')
def go(slug):
    try:
        # Write first: avoids SQLite read-to-write lock upgrades. Only one
        # transaction can change an eligible one-time link from enabled to disabled.
        claimed = db.session.execute(
            update(Link).where(
                Link.slug == slug,
                Link.deleted_at.is_(None),
                Link.one_time.is_(True),
                Link.disabled.is_(False),
                or_(Link.expires_at.is_(None), Link.expires_at > utc_now()),
            ).values(disabled=True).execution_options(synchronize_session=False)
        ).rowcount == 1
        query = Link.query.filter_by(slug=slug, deleted_at=None)
        if not claimed:
            query = query.filter_by(disabled=False, one_time=False)
        link = query.first()
        if not link or link.is_expired():
            db.session.rollback()
            abort(404)

        target = link.pick_target()
        ip = get_client_ip(request)
        db.session.add(Click(
            link_id=link.id,
            ip=ip,
            referrer=request.referrer,
            country=lookup_country(ip),
            device=parse_device(request.headers.get('User-Agent', '')),
        ))
        # A failed click insert also rolls back the one-time claim.
        db.session.commit()
    except OperationalError as exc:
        db.session.rollback()
        if db.engine.dialect.name == 'sqlite' and str(exc.orig).lower() in (
            'database is locked', 'database table is locked',
        ):
            return jsonify({'error': 'database busy; retry later'}), 503, {'Retry-After': '1'}
        raise
    except Exception:
        db.session.rollback()
        raise
    return redirect(target, code=302)

@bp.get('/api/links/<slug>/metrics')
def metrics(slug):
    identity(required=False)
    if g.user_id is None:
        if not current_app.config['ALLOW_ANONYMOUS_ANALYTICS']:
            return error('Authentication required', 'authentication_required', 401)
        link = Link.query.filter_by(slug=slug, owner_id=None, deleted_at=None).first()
        if not link:
            # Do not distinguish a private slug from a nonexistent slug.
            return error('not found', 'not_found', 404)
    else:
        link = Link.query.filter(Link.slug == slug, Link.deleted_at.is_(None),
                                 or_(Link.owner_id == g.user_id, Link.owner_id.is_(None))).first()
        if not link or (link.owner_id is None and not current_app.config['ALLOW_ANONYMOUS_ANALYTICS']):
            return error('not found', 'not_found', 404)
    total = db.session.query(func.count(Click.id)).filter_by(link_id=link.id).scalar()

    def grouped_counts(column):
        # Preserve the old treatment of both NULL and empty strings as unknown.
        label = func.coalesce(func.nullif(column, ''), 'unknown')
        return dict(db.session.query(label, func.count(Click.id))
                    .filter(Click.link_id == link.id).group_by(label).all())

    by_device = grouped_counts(Click.device)
    by_country = grouped_counts(Click.country)
    return jsonify({'total': total, 'by_device': by_device, 'by_country': by_country})

@bp.post('/api/qr/<slug>')
def qr(slug):
    url = request.host_url.rstrip('/') + '/' + slug
    img = qrcode.make(url)
    buf = io.BytesIO(); img.save(buf, format='PNG'); buf.seek(0)
    return send_file(buf, mimetype='image/png')

@bp.get('/_debug/ip')
def debug_ip():
    if not current_app.config['ENABLE_DEBUG_IP']:
        abort(404)
    ip = get_client_ip(request)
    return jsonify({'ip': ip, 'country': lookup_country(ip)})

@bp.get('/health')
def health():
    return jsonify({'status': 'ok'}), 200

@bp.get("/")
def root():
    return "Smartlink is live", 200

@bp.get('/api/ready')
def ready():
    from .database import check_database
    from sqlalchemy.exc import SQLAlchemyError
    try:
        check_database()
    except (SQLAlchemyError, RuntimeError):
        return jsonify({'status': 'not ready'}), 503
    return jsonify({'status': 'ok'}), 200


def link_json(link):
    import json
    return {
        'slug': link.slug, 'target': link.target,
        'ab_targets': json.loads(link.ab_targets_json) if link.ab_targets_json else None,
        'expires_at': link.expires_at.isoformat() + 'Z' if link.expires_at else None,
        'one_time': link.one_time, 'disabled': link.disabled,
        'created_at': link.created_at.isoformat() + 'Z',
    }


def owned_link(slug):
    return Link.query.filter_by(slug=slug, owner_id=g.user_id, deleted_at=None).first()


@bp.get('/api/links')
@protected
def list_links():
    try:
        limit = int(request.args.get('limit', '20'))
        before = request.args.get('before')
        if not 1 <= limit <= 100 or (before is not None and (not before.isascii() or not before.isdigit() or not 0 < int(before) <= 2147483647)):
            raise ValueError()
    except ValueError:
        return error('Invalid pagination', 'invalid_request', 400)
    query = Link.query.filter_by(owner_id=g.user_id, deleted_at=None)
    if before is not None:
        query = query.filter(Link.id < int(before))
    links = query.order_by(Link.id.desc()).limit(limit + 1).all()
    return jsonify({'links': [link_json(link) for link in links[:limit]],
                    'next_cursor': str(links[limit - 1].id) if len(links) > limit else None})


@bp.get('/api/links/<slug>')
@protected
def get_link(slug):
    link = owned_link(slug)
    if link is None:
        return error('not found', 'not_found', 404)
    return jsonify(link_json(link))


@bp.patch('/api/links/<slug>')
@protected
def update_link(slug):
    link = owned_link(slug)
    if link is None:
        return error('not found', 'not_found', 404)
    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict) or not data or set(data) - {'target', 'ab_targets', 'expires_at', 'disabled'}:
            raise ValueError('unsupported or missing update fields')
        if 'disabled' in data and type(data['disabled']) is not bool:
            raise ValueError('disabled must be a boolean')
        if data.get('disabled') is False and link.one_time and link.disabled:
            return error('One-time links cannot be re-enabled', 'invalid_state', 409)
        import json
        merged = {'slug': link.slug, 'target': link.target,
                  'ab_targets': json.loads(link.ab_targets_json) if link.ab_targets_json else None,
                  'expires_at': link.expires_at.isoformat() if link.expires_at else None}
        merged.update({key: value for key, value in data.items() if key != 'disabled'})
        validated = Link.from_json(merged)
        values = {}
        for field in ('target', 'ab_targets_json', 'expires_at'):
            if field in data or (field == 'ab_targets_json' and 'ab_targets' in data):
                values[field] = getattr(validated, field)
        if 'disabled' in data:
            values['disabled'] = data['disabled']
        # Optimistic conditional write prevents overwriting concurrent edits or
        # re-enabling a one-time link consumed since it was read.
        predicates = [Link.slug == slug, Link.owner_id == g.user_id, Link.deleted_at.is_(None)]
        for field in ('target', 'ab_targets_json', 'expires_at', 'disabled'):
            predicates.append(getattr(Link, field) == getattr(link, field))
    except (BadRequest, ValueError) as exc:
        return error(str(exc) if isinstance(exc, ValueError) else 'Invalid JSON', 'invalid_request', 400)
    db.session.rollback()
    changed = db.session.execute(update(Link).where(*predicates).values(**values)
                                 .execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        db.session.rollback()
        if owned_link(slug) is None:
            return error('not found', 'not_found', 404)
        return error('Link changed; retry your request', 'conflict', 409)
    response_data = link_json(owned_link(slug))
    db.session.commit()
    return jsonify(response_data)


@bp.delete('/api/links/<slug>')
@protected
def delete_link(slug):
    user_id = g.user_id
    db.session.rollback()
    changed = db.session.execute(update(Link).where(
        Link.slug == slug, Link.owner_id == user_id, Link.deleted_at.is_(None),
    ).values(deleted_at=utc_now(), disabled=True).execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        db.session.rollback()
        return error('not found', 'not_found', 404)
    db.session.commit()
    return '', 204
