import os
from flask import Flask
from .models import db
from werkzeug.middleware.proxy_fix import ProxyFix
from .main import bp as main_bp

def create_app(config=None):
    app = Flask(__name__)

    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///smartlink.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev')

    app.config['TRUSTED_PROXY_HOPS'] = int(os.getenv('TRUSTED_PROXY_HOPS', '0'))
    app.config['ENABLE_DEBUG_IP'] = os.getenv('ENABLE_DEBUG_IP', '0') == '1'

    if config is not None:
        app.config.update(config)

    hops = app.config['TRUSTED_PROXY_HOPS']
    if type(hops) is not int or hops < 0:
        raise ValueError('TRUSTED_PROXY_HOPS must be a nonnegative integer')
    if hops:
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=hops, x_proto=0, x_host=0, x_port=0, x_prefix=0,
        )

    db.init_app(app)
    with app.app_context():
        db.create_all()

    app.register_blueprint(main_bp)
    return app

app = create_app()
