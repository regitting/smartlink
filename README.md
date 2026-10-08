# Smartlink  
*A URL Shortener & Analytics Service*  

Smartlink is a lightweight Flask-based web service that lets you shorten links, generate QR codes, and track clicks by device and country. It’s containerized with Docker and deployed to **AWS ECS Fargate**, using **ECR** for image storage and **CloudWatch** for logging.  

---

## Features
- **Shorten Links** – create unique slugs for redirecting to target URLs.  
- **Click Analytics** – track device type, referrer, and geolocation.  
- **QR Code Generation** – instantly generate PNG QR codes for shortlinks.  
- **Health Monitoring** – `/health` endpoint for container health checks.  
- **RESTful API** – JSON-based endpoints for integration.  

---

## Architecture
Flask App → Docker Container → AWS ECR → ECS Fargate → CloudWatch Logs  
                          ↑  
                       SQLite DB (dev)  

- **ECS Fargate** runs the container serverless.  
- **Security Groups** restrict access to port 8000.  
- **CloudWatch Logs** capture application output.  
- **SQLite** for persistence (simple demo; production-ready swap with RDS).  

---

## Getting Started

### Local development and tests
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest
python -m flask --app wsgi:app run --port 8000
```

Tests use temporary SQLite databases and do not require GeoIP data or AWS access.
Link creation requires a slug of 1–64 letters, digits, underscores or hyphens
(`health` is reserved), and either `target` or a nonempty `ab_targets` URL list.
Destinations must use HTTP or HTTPS. Optional `one_time` is a JSON boolean.
Optional `expires_at` is an ISO 8601 timestamp; offsets are converted to UTC,
and timestamps without an offset are interpreted as UTC. SQLite stores UTC
without timezone information. Invalid requests return 400; duplicate slugs return 409.

### Proxy trust and concurrent redirects
`TRUSTED_PROXY_HOPS` defaults to `0`: forwarded headers are ignored and
analytics uses the direct peer IP. Set it to the exact number of trusted
reverse proxies supplying `X-Forwarded-For` (for example, `1`). Werkzeug selects
the corresponding value from the right. This is a hop count, not an IP allowlist:
only enable it when network access to the app is restricted to those proxies
and they sanitize or append forwarded headers. Forwarded host, scheme, port,
and prefix headers are not trusted. The same settings can be passed to `create_app`.
`ENABLE_DEBUG_IP` defaults to false; the existing `/_debug/ip` route returns 404
unless explicitly enabled. It has no authentication, so leave it disabled publicly.

Metrics aggregate all clicks in SQL and retain the existing response format.
One-time redirects conditionally update an enabled, unexpired link to disabled
and record the click in the same transaction. A losing request returns 404
without a click; transaction failure rolls back both operations. The update runs
before reads to avoid SQLite snapshot/lock upgrade failures. Regular links
continue returning 302 and recording each click.

SQLite serializes writers, including regular redirects (which also write clicks).
Requests wait up to the connection's busy timeout (sqlite3 defaults to 5 seconds);
if a lock outlasts it, redirects return 503 with `Retry-After: 1`, without a click
or consumed link. Configure the timeout via `SQLALCHEMY_ENGINE_OPTIONS` /
`connect_args.timeout` if needed. This requires a shared database with reliable
local filesystem locking across workers; separate SQLite files on different
instances cannot coordinate redemption. Consumption commits before the HTTP
response: if delivery fails afterward, the link remains consumed. No database
transaction can guarantee that a client receives the response exactly once.

### Run locally (Docker)
```bash
# Build image
docker build -t smartlink .

# Run container on localhost:8000
docker run -p 8000:8000 smartlink
```

## API Endpoints
```bash
curl http://localhost:8000/health
```
Response:
```bash
{"status": "ok"}
```

Create a Shortlink
```bash
curl -X POST http://localhost:8000/api/links \
  -H "Content-Type: application/json" \
  -d '{"slug":"hello","target":"https://example.com"}'
```
Response:
```bash
{"slug": "hello"}
```

Visit a Shortlink
```bash
curl -i http://localhost:8000/hello
```
Response:
```bash
HTTP/1.1 302 FOUND
Location: https://example.com
```

Metrics for a Shortlink:
```bash
curl http://localhost:8000/api/links/hello/metrics
```
Response:
```bash
{
  "total": 1,
  "by_device": {
    "desktop": 1
  },
  "by_country": {
    "Canada": 1
  }
}
```

Generate QR Code
```bash
curl -o qr.png -X POST http://localhost:8000/api/qr/hello
```
Output: saves qr.png in the current directory.

Debug IP (disabled by default; enable only in a controlled local environment
with `ENABLE_DEBUG_IP=1`)
```bash
curl http://localhost:8000/_debug/ip
```
Response:
```bash
{
  "ip": "203.0.113.25",
  "country": "Canada"
}
```

## Deployment (AWS ECS)

1. Build & push Docker image to ECR

2. Register ECS task definition (containerPort: 8000)

3. Run ECS Fargate service in public subnet with assignPublicIp=ENABLED

4. View logs in CloudWatch:
```bash
aws logs get-log-events --log-group-name /ecs/smartlink ...
```

Note: account IDs are replaced with <YOUR_ACCOUNT_ID> placeholders for security.