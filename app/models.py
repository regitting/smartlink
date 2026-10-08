from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
import json, random
import re
import ipaddress
from urllib.parse import urlsplit


def utc_naive(value):
    """SQLite stores naive datetimes; interpret naive input as UTC."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def utc_now():
    return utc_naive(datetime.now(timezone.utc))


def validate_target(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("destination must be a URL of at most 2048 characters")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ValueError("destination must be a valid HTTP/HTTPS URL")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        parsed.port  # Access validates numeric range and syntax.
    except ValueError:
        raise ValueError("destination must be a valid HTTP/HTTPS URL") from None
    if parsed.scheme.lower() not in ("http", "https") or not host:
        raise ValueError("destination must be a valid HTTP/HTTPS URL")
    try:
        if ':' in host:
            ipaddress.IPv6Address(host)
        else:
            ascii_host = host.rstrip('.').encode('idna').decode('ascii')
            if len(ascii_host) > 253 or any(
                not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                for label in ascii_host.split('.')
            ):
                raise ValueError('invalid hostname')
    except (ValueError, UnicodeError):
        raise ValueError('destination must be a valid HTTP/HTTPS URL') from None
    return value


db = SQLAlchemy()

class Link(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(64), unique=True, nullable=False)
    target = db.Column(db.String(2048))
    ab_targets_json = db.Column(db.Text)  # JSON list or null
    created_at = db.Column(db.DateTime, default=utc_now)
    expires_at = db.Column(db.DateTime, nullable=True)
    one_time = db.Column(db.Boolean, default=False)
    disabled = db.Column(db.Boolean, default=False)

    def is_expired(self):
        return self.expires_at is not None and utc_now() >= utc_naive(self.expires_at)

    def pick_target(self):
        if self.ab_targets_json:
            targets = json.loads(self.ab_targets_json)
            return random.choice(targets) if targets else self.target
        return self.target

    @classmethod
    def from_json(cls, data: dict):
        if not isinstance(data, dict):
            raise ValueError('request must be a JSON object')
        slug = data.get('slug')
        if not isinstance(slug, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', slug):
            raise ValueError('slug must contain 1–64 letters, digits, underscores or hyphens')
        if slug == 'health':
            raise ValueError('slug is reserved')
        target = data.get('target')
        if target is not None:
            validate_target(target)
        ab = data.get('ab_targets')
        if ab is not None:
            if not isinstance(ab, list) or not ab:
                raise ValueError('ab_targets must be a nonempty list of HTTP/HTTPS URLs')
            for destination in ab:
                validate_target(destination)
        if target is None and ab is None:
            raise ValueError('target or ab_targets is required')
        expires = data.get('expires_at')
        if expires is not None:
            if not isinstance(expires, str) or ('T' not in expires and ' ' not in expires):
                raise ValueError('expires_at must be an ISO 8601 timestamp')
            try:
                expires = utc_naive(datetime.fromisoformat(expires.replace('Z', '+00:00')))
            except (ValueError, OverflowError):
                raise ValueError('expires_at must be an ISO 8601 timestamp') from None
        one_time = data.get('one_time', False)
        if not isinstance(one_time, bool):
            raise ValueError('one_time must be a boolean')
        return cls(
            slug=slug,
            target=target,
            ab_targets_json=(None if ab is None else json.dumps(ab)),
            expires_at=expires,
            one_time=one_time,
            disabled=False
        )

class Click(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    link_id = db.Column(db.Integer, db.ForeignKey('link.id'), nullable=False)
    ts = db.Column(db.DateTime, default=utc_now)
    ip = db.Column(db.String(64))
    referrer = db.Column(db.String(2048))
    country = db.Column(db.String(64))
    device = db.Column(db.String(64))
