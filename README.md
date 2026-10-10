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
# Generate once and retain outside source control; reuse across restarts/workers.
export JWT_SIGNING_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
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
export JWT_SIGNING_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
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
docker run --rm -e JWT_SIGNING_KEY -v "$PWD/instance:/app/instance" smartlink alembic upgrade head
docker run --rm -e JWT_SIGNING_KEY -p 127.0.0.1:8000:8000 -v "$PWD/instance:/app/instance" smartlink
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

## Authentication and authorization
Accounts use Argon2id password hashes and HS256 bearer JWTs with a **60-minute**
access lifetime. No refresh tokens are issued. Every authenticated request checks
user activity and the token version in the database. `/api/auth/logout` revokes
**all tokens for the user**, not just the supplied token. Requests already in
progress may finish. Signing-key rotation immediately invalidates existing tokens.

### Configuration
`JWT_SIGNING_KEY` is mandatory for app startup and migration commands. Generate
at least 32 random bytes independently of `SECRET_KEY`, retain the key outside
source control, and provide the same key to every worker. Never use `SECRET_KEY=dev`
as the signing key. Keep `DATABASE_URL`, limiter credentials, and tokens out of
logs; the default database engine hides query parameter values.

| Variable | Default / policy |
| --- | --- |
| `JWT_SIGNING_KEY` | Required; at least 32 bytes, generated cryptographically |
| `JWT_ISSUER` / `JWT_AUDIENCE` | `smartlink` / `smartlink-api`; validated on every token |
| `APP_ENV` | `development`; set explicitly to `production` when deploying |
| `AUTH_RATE_LIMIT_MODE` | `local`, `shared`, or `ingress`; defaults to `local` |
| `RATELIMIT_STORAGE_URI` | Required in `shared` mode, e.g. an external Redis URL |
| `INGRESS_RATE_LIMITS_VERIFIED` | `0`; set to `1` only after verifying ingress enforcement |
| `LOGIN_IP_LIMIT` / `LOGIN_ACCOUNT_LIMIT` | `30/minute` / `5/minute` |
| `REGISTER_IP_LIMIT` / `REGISTER_ACCOUNT_LIMIT` | `10/minute` / `3/hour` |
| `ANONYMOUS_CREATE_LIMIT` | `60/minute` |
| `ALLOW_ANONYMOUS_LINK_CREATION` | `1`; set `0` to require login for creation |
| `ALLOW_ANONYMOUS_ANALYTICS` | `1`; set `0` to stop public anonymous analytics |

Passwords are 15–128 characters. Spaces and Unicode are supported, without
trimming, normalization, composition rules, or truncation. Emails are ASCII,
validated syntactically, trimmed, and lowercased for identity; dots and plus tags
are retained. Lowercasing treats rare case-sensitive mailboxes as the same account.
Email addresses are **not verified**. This PR does not provide password recovery,
email changes, account deletion, MFA, or administrative roles.

### Rate limits: local versus production
Local development uses process-local memory and logs a warning. Tests normally
disable limits and separately test enforcement. **Memory is not production-safe**:
Compose/Gunicorn workers do not share counters, and restarting a process resets them.
`APP_ENV=production` refuses local memory limiting.

For application-level shared limits, set `AUTH_RATE_LIMIT_MODE=shared` and an
external `RATELIMIT_STORAGE_URI`, such as Redis with authentication/TLS as required
by your service. Redis is supported but not mandatory: compatible shared backends
supported by Flask-Limiter can be configured with their appropriate drivers.
Backend failures fail closed with 503; there is no memory fallback. Two independent
app instances must share counters. Limit normalized accounts as well as source IPs,
and use the existing trusted-proxy settings so clients cannot spoof the limiter IP.

Alternatively set `AUTH_RATE_LIMIT_MODE=ingress` and
`INGRESS_RATE_LIMITS_VERIFIED=1` when an equivalent shared ingress solution enforces
registration/login limits per normalized account and IP, plus anonymous creation
limits. This setting disables application limiting; it is an operator assertion,
not proof of protection. Restrict direct backend access so the ingress cannot be
bypassed, test enforcement across workers/instances, and ensure ingress responses
use the documented JSON 429 contract and `Retry-After`. IP-only throttling is not
equivalent to account-and-IP protection. No Redis or cloud resource is required by
this configuration; no AWS infrastructure is modified in this PR.

### Auth API
Use JSON objects containing exactly `email` and `password` for registration/login:
```bash
curl -i -X POST http://localhost:8000/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"person@example.com","password":"a long example passphrase"}'
curl -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"person@example.com","password":"a long example passphrase"}'
```
Registration returns 201 with `{"user":{"id":1,"email":"person@example.com","created_at":"...Z"}}`.
Duplicate normalized emails return 409 with `code=email_conflict` and never overwrite
an account. **This deliberately exposes account existence**, including via status
codes; throttling mitigates abuse but does not eliminate enumeration. Login returns
the same generic 401 for an unknown email, wrong password, or inactive account.
Unknown-account login verifies a dummy Argon2 hash to reduce timing differences.

A successful login returns `access_token`, `token_type` (`Bearer`), `expires_in`
(`3600`), and the public `user` object. Set `ACCESS_TOKEN` from that response in your
shell without recording it in source control:
```bash
curl -H "Authorization: Bearer $ACCESS_TOKEN" http://localhost:8000/api/auth/me
curl -X POST -H "Authorization: Bearer $ACCESS_TOKEN" http://localhost:8000/api/auth/logout
```
`me` returns `{"user":{...}}`; logout returns 204. Tokens are accepted only in the
Authorization header, never query strings or cookies. Claims include a string
user ID, issuer, audience, issuance/start/expiry times, random `jti`, and token
version. The algorithm is fixed to HS256; required claims and lifetime are checked.
There is up to 30 seconds of clock tolerance. Auth responses use `Cache-Control:
no-store`. Use HTTPS in production and avoid persistent browser token storage;
cookie authentication would require a separate CSRF design.

### Owned links and management
The existing `POST /api/links` body and `201 {"slug":"..."}` response are preserved.
A valid bearer token assigns the new link to that user. An absent token creates an
anonymous link by default; an invalid supplied token returns 401, never anonymous
fallback. Ownership/internal fields cannot be supplied in the request.
```bash
curl -X POST http://localhost:8000/api/links \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"slug":"mine","target":"https://example.com"}'
curl -H "Authorization: Bearer $ACCESS_TOKEN" 'http://localhost:8000/api/links?limit=20'
curl -H "Authorization: Bearer $ACCESS_TOKEN" http://localhost:8000/api/links/mine
curl -H "Authorization: Bearer $ACCESS_TOKEN" http://localhost:8000/api/links/mine/metrics
curl -X PATCH http://localhost:8000/api/links/mine \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"disabled":true}'
curl -X DELETE -H "Authorization: Bearer $ACCESS_TOKEN" http://localhost:8000/api/links/mine
```
Listing, details, PATCH, and DELETE require authentication and only access the
caller's nondeleted links. Lists use descending IDs, `limit` 1–100 (default 20),
and optional `before` cursor; response fields are `links` and `next_cursor`.
Link objects include slug, destinations, expiry, one-time/disabled flags, and
creation timestamp, without account secrets. PATCH accepts only `target`,
`ab_targets`, `expires_at`, and boolean `disabled`, revalidating the resulting
configuration. Slug, ownership, and `one_time` are immutable. Disabled (including consumed) one-time
links cannot be re-enabled. Concurrent stale edits return 409; writes include
ownership/state predicates in SQL.

DELETE returns 204 and marks the link deleted/disabled, preserving click history
and permanently reserving its slug. Deleted links disappear from listings,
management, analytics, and redirects. Public QR generation still encodes a short
URL, even for missing/deleted links; it does not expose private analytics.

Owned analytics require the owning user. Other users and nonexistent resources
receive identical JSON 404s. On the optional-auth analytics route, unauthenticated
requests for private or missing slugs also receive identical 404s; mandatory-auth
routes return 401 before looking up resources. Legacy and new anonymous links keep
public redirects and public analytics by default, but cannot be claimed/managed
merely by knowing a slug or logging in. Disabling anonymous creation/analytics is
an explicit deployment compatibility change. Public redirects remain public for
owned links too: ownership protects management and analytics, not the short URL.

API errors retain a string `error` and add a stable `code`: 400 invalid request,
401 missing/invalid credentials or tokens, 404 inaccessible/missing resource,
409 email/slug conflict or invalid state, 429 rate-limited, and 503 temporary
unavailability. Bodies above 1 MiB return 413. Invalid/expired/tampered/revoked tokens
share `invalid_token`. No password hashes or tokens are exposed by user/link serializers.

### Migration and rollout
Back up and stop writers, export the signing key and limiter configuration, and
run a **single** `alembic upgrade head` before starting the new image. Revision
`0003` creates users and adds nullable ownership/deletion columns and the owner
listing index. Existing links remain anonymous and all click rows are retained.
SQLite adds a nullable REFERENCES column without rebuilding the referenced link
table. SQLite account IDs use AUTOINCREMENT to prevent a deleted account ID from being
reused while an old JWT exists. Both engines restrict deleting users with links; ownership never silently
becomes anonymous. SQLite foreign keys are enabled on every application connection.
Migration refuses orphaned clicks; reconcile them on a verified copy before retrying,
without discarding historical rows automatically.

For an original unversioned database, follow the earlier backup/schema-check and
`stamp 0001` procedure, then upgrade through 0003. Do not stamp head. Existing 0002
databases upgrade directly. Downgrading 0003 discards users and ownership, exposing
formerly owned links under the anonymous policy; it requires explicit planning,
not routine rollback. SQLite downgrade requires version 3.35+ for DROP COLUMN.
Deletion/disable cannot recall a redirect already in progress. One-time consumption
and the click remain in one transaction; failed inserts roll back the claim.
A committed redirect can still consume the link if network delivery fails.

Run `python -m pytest -ra` for all tests. Set `TEST_POSTGRES_URL` for real PostgreSQL
migration, ownership, registration/logout races, and one-time integration tests.
Set `TEST_REDIS_URL` only to a disposable Redis instance to exercise shared counters
across apps; its limiter key namespace is cleared by that test. Neither variable
means those integrations passed unless the tests actually ran.
