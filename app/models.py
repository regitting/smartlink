from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
import json, random

db = SQLAlchemy()

class Link(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(64), unique=True, nullable=False)
    target = db.Column(db.String(2048))
    ab_targets_json = db.Column(db.Text)  # JSON list or null
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = db.Column(db.DateTime, nullable=True)
    one_time = db.Column(db.Boolean, default=False)
    disabled = db.Column(db.Boolean, default=False)

    def is_expired(self):
        return self.expires_at and datetime.now(timezone.utc) > self.expires_at

    def pick_target(self):
        if self.ab_targets_json:
            targets = json.loads(self.ab_targets_json)
            return random.choice(targets) if targets else self.target
        return self.target

    @classmethod
    def from_json(cls, data: dict):
        ab = data.get('ab_targets')
        expires = data.get('expires_at')
        return cls(
            slug=data['slug'],
            target=data.get('target'),
            ab_targets_json=(None if not ab else json.dumps(ab)),
            expires_at=(None if not expires else datetime.fromisoformat(expires.replace('Z','+00:00'))),
            one_time=bool(data.get('one_time', False)),
            disabled=False
        )

class Click(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    link_id = db.Column(db.Integer, db.ForeignKey('link.id'), nullable=False)
    ts = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    ip = db.Column(db.String(64))
    referrer = db.Column(db.String(2048))
    country = db.Column(db.String(64))
    device = db.Column(db.String(64))
