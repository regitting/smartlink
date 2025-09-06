from flask import Blueprint, request, redirect, jsonify, abort, send_file
from .models import db, Link, Click
from .utils import lookup_country, parse_device, get_client_ip
import qrcode, io

bp = Blueprint('main', __name__)

@bp.post('/api/links')
def create_link():
    data = request.get_json(force=True)
    if not data or 'slug' not in data:
        return jsonify({'error': 'slug is required'}), 400
    if Link.query.filter_by(slug=data['slug']).first():
        return jsonify({'error': 'slug already exists'}), 409
    link = Link.from_json(data)
    db.session.add(link); db.session.commit()
    return jsonify({'slug': link.slug}), 201

@bp.get('/<slug>')
def go(slug):
    link = Link.query.filter_by(slug=slug, disabled=False).first()
    if not link or link.is_expired():
        abort(404)

    target = link.pick_target()

    # --- use helper to resolve client IP (X-Forwarded-For or remote_addr)
    ip = get_client_ip(request)

    c = Click(
        link_id=link.id,
        ip=ip,
        referrer=request.referrer,
        country=lookup_country(ip),
        device=parse_device(request.headers.get('User-Agent', ''))
    )
    db.session.add(c); db.session.commit()

    if link.one_time:
        link.disabled = True
        db.session.commit()

    return redirect(target, code=302)

@bp.get('/api/links/<slug>/metrics')
def metrics(slug):
    link = Link.query.filter_by(slug=slug).first()
    if not link:
        return jsonify({'error': 'not found'}), 404
    clicks = (Click.query
              .filter_by(link_id=link.id)
              .order_by(Click.ts.desc())
              .limit(1000)
              .all())
    total = len(clicks)
    by_device, by_country = {}, {}
    for c in clicks:
        d = c.device or 'unknown'
        by_device[d] = by_device.get(d, 0) + 1
        co = c.country or 'unknown'
        by_country[co] = by_country.get(co, 0) + 1
    return jsonify({'total': total, 'by_device': by_device, 'by_country': by_country})

@bp.post('/api/qr/<slug>')
def qr(slug):
    url = request.host_url.rstrip('/') + '/' + slug
    img = qrcode.make(url)
    buf = io.BytesIO(); img.save(buf, format='PNG'); buf.seek(0)
    return send_file(buf, mimetype='image/png')

@bp.get('/_debug/ip')
def debug_ip():
    ip = get_client_ip(request)
    return jsonify({'ip': ip, 'country': lookup_country(ip)})

@bp.get('/health')
def health():
    return jsonify({'status': 'ok'}), 200

@bp.get("/")
def root():
    return "Smartlink is live", 200