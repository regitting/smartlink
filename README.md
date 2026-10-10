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
- **SQLite** for lightweight development; **PostgreSQL** for shared database persistence.

---

## Getting Started

### Local development and tests
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest
alembic upgrade head
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

### Databases and explicit migrations
`DATABASE_URL` defaults to `sqlite:///smartlink.db`, stored at
`instance/smartlink.db` by Flask-SQLAlchemy. To use another SQLite file, supply
an absolute URL, e.g. `sqlite:////absolute/path/smartlink.db`. Creating or importing
the app no longer creates tables or connects to the database. Run migrations
explicitly before serving requests:
```bash
alembic upgrade head
flask --app wsgi:app db-check
flask --app wsgi:app run --port 8000
```
`/health` remains the liveness endpoint. `/api/ready` returns 200 only when the
database is reachable and its migration revision is current, otherwise 503.
The Docker image runs `db-check` before Gunicorn and fails with a clear error if
migration or database readiness is missing; it never applies migrations at startup.
PostgreSQL connections use pool pre-ping to recover stale pooled connections.

For PostgreSQL, set `DATABASE_URL` to a SQLAlchemy URL using
`postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE` before running the same
commands. `postgresql://` and `postgres://` also select psycopg 3. Percent-encode
special characters in URL credentials, keep credentials outside source control,
and use `sslmode=require` when your database requires TLS. SQLite and PostgreSQL
both store timestamps without timezone information, representing UTC.

### Docker Compose with PostgreSQL
Compose provides PostgreSQL 16, a `pg_isready` health check, and the persistent
`postgres_data` named volume. Store your environment values outside the repository
or supply them in your shell. For a temporary local development password:
```bash
export POSTGRES_PASSWORD="$(python -c 'import secrets; print(secrets.token_hex(24))')"
export DATABASE_URL="postgresql+psycopg://smartlink:${POSTGRES_PASSWORD}@db:5432/smartlink"
docker compose up -d db
docker compose run --rm migrate
docker compose up -d --build web
curl -f http://localhost:8000/api/ready
```
The database password must match the existing volume if restarting it; do not
regenerate it for a populated volume. Compose waits for the database to become
healthy before migration or web commands. The migration service is an explicit
one-off command, outside normal startup. If migrations fail, investigate the
failure before starting web. Use `docker compose down` to stop while retaining
data; do not add `-v` unless you intend to delete the database volume.

To run the Python app on the host against the Compose database, use the same URL
but replace `@db:` with `@localhost:`; port 5432 is bound only to localhost.
The web port is also bound only to localhost. The container and host can run the
same migrations, but run only one migration operation at a time.

For standalone SQLite Docker development, mount your own persistent directory at
`/app/instance`. On a fresh data directory:
```bash
docker build -t smartlink .
mkdir -p instance
docker run --rm -v "$PWD/instance:/app/instance" smartlink alembic upgrade head
docker run --rm -p 127.0.0.1:8000:8000 -v "$PWD/instance:/app/instance" smartlink
```
Do not run migrations against an existing unversioned database before following
the adoption steps below.

### Adopting migrations for an existing SQLite database
Migration `0001` represents the exact original Link/Click schema. Migration `0002`
adds `ix_click_link_id` for the three analytics queries filtering clicks by link.
The existing unique slug constraint already indexes redirect lookup; another slug
index would be redundant. No rows or tables are rebuilt by the index migration.

1. Stop writers and make a verified SQLite backup using SQLite's backup API or
   `.backup` (a raw file copy while writers run may miss WAL data). Keep the original.
2. Set `DATABASE_URL` to the existing file's absolute path. Verify that both tables,
   column types/nullability, primary keys, slug uniqueness, and the click-to-link
   foreign key match `0001_existing_schema.py`. If they differ, reconcile the
   differences on a copy before continuing. Confirm that no existing migration
   history or `ix_click_link_id` index is present.
3. For a matching unversioned database only, mark the baseline as already applied:
   ```bash
   alembic stamp 0001
   alembic upgrade head
   flask --app wsgi:app db-check
   ```
4. Verify links and click counts, then resume writers. Tests exercise this sequence
   against a legacy SQLite file and verify every existing row is preserved.

Do **not** stamp `head`: that would skip creating the analytics index. Stamping
only writes migration metadata; it does not validate or fix a schema. For an empty
new database, use `upgrade head` directly. Never rerun the initial creation migration
against existing tables. This workflow does not transfer SQLite rows to PostgreSQL;
a cross-database transfer requires a separately reviewed data export/import,
foreign-key checks, and PostgreSQL sequence resets after preserving existing IDs.
Previously stored expiry timestamps with discarded offsets cannot be reconstructed.

### Future schema changes and database tests
```bash
alembic current
alembic revision --autogenerate -m "describe schema change"
# Review the generated migration, then test it on a disposable database.
alembic upgrade head
python -m pytest
```
Commit reviewed revisions with model changes. Back up existing data and serialize
migration execution. Downgrading to `0001` removes only the analytics index;
downgrading to `base` drops tables and their data, so do not use it on retained data.

Real PostgreSQL tests are opt-in and fail on connection/setup errors once enabled:
```bash
# Use a disposable PostgreSQL database, with permission to create schemas.
export TEST_POSTGRES_URL="$DATABASE_URL"  # host runs require @localhost, not @db
python -m pytest tests/test_postgresql.py -v
```
These tests create and drop only randomly named test schemas. They test migrations,
API behavior, and six concurrent redemptions using independent connections.
Without `TEST_POSTGRES_URL`, they are explicitly skipped; offline PostgreSQL SQL
compilation alone is not an integration test. SQLite continues to serialize writers;
PostgreSQL's conditional row update preserves one-time atomicity under its default
READ COMMITTED isolation. Both databases can consume a committed link even if
subsequent HTTP response delivery fails. No AWS deployment changes are made here;
existing deployment operators must explicitly migrate before starting this version.

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