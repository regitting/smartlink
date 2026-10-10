import os
from flask import Flask
from .models import db
from .database import database_url, register_database_commands
from werkzeug.middleware.proxy_fix import ProxyFix
from .main import bp as main_bp
from .auth import bp as auth_bp, configure_auth

def create_app(config=None):
    app = Flask(__name__)

    app.config['SQLALCHEMY_DATABASE_URI'] = database_url(os.getenv('DATABASE_URL', 'sqlite:///smartlink.db'))
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev')

    app.config['TRUSTED_PROXY_HOPS'] = int(os.getenv('TRUSTED_PROXY_HOPS', '0'))
    app.config['ENABLE_DEBUG_IP'] = os.getenv('ENABLE_DEBUG_IP', '0') == '1'

    app.config.update(
        JWT_SIGNING_KEY=os.getenv('JWT_SIGNING_KEY'),
        JWT_ISSUER=os.getenv('JWT_ISSUER', 'smartlink'),
        JWT_AUDIENCE=os.getenv('JWT_AUDIENCE', 'smartlink-api'),
        APP_ENV=os.getenv('APP_ENV', 'development'),
        AUTH_RATE_LIMIT_MODE=os.getenv('AUTH_RATE_LIMIT_MODE', 'local'),
        INGRESS_RATE_LIMITS_VERIFIED=os.getenv('INGRESS_RATE_LIMITS_VERIFIED', '0') == '1',
        RATELIMIT_STORAGE_URI=os.getenv('RATELIMIT_STORAGE_URI'),
        RATELIMIT_ENABLED=True,
        LOGIN_IP_LIMIT=os.getenv('LOGIN_IP_LIMIT', '30/minute'),
        LOGIN_ACCOUNT_LIMIT=os.getenv('LOGIN_ACCOUNT_LIMIT', '5/minute'),
        REGISTER_IP_LIMIT=os.getenv('REGISTER_IP_LIMIT', '10/minute'),
        REGISTER_ACCOUNT_LIMIT=os.getenv('REGISTER_ACCOUNT_LIMIT', '3/hour'),
        ANONYMOUS_CREATE_LIMIT=os.getenv('ANONYMOUS_CREATE_LIMIT', '60/minute'),
        ALLOW_ANONYMOUS_LINK_CREATION=os.getenv('ALLOW_ANONYMOUS_LINK_CREATION', '1') == '1',
        ALLOW_ANONYMOUS_ANALYTICS=os.getenv('ALLOW_ANONYMOUS_ANALYTICS', '1') == '1',
        MAX_CONTENT_LENGTH=1024 * 1024,
    )

    if config is not None:
        app.config.update(config)

    app.config['SQLALCHEMY_DATABASE_URI'] = database_url(app.config['SQLALCHEMY_DATABASE_URI'])
    engine_options = {'pool_pre_ping': True, 'hide_parameters': True}
    if app.config['SQLALCHEMY_DATABASE_URI'].get_backend_name() == 'postgresql':
        engine_options['connect_args'] = {'connect_timeout': 5}
    app.config.setdefault('SQLALCHEMY_ENGINE_OPTIONS', engine_options)

    hops = app.config['TRUSTED_PROXY_HOPS']
    if type(hops) is not int or hops < 0:
        raise ValueError('TRUSTED_PROXY_HOPS must be a nonnegative integer')
    if hops:
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=hops, x_proto=0, x_host=0, x_port=0, x_prefix=0,
        )

    db.init_app(app)
    register_database_commands(app)
    configure_auth(app)

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    return app

app = create_app()
