"""Bearer authentication, password verification, and explicit limiter modes."""
import hashlib
import re
import secrets
import time
from functools import wraps

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from email_validator import EmailNotValidError, validate_email
from flask import Blueprint, current_app, g, jsonify, request
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from limits.errors import StorageError
from limits import parse
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, OperationalError
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from .models import User, db

bp = Blueprint('auth', __name__, url_prefix='/api/auth')
TOKEN_SECONDS = 3600


class AuthError(Exception):
    def __init__(self, message, code, status=401):
        self.message, self.code, self.status = message, code, status


def error(message, code, status):
    return jsonify({'error': message, 'code': code}), status


def configure_auth(app):
    key = app.config.get('JWT_SIGNING_KEY')
    if not isinstance(key, str) or len(key.encode()) < 32:
        raise ValueError('JWT_SIGNING_KEY must be an independently generated secret of at least 32 bytes')
    for name in ('JWT_ISSUER', 'JWT_AUDIENCE'):
        if not isinstance(app.config[name], str) or not app.config[name]:
            raise ValueError(f'{name} must be nonempty')
    if app.config['APP_ENV'] not in ('development', 'production', 'testing'):
        raise ValueError('APP_ENV must be development, production, or testing')
    mode = app.config['AUTH_RATE_LIMIT_MODE']
    if mode not in ('local', 'shared', 'ingress'):
        raise ValueError('AUTH_RATE_LIMIT_MODE must be local, shared, or ingress')
    if mode == 'shared':
        uri = app.config.get('RATELIMIT_STORAGE_URI', '')
        if not uri or uri.startswith('memory:'):
            raise ValueError('shared rate limiting requires external storage')
    if mode == 'ingress' and not app.config['INGRESS_RATE_LIMITS_VERIFIED']:
        raise ValueError('ingress limiting requires INGRESS_RATE_LIMITS_VERIFIED=1')
    if app.config['APP_ENV'] == 'production':
        if mode == 'local' or (mode == 'shared' and not app.config['RATELIMIT_ENABLED']):
            raise ValueError('production requires shared rate limiting or verified ingress enforcement')
    if mode == 'local' and not app.config['TESTING']:
        app.logger.warning('Local memory rate limits are not production-safe across workers')
    app.config['RATELIMIT_STORAGE_URI'] = app.config.get('RATELIMIT_STORAGE_URI') if mode == 'shared' else 'memory://'
    app.config['RATELIMIT_ENABLED'] = app.config['RATELIMIT_ENABLED'] and mode != 'ingress'
    app.config['RATELIMIT_SWALLOW_ERRORS'] = False
    app.config['RATELIMIT_IN_MEMORY_FALLBACK_ENABLED'] = False
    app.config['RATELIMIT_HEADERS_ENABLED'] = True
    app.config['RATELIMIT_KEY_PREFIX'] = 'smartlink-auth'
    app.config['RATELIMIT_STORAGE_OPTIONS'] = {
        **app.config.get('RATELIMIT_STORAGE_OPTIONS', {}), 'wrap_exceptions': True,
    }
    for setting in ('LOGIN_IP_LIMIT', 'LOGIN_ACCOUNT_LIMIT', 'REGISTER_IP_LIMIT', 'REGISTER_ACCOUNT_LIMIT', 'ANONYMOUS_CREATE_LIMIT'):
        try:
            if parse(app.config[setting]).amount < 1:
                raise ValueError('limit must be positive')
        except (ValueError, TypeError):
            raise ValueError(f'{setting} must contain a valid positive rate limit') from None
    limiter = Limiter(key_func=lambda: request.remote_addr or 'unknown', default_limits=[])
    limiter.init_app(app)
    app.extensions['smartlink_limiter'] = limiter
    hasher = PasswordHasher()
    app.extensions['password_hasher'] = hasher
    # Precomputed once per app; unknown logins still perform a real verification.
    app.extensions['dummy_password_hash'] = hasher.hash(secrets.token_urlsafe(32))

    @app.errorhandler(AuthError)
    def auth_error(exc):
        response, status = error(exc.message, exc.code, exc.status)
        if status == 401:
            response.headers['WWW-Authenticate'] = 'Bearer'
        return response, status

    @app.errorhandler(RateLimitExceeded)
    def rate_error(exc):
        response, status = error('Too many requests', 'rate_limited', 429)
        # Conservative retry hint; limiter also supplies its configured headers.
        response.headers['Retry-After'] = '60'
        return response, status

    @app.errorhandler(StorageError)
    def limiter_unavailable(exc):
        return error('Rate limiter unavailable', 'temporarily_unavailable', 503)

    @app.errorhandler(OperationalError)
    def database_unavailable(exc):
        db.session.rollback()
        return error('Database unavailable; retry later', 'temporarily_unavailable', 503)

    @app.errorhandler(RequestEntityTooLarge)
    def body_too_large(exc):
        return error('Request body too large', 'invalid_request', 413)

    @app.after_request
    def protect_auth_responses(response):
        if response.status_code == 401:
            response.headers['WWW-Authenticate'] = 'Bearer'
        if request.path.startswith('/api/auth/'):
            response.headers['Cache-Control'] = 'no-store'
        return response


def limited(scope, setting, account=False):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if account:
                body = request.get_json(silent=True)
                email = body.get('email', '') if isinstance(body, dict) else ''
                email = email.strip().lower() if isinstance(email, str) else ''
                key = hashlib.sha256(email.encode('utf-8', errors='replace')).hexdigest()
            else:
                key = request.remote_addr or 'unknown'
            limiter = current_app.extensions['smartlink_limiter']
            with limiter.limit(current_app.config[setting], key_func=lambda: key, scope=scope):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def identity(required=True):
    if not getattr(g, 'identity_checked', False):
        g.identity_checked = True
        g.user = None
        g.user_id = None
        g.token_version = None
        header = request.headers.get('Authorization')
        if header is not None:
            parts = header.split()
            if len(parts) != 2 or parts[0].lower() != 'bearer' or len(parts[1]) > 8192:
                raise AuthError('Invalid access token', 'invalid_token')
            try:
                claims = jwt.decode(
                    parts[1], current_app.config['JWT_SIGNING_KEY'], algorithms=['HS256'],
                    issuer=current_app.config['JWT_ISSUER'], audience=current_app.config['JWT_AUDIENCE'],
                    leeway=30, options={'strict_aud': True, 'require': ['sub', 'iss', 'aud', 'iat', 'nbf', 'exp', 'jti', 'token_version']},
                )
                if not isinstance(claims['sub'], str) or not re.fullmatch(r'[1-9][0-9]{0,9}', claims['sub']):
                    raise ValueError('invalid subject')
                user_id = int(claims['sub'])
                if user_id > 2147483647:
                    raise ValueError('invalid subject')
                if any(type(claims[key]) is not int for key in ('iat', 'nbf', 'exp', 'token_version')):
                    raise ValueError('invalid claim type')
                if not (claims['iat'] <= claims['nbf'] < claims['exp'] and 0 < claims['exp'] - claims['iat'] <= TOKEN_SECONDS):
                    raise ValueError('invalid lifetime')
                if not isinstance(claims['jti'], str) or not re.fullmatch(r'[0-9a-f]{32}', claims['jti']):
                    raise ValueError('invalid token id')
            except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
                raise AuthError('Invalid access token', 'invalid_token') from None
            user = db.session.get(User, user_id)
            if user is None or not user.is_active or user.token_version != claims['token_version']:
                raise AuthError('Invalid access token', 'invalid_token')
            g.user, g.user_id, g.token_version = user, user.id, user.token_version
    if required and g.user_id is None:
        raise AuthError('Authentication required', 'authentication_required')
    return g.user


def protected(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        identity()
        return function(*args, **kwargs)
    return wrapped


def public_user(user):
    return {'id': user.id, 'email': user.email, 'created_at': user.created_at.isoformat() + 'Z'}


def credentials():
    try:
        body = request.get_json(force=True)
    except BadRequest:
        raise AuthError('Invalid JSON', 'invalid_request', 400) from None
    if not isinstance(body, dict) or set(body) != {'email', 'password'}:
        raise AuthError('email and password are required; other fields are unsupported', 'invalid_request', 400)
    email, password = body['email'], body['password']
    if not isinstance(email, str) or not isinstance(password, str) or not 15 <= len(password) <= 128:
        raise AuthError('Invalid email or password format', 'invalid_request', 400)
    try:
        password.encode('utf-8')
        email = validate_email(email.strip(), check_deliverability=False, allow_smtputf8=False).normalized.lower()
        email.encode('ascii')
        if len(email) > 254:
            raise ValueError('email too long')
    except (EmailNotValidError, UnicodeError, ValueError):
        raise AuthError('Invalid email address', 'invalid_request', 400) from None
    return email, password


@bp.post('/register')
@limited('register-ip', 'REGISTER_IP_LIMIT')
@limited('register-account', 'REGISTER_ACCOUNT_LIMIT', account=True)
def register():
    email, password = credentials()
    user = User(email=email, password_hash=current_app.extensions['password_hasher'].hash(password))
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if User.query.filter_by(email=email).first() is not None:
            return error('Email is already registered', 'email_conflict', 409)
        raise
    return jsonify({'user': public_user(user)}), 201


@bp.post('/login')
@limited('login-ip', 'LOGIN_IP_LIMIT')
@limited('login-account', 'LOGIN_ACCOUNT_LIMIT', account=True)
def login():
    email, password = credentials()
    user = User.query.filter_by(email=email).first()
    hasher = current_app.extensions['password_hasher']
    stored = user.password_hash if user else current_app.extensions['dummy_password_hash']
    try:
        valid = hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        valid = False
    if not valid or user is None or not user.is_active:
        return error('Invalid email or password', 'invalid_credentials', 401)
    if hasher.check_needs_rehash(stored):
        replacement = hasher.hash(password)
        # Do not overwrite a concurrently changed password or issue a stale token.
        changed = db.session.execute(update(User).where(User.id == user.id, User.password_hash == stored)
                           .values(password_hash=replacement).execution_options(synchronize_session=False)).rowcount
        db.session.commit()
        db.session.refresh(user)
        if changed != 1 or not user.is_active:
            return error('Invalid email or password', 'invalid_credentials', 401)
    now = int(time.time())
    token = jwt.encode({
        'sub': str(user.id), 'iss': current_app.config['JWT_ISSUER'], 'aud': current_app.config['JWT_AUDIENCE'],
        'iat': now, 'nbf': now, 'exp': now + TOKEN_SECONDS,
        'jti': secrets.token_hex(16), 'token_version': user.token_version,
    }, current_app.config['JWT_SIGNING_KEY'], algorithm='HS256')
    return jsonify({'access_token': token, 'token_type': 'Bearer', 'expires_in': TOKEN_SECONDS, 'user': public_user(user)})


@bp.get('/me')
@protected
def me():
    return jsonify({'user': public_user(g.user)})


@bp.post('/logout')
@protected
def logout():
    user_id = g.user_id
    # Release the authentication read before this atomic write on SQLite.
    db.session.rollback()
    db.session.execute(update(User).where(User.id == user_id)
                       .values(token_version=User.token_version + 1).execution_options(synchronize_session=False))
    db.session.commit()
    return '', 204
