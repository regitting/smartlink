import os
from user_agents import parse

def get_client_ip(req):
    xff = req.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return req.remote_addr

_reader = None
def _get_reader():
    global _reader
    if _reader is not None:
        return _reader
    path = os.getenv("GEOIP_DB_PATH", os.path.join(os.path.dirname(__file__), "GeoLite2-Country.mmdb"))
    if os.path.exists(path):
        import geoip2.database
        _reader = geoip2.database.Reader(path)
    else:
        _reader = False
    return _reader

def lookup_country(ip: str):
    r = _get_reader()
    if not r or not ip:
        return None
    try:
        return r.country(ip).country.iso_code
    except Exception:
        return None

def parse_device(ua_string):
    ua = parse(ua_string or "")
    if ua.is_mobile: return "mobile"
    if ua.is_tablet: return "tablet"
    if ua.is_pc: return "desktop"
    if ua.is_bot: return "bot"
    return "unknown"
