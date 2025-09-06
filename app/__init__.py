import os
from flask import Flask
from .models import db
from werkzeug.middleware.proxy_fix import ProxyFix
from .main import bp as main_bp

def create_app():
    app = Flask(__name__)

    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///smartlink.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev')

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1) #trust proxy headers

    db.init_app(app)
    with app.app_context():
        db.create_all()

    app.register_blueprint(main_bp)
    return app

app = create_app()
