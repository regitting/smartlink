from flask import Blueprint, request, redirect, jsonify, abort, send_file, current_app
from .models import db, Link, Click, utc_now
from .utils import lookup_country, parse_device, get_client_ip
from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError, OperationalError
from werkzeug.exceptions import BadRequest
import qrcode, io

bp = Blueprint('main', __name__)

@bp.post('/api/links')
def create_link():
    try:
        data = request.get_json(force=True)
        link = Link.from_json(data)
    except (BadRequest, ValueError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else 'invalid JSON'
        return jsonify({'error': message}), 400
    if Link.query.filter_by(slug=link.slug).first():
        return jsonify({'error': 'slug already exists'}), 409
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
                Link.one_time.is_(True),
                Link.disabled.is_(False),
                or_(Link.expires_at.is_(None), Link.expires_at > utc_now()),
            ).values(disabled=True).execution_options(synchronize_session=False)
        ).rowcount == 1
        query = Link.query.filter_by(slug=slug)
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
    link = Link.query.filter_by(slug=slug).first()
    if not link:
        return jsonify({'error': 'not found'}), 404
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
