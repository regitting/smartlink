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

Debug IP
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