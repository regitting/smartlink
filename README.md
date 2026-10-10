# SmartLink

SmartLink is a Flask API for shortening URLs, generating QR codes, and collecting
click analytics. It supports anonymous and user-owned links, random A/B destination
selection, expiration, and concurrency-safe one-time redirects. SQLite supports
lightweight local development; PostgreSQL supports a shared production database.

## Table of contents

- [Architecture and repository layout](#architecture-and-repository-layout)
- [Local setup](#local-setup)
- [Configuration](#configuration)
- [Databases and Docker](#databases-and-docker)
- [Schema migrations and data preservation](#schema-migrations-and-data-preservation)
- [Authentication and authorization](#authentication-and-authorization)
- [API reference](#api-reference)
- [Analytics and concurrent redirects](#analytics-and-concurrent-redirects)
- [Security and production deployment](#security-and-production-deployment)
- [Testing and verification](#testing-and-verification)
- [Known limitations](#known-limitations)

## Architecture and repository layout

Requests are served by Flask, through Gunicorn in the Docker image. Flask-SQLAlchemy
manages the `User`, `Link`, and `Click` models; Alembic manages schema revisions.
Redis or another supported shared limiter backend is optional when equivalent
rate limits are enforced at ingress. QR generation is public, and country lookup
uses an optional local GeoIP database. There is no frontend.

```text
Client → trusted HTTPS ingress → Gunicorn / Flask → SQLite or PostgreSQL
                                      ├────────→ shared limiter (when configured)
                                      └────────→ optional GeoIP database
```

| Path | Responsibility |
| --- | --- |
| `app/__init__.py` | Application factory, environment configuration, proxy handling |
| `app/auth.py` | Argon2id authentication, JWT validation, account/IP rate limits |
| `app/main.py` | Link creation/management, redirects, analytics, QR and health routes |
| `app/models.py` | User/link/click models, URL validation and UTC helpers |
| `app/database.py` | Database URL normalization, readiness commands, SQLite foreign keys |
| `app/utils.py` | Client IP, device classification and optional country lookup |
| `wsgi.py` | WSGI entry point |
| `migrations/` | Reviewed Alembic revisions |
| `tests/` | Isolated SQLite tests and opt-in PostgreSQL/Redis integrations |
| `Dockerfile`, `compose.yaml` | Application image and local PostgreSQL stack |
| `taskdef.json`, `trust.json` | Existing AWS deployment templates |

## Local setup

Use a supported Python interpreter and run these commands from the repository root.
The Docker image uses Python 3.11; a virtual environment is recommended for host use.
The commands below initialize a **new** SQLite database. For retained data, follow
[the migration adoption procedure](#adopting-an-unversioned-sqlite-database) first.

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

## Configuration

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

| Additional variable | Default / behavior |
| --- | --- |
| `DATABASE_URL` | `sqlite:///smartlink.db`; relative SQLite paths resolve under `instance/` |
| `SECRET_KEY` | `dev`; separate Flask configuration, never a JWT signing-key fallback |
| `TRUSTED_PROXY_HOPS` | `0`; nonnegative count of trusted `X-Forwarded-For` hops |
| `ENABLE_DEBUG_IP` | `0`; `1` enables the unauthenticated debug route |
| `GEOIP_DB_PATH` | `app/GeoLite2-Country.mmdb`; optional country database file |
| `TEST_POSTGRES_URL` | Unset; enables real PostgreSQL integration tests |
| `TEST_REDIS_URL` | Unset; enables real shared Redis counter tests |

Boolean environment switches are enabled by the literal value `1`. `APP_ENV`
accepts `development`, `production`, or `testing`; use the full name `production`,
not `prod`. Production safeguards are conditional on that setting.

`create_app(config)` also accepts configuration overrides. `MAX_CONTENT_LENGTH`
defaults to 1 MiB. Tests can disable limits using `RATELIMIT_ENABLED=False`; shared
production mode rejects that setting. Engine overrides use
`SQLALCHEMY_ENGINE_OPTIONS`: supplying your own dictionary replaces the default
options, so retain `pool_pre_ping`, `hide_parameters`, and appropriate connection
timeouts explicitly when customizing it. Do not put secrets into source-controlled
configuration or enable parameter logging for sensitive account queries.

## Databases and Docker

### SQLite and PostgreSQL

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
docker compose run --rm --build migrate
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

For standalone SQLite Docker development, export `JWT_SIGNING_KEY` as shown in
[local setup](#local-setup), then mount a persistent directory at `/app/instance`.
On a fresh data directory:

```bash
docker build -t smartlink .
mkdir -p instance
docker run --rm -e JWT_SIGNING_KEY -v "$PWD/instance:/app/instance" smartlink alembic upgrade head
docker run --rm -e JWT_SIGNING_KEY -p 127.0.0.1:8000:8000 -v "$PWD/instance:/app/instance" smartlink
```

Do not run migrations against an existing unversioned database before following
the adoption steps below.

## Schema migrations and data preservation

### Revisions and upgrade workflow

| Revision | Change |
| --- | --- |
| `0001` | Original Link/Click schema |
| `0002` | `ix_click_link_id` for analytics filtering |
| `0003` | Users, nullable link ownership, soft deletion and `(owner_id, id)` listing index |

Back up and stop writers, export the signing key and limiter configuration, and
run a **single** `alembic upgrade head` before starting the new image. Revision
`0003` creates users and adds nullable ownership/deletion columns and the owner
listing index. Existing links remain anonymous and all click rows are retained.
SQLite adds a nullable REFERENCES column without rebuilding the referenced link
table. SQLite account IDs use AUTOINCREMENT to prevent a deleted account ID from
being reused while an old JWT exists. Both engines restrict deleting users with
links; ownership never silently becomes anonymous. SQLite foreign keys are enabled on every application connection.
Migration refuses orphaned clicks; reconcile them on a verified copy before retrying,
without discarding historical rows automatically.

Online SQLite migrations explicitly begin a real DDL transaction, so interrupted
upgrades roll back schema changes and revision metadata together. PostgreSQL uses
its transactional DDL behavior. Offline generated SQL must be reviewed and applied
with an appropriate transaction; it does not execute or verify an upgrade.

For a database already at 0002, back up, stop writers, and run:

```bash
alembic current
alembic upgrade head
flask --app wsgi:app db-check
```

### Adopting an unversioned SQLite database

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

Do **not** stamp `head`: that would skip required indexes, users, ownership, and
deletion columns. Stamping only writes migration metadata; it does not validate
or fix a schema. For an empty new database, use `upgrade head` directly. Never rerun the initial creation migration
against existing tables. This workflow does not transfer SQLite rows to PostgreSQL;
a cross-database transfer requires a separately reviewed data export/import,
foreign-key checks, and PostgreSQL sequence resets after preserving existing IDs.
Previously stored expiry timestamps with discarded offsets cannot be reconstructed.

### Recovery from an interrupted SQLite upgrade

For SQLite databases partially upgraded by an earlier authentication build, **do not stamp
0003 or repeatedly retry blindly**. Work on a verified backup/copy first. Inspect
`alembic_version`, `PRAGMA table_info(link)`, the ownership index, and `app_user`.
Prefer restoring the pre-upgrade backup and rerunning the fixed migration. If no
backup exists, an operator must reconcile the partial schema: only when the marker
is 0002, there are no user rows, every owner_id/deleted_at is NULL, and all original
link/click records are verified, remove any new ownership index, then the new
columns (SQLite 3.35+), and finally the empty app_user table inside an explicit
transaction. Keep the revision at 0002 and retry the fixed upgrade on the copy.
If any account or ownership/deletion data exists, preserve it and obtain a tailored
recovery plan; removing those fields can expose private links. Never discard link
or click rows or overwrite the original database during recovery.

### Future revisions and downgrade risks

```bash
alembic current
alembic revision --autogenerate -m "describe schema change"
# Review the generated migration; test on a disposable database before rollout.
alembic upgrade head
python -m pytest -ra
```

Commit reviewed revisions together with model changes. Autogeneration is a proposal,
not a substitute for reviewing data preservation and database compatibility.
Serialize migration execution and retain a verified backup.

Downgrading **from 0003 to 0002 discards users and ownership**, which can expose
formerly owned analytics under the anonymous policy. It is not a routine safe
application rollback. Only the 0002-to-0001 step removes just the analytics index;
a downgrade from head to 0001 also executes the destructive 0003 downgrade.
Downgrading to `base` drops tables and data. SQLite's ownership-column downgrade
requires SQLite 3.35+ for DROP COLUMN. Plan recovery explicitly before any downgrade.

## Authentication and authorization

Accounts use Argon2id password hashes and HS256 bearer JWTs with a **60-minute**
access lifetime. No refresh tokens are issued. Every authenticated request validates
required claims and the account's current activity and token version in the database.

### Credentials and canonical identity

Passwords are 15–128 characters. Spaces and Unicode are supported, without
trimming, normalization, composition rules, or truncation. Email identities are
syntactically validated, trimmed, normalized, and lowercased;
the normalized result must be ASCII. Equivalent Unicode domain spellings are
accepted when they normalize to the same ASCII domain. Dots and plus tags are retained.
Raw email inputs above 1,024 characters are rejected before normalization;
normalized identities are limited to 254 characters. Authentication and
account-based throttling share the same canonicalization function. Lowercasing
treats rare case-sensitive mailboxes as the same account.
Email addresses are **not verified**. The API does not provide password recovery,
email changes, account deletion, MFA, or administrative roles.

Unknown-account login verifies a precomputed dummy Argon2 hash to reduce timing
differences. Concurrent password-hash upgrades use a conditional update, then reload
and verify the current hash rather than rejecting a successful competing rehash.
Login rechecks activity and token version before issuing a JWT. A concurrent
password change is verified against the new hash; a changed token version or
inactive account causes a generic login failure.

### Token validation and revocation

Tokens include string user ID (`sub`), issuer (`iss`), audience (`aud`), issuance,
start and expiry times (`iat`, `nbf`, `exp`), random `jti`, and `token_version`.
The accepted algorithm is fixed to HS256; required claim types and a lifetime no
longer than 3,600 seconds are validated. Clock tolerance is up to 30 seconds.

Tokens are accepted only as bearer Authorization headers, never query parameters
or cookies. `/api/auth/logout` atomically increments the user's token version,
revoking **all** access tokens for that account. Login is required to obtain a new
token afterward. Signing-key rotation immediately invalidates existing tokens;
retain the same signing key across normal restarts and workers.
Revocation after login's final state check invalidates that token on its next use.
Requests already in progress may finish. Auth-route responses use
`Cache-Control: no-store`; use HTTPS and avoid persistent browser token storage.
Cookie authentication would require a separate CSRF design.

### Ownership and anonymous compatibility

| Operation | Owned link | Anonymous or legacy link |
| --- | --- | --- |
| Redirect | Public when available | Public when available |
| QR generation | Public; encodes URL only | Public; encodes URL only |
| Analytics | Owning user only | Public by default; configurable |
| Listing / detail / PATCH / DELETE | Owning user only | No API management or claiming |

Ownership protects management and analytics, **not the public short URL**. Other
users receive the same JSON 404 as nonexistent resources. Mandatory-auth routes
return 401 before resource lookup; the optional-auth metrics route returns matching
404s for an anonymous request to private or missing slugs when anonymous analytics
is enabled. Invalid supplied tokens never fall back to anonymous behavior.

Disabling anonymous creation or analytics is an explicit compatibility change.
Legacy links remain anonymous after migration. Soft deletion preserves clicks and
reserves the slug permanently; it never changes ownership to anonymous.

## API reference

Examples assume `http://localhost:8000`. Replace it with your HTTPS API origin in
production. Link creation uses **`target`**, not `url`. Use JSON request bodies and
`Content-Type: application/json`; bearer tokens belong in the Authorization header.

| Method | Endpoint | Access / result |
| --- | --- | --- |
| GET | `/` | Public text liveness message |
| GET | `/health` | Public liveness; 200 without a database query |
| GET | `/api/ready` | Public database/revision readiness; 200 or 503 |
| POST | `/api/auth/register` | Public, rate-limited; 201 or duplicate-email 409 |
| POST | `/api/auth/login` | Public, rate-limited; 200 token or generic 401 |
| GET | `/api/auth/me` | Valid bearer token required |
| POST | `/api/auth/logout` | Valid bearer token; 204, revokes all user tokens |
| POST | `/api/links` | Optional authentication; anonymous creation configurable |
| GET | `/api/links` | Owner-only listing |
| GET / PATCH / DELETE | `/api/links/<slug>` | Owner-only detail / update / soft deletion |
| GET | `/api/links/<slug>/metrics` | Owner-only for owned links; anonymous policy otherwise |
| GET | `/<slug>` | Public redirect; 302 or unavailable-link 404 |
| POST | `/api/qr/<slug>` | Public PNG generation |
| GET | `/_debug/ip` | Public only when explicitly enabled; otherwise 404 |

### Health and readiness

```bash
curl -f http://localhost:8000/health
curl -f http://localhost:8000/api/ready
```

Successful responses are `{"status":"ok"}`. An unavailable or outdated database
returns `503 {"status":"not ready"}` from readiness; liveness stays independent.

### Registration, login and logout

Use JSON objects containing exactly `email` and `password` for registration/login:

```bash
curl -i -X POST http://localhost:8000/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"person@example.com","password":"a long example passphrase"}'
curl -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"person@example.com","password":"a long example passphrase"}'
```

**Registration:** returns 201 with `{"user":{"id":1,"email":"person@example.com","created_at":"...Z"}}`.
Duplicate normalized emails return 409 with `code=email_conflict` and never overwrite
an account. **This deliberately exposes account existence**, including via status
codes; throttling mitigates abuse but does not eliminate enumeration. Login returns
the same generic 401 for an unknown email, wrong password, or inactive account.
The verification strategy is described under
[credentials and canonical identity](#credentials-and-canonical-identity).

A successful login returns `access_token`, `token_type` (`Bearer`), `expires_in`
(`3600`), and the public `user` object. Set `ACCESS_TOKEN` from that response in your
shell without recording it in source control:

```bash
curl -H "Authorization: Bearer $ACCESS_TOKEN" http://localhost:8000/api/auth/me
```

`me` returns `{"user":{...}}`; logout returns 204 with no body. Keep the token
active for the owned-link examples below, then log out when finished. See
[token validation and revocation](#token-validation-and-revocation) for claim
validation, clock tolerance, key rotation and logout-all behavior.

### Creating anonymous links

```bash
curl -i -X POST http://localhost:8000/api/links \
  -H 'Content-Type: application/json' \
  -d '{"slug":"hello","target":"https://example.com"}'
```

A successful creation returns HTTP 201:

```json
{"slug": "hello"}
```

| Field | Validation / behavior |
| --- | --- |
| `slug` | Required; 1–64 ASCII letters, digits, underscores or hyphens; `health` is reserved; globally unique, including deleted links |
| `target` | HTTP/HTTPS destination, at most 2,048 characters; may be omitted/null when `ab_targets` is supplied |
| `ab_targets` | Optional nonempty list of valid HTTP/HTTPS destinations; each entry is validated |
| `expires_at` | Optional ISO 8601 timestamp with date and time; null means no expiration |
| `one_time` | Optional JSON boolean, default false |

At least one destination source is required. URLs require a valid host and port
syntax and reject whitespace, control characters, and backslashes. Validation is
syntactic; SmartLink does not fetch destinations or verify their ownership.
If both `target` and `ab_targets` are supplied, the A/B list determines redirects.
A/B selection is random per redemption, without weights or destination-level metrics.
Ownership and internal fields (`owner_id`, `user_id`, `owner`, `deleted_at`,
`disabled`, `id`) cannot be supplied during creation. Other unknown creation fields
are not generally rejected; they do not become model attributes.

Expiry offsets are converted to UTC. Timestamps without offsets are interpreted
as UTC; malformed values return 400. Both engines store UTC without timezone
information. An already expired timestamp can be stored, but the link cannot redirect.

An A/B, expiring, one-time example:

```bash
curl -i -X POST http://localhost:8000/api/links \
  -H 'Content-Type: application/json' \
  -d '{"slug":"campaign","ab_targets":["https://example.com/a","https://example.com/b"],"expires_at":"2099-01-01T12:00:00+05:30","one_time":true}'
```

### Redirects, metrics and QR codes

```bash
curl -i http://localhost:8000/hello
```

```http
HTTP/1.1 302 FOUND
Location: https://example.com
```

Missing, expired, disabled, consumed one-time, and soft-deleted links return 404.
Each successful redirect records a click. For an anonymous link with public
analytics enabled:

```bash
curl http://localhost:8000/api/links/hello/metrics
```

Illustrative response for one desktop click whose IP resolves to Canada:

```json
{
  "total": 1,
  "by_device": {"desktop": 1},
  "by_country": {"CA": 1}
}
```

Country keys are ISO country codes, not country names. Without GeoIP data or a
successful lookup, country is `unknown`. Device keys are `mobile`, `tablet`,
`desktop`, `bot`, or `unknown`, based on the request's User-Agent; a curl request
normally produces `unknown`. NULL and empty stored labels also group as `unknown`.

```bash
curl -o qr.png -X POST http://localhost:8000/api/qr/hello
```

The response is `image/png`. QR generation encodes the request origin plus the
slug; it does not check link existence, ownership, or availability. Missing and
deleted slugs can still produce PNGs. Use the intended public origin when requesting
a QR code; forwarded scheme/host headers are not trusted by the app.

### Owned links, pagination and management

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
# Revoke all account tokens after completing the authenticated examples.
curl -X POST -H "Authorization: Bearer $ACCESS_TOKEN" http://localhost:8000/api/auth/logout
```

Listing, details, PATCH, and DELETE require authentication and only access the
caller's nondeleted links. Lists use descending IDs, `limit` 1–100 (default 20),
and optional `before` cursor; response fields are `links` and `next_cursor`.
Link objects include slug, destinations, expiry, one-time/disabled flags, and
creation timestamp, without account secrets. PATCH accepts only `target`,
`ab_targets`, `expires_at`, and boolean `disabled`, revalidating the resulting
configuration. Slug, ownership, and `one_time` are immutable. Disabled (including
consumed) one-time links cannot be re-enabled. Concurrent stale edits return 409; writes include
ownership/state predicates in SQL.

DELETE returns 204 and marks the link deleted/disabled, preserving click history
and permanently reserving its slug. Deleted links disappear from listings,
management, analytics, and redirects. Public QR generation still encodes a short
URL, even for missing/deleted links; it does not expose private analytics.

See [ownership and anonymous compatibility](#ownership-and-anonymous-compatibility)
for access-control rules and deployment switches.

A detail response has this shape (timestamps are illustrative):

```json
{
  "slug": "mine",
  "target": "https://example.com",
  "ab_targets": null,
  "expires_at": null,
  "one_time": false,
  "disabled": false,
  "created_at": "2026-10-10T12:00:00Z"
}
```

A list returns `{"links":[...],"next_cursor":"..."}`. To fetch the next page, pass
its cursor as `before`; `next_cursor` is null on the last page. Cursors are positive
integer link IDs up to 2,147,483,647. Pagination is ordered by descending ID.
A PATCH body must be a nonempty JSON object containing only the documented mutable
fields. `disabled` must be a boolean. Stale updates return 409, or 404 if deletion
made the resource inaccessible. Successful PATCH returns the updated link object;
successful DELETE returns 204 with no body.

### Errors and response compatibility

API errors contain a string `error`. Authentication and new management errors also
include a stable `code`, for example:

```json
{"error": "Invalid access token", "code": "invalid_token"}
```

| Status | Typical condition |
| --- | --- |
| 400 | Invalid JSON, credentials format, destination, expiry, pagination or update fields |
| 401 | Missing required authentication, invalid credentials, expired/tampered/revoked token |
| 404 | Inaccessible or missing resource; unavailable redirect |
| 409 | Duplicate normalized email/slug, stale edit or prohibited one-time re-enable |
| 413 | Request body exceeds the default 1 MiB maximum |
| 429 | Rate limit exceeded; inspect `Retry-After` and limiter headers |
| 503 | Database/limiter unavailable or retryable SQLite redirect contention |

Existing link-creation validation/conflict and redirect-lock responses can contain
only `error`, without `code`. Public redirect/debug 404s use Flask's default HTML
error response. Health/readiness have their own `status` contract. Clients should
not assume every error is JSON or every JSON error has a `code` field.
Authentication 401 responses include `WWW-Authenticate: Bearer`.

## Analytics and concurrent redirects

Metrics aggregate all clicks in SQL and retain the existing response format.
One-time redirects conditionally update an enabled, unexpired link to disabled
and record the click in the same transaction. A losing request returns 404
without a click; transaction failure rolls back both operations. The update runs
before reads to avoid SQLite snapshot/lock upgrade failures. Regular links
continue returning 302 and recording each successful redirect.

SQLite serializes writers, including regular redirects (which also write clicks).
Requests wait up to the connection's busy timeout (sqlite3 defaults to 5 seconds);
if a lock outlasts it, redirects return 503 with `Retry-After: 1`, without a click
or consumed link. Configure the timeout via `SQLALCHEMY_ENGINE_OPTIONS` /
`connect_args.timeout` if needed. This requires a shared database with reliable
local filesystem locking across workers; separate SQLite files on different
instances cannot coordinate redemption. Consumption commits before the HTTP
response: if delivery fails afterward, the link remains consumed. No database
transaction can guarantee that a client receives the response exactly once.

## Security and production deployment

### Rate limiting

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
equivalent to account-and-IP protection. Ingress mode does not require Redis or an
application-managed shared storage service.

Registration and login apply separate IP and canonical-account limits. Invalid
email inputs still encounter the outer IP limit before returning 400. Anonymous
creation has an IP limit; authenticated creation does not currently have a
per-account quota or a built-in creation limit. Configure ingress capacity controls
as appropriate for a public service. Account counter keys hash canonical email
identities rather than using plaintext email as the key.

### Proxy trust and debug access

`TRUSTED_PROXY_HOPS` defaults to `0`: forwarded headers are ignored and
analytics uses the direct peer IP. Set it to the exact number of trusted
reverse proxies supplying `X-Forwarded-For` (for example, `1`). Werkzeug selects
the corresponding value from the right. This is a hop count, not an IP allowlist:
only enable it when network access to the app is restricted to those proxies
and they sanitize or append forwarded headers. Forwarded host, scheme, port,
and prefix headers are not trusted. The same settings can be passed to `create_app`.
`ENABLE_DEBUG_IP` defaults to false; the existing `/_debug/ip` route returns 404
unless explicitly enabled. It has no authentication, so leave it disabled publicly.

The debug endpoint can be inspected in a controlled local environment after
starting the application with `ENABLE_DEBUG_IP=1`:

```bash
curl http://localhost:8000/_debug/ip
```

Illustrative response when country lookup succeeds:

```json
{"ip": "203.0.113.25", "country": "CA"}
```

Unlike the metrics groups, debug country is null when no lookup is available.

### Production rollout

The repository contains an ECS/Fargate task template with port 8000 and CloudWatch
`awslogs` output. It does **not** provide the current database, JWT or limiter
configuration. The files under `deploy/` are placeholder artifacts, not usable
host-deployment instructions. The local Compose file is a development stack;
its defaults do not constitute a production deployment.

1. Build an application image and publish it to your deployment's registry (ECR
   for the existing ECS setup). Use an immutable image tag or digest.
2. Provide a shared PostgreSQL database for multiple application instances. Use
   suitable TLS settings and keep URL credentials outside source control.
3. Supply `JWT_SIGNING_KEY`, `APP_ENV=production`, and either shared limiter storage
   or verified ingress limiting to **every worker and migration task**. Set an
   independent production `SECRET_KEY` if using Flask facilities that rely on it.
   Use your deployment's secret injection rather than embedding credentials in images.
4. Back up retained data, stop writers, and run one explicit migration job with
   the new image: `alembic upgrade head`. Check `db-check` before resuming service.
5. Terminate HTTPS at a trusted ingress and restrict direct backend access. Configure
   the exact proxy hop count. ECS `awsvpc` tasks serve container port 8000; security
   groups should allow only the intended ingress. Public subnets/IP assignment are
   deployment choices, not authentication safeguards or application requirements.
6. Probe `/api/ready` for database/revision readiness and `/health` for liveness.
   The image checks readiness before starting Gunicorn with two workers, four
   threads each, and a 60-second timeout. It fails clearly rather than waiting
   indefinitely for a missing database, and does not run migrations automatically.
7. Verify shared throttling, token validation, ownership isolation, redirects,
   metrics, and one-time redemption through the actual public ingress.

To inspect the configured CloudWatch stream with existing AWS CLI access:

```bash
aws logs describe-log-streams \
  --log-group-name /ecs/smartlink \
  --order-by LastEventTime --descending --limit 5
# Set LOG_STREAM_NAME to one returned stream name.
aws logs get-log-events \
  --log-group-name /ecs/smartlink \
  --log-stream-name "$LOG_STREAM_NAME" --limit 50
```

Use your configured region and log group when they differ. Avoid logging passwords,
bearer tokens, database URLs, or limiter credentials. Default SQLAlchemy parameters
are hidden, but custom engine options and external logging need separate review.

### GeoIP and analytics privacy

Country enrichment is optional. Provision a compatible country database outside
source control and set `GEOIP_DB_PATH` to its path; mount it explicitly in Docker
if needed. Missing data, unknown IPs, and lookup failures produce `unknown` metrics.
The reader is cached in the process; restart workers after replacing the database.

Click records include IP, referrer, timestamp, device, and optional country. The
metrics endpoint returns aggregate counts, but these underlying records persist,
including after link soft deletion. Establish a retention and access policy before
collecting production traffic. Avoid secrets in destination URLs and referrers.

## Testing and verification

### SQLite regression suite

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -ra
python -m pip check
```

Tests use Flask's test client, temporary SQLite databases, and migrations rather
than automatic table creation. Fixtures isolate database state and provide signing
keys; no AWS access or GeoIP download is required. Most tests disable rate limits,
with separate tests enabling enforcement. PostgreSQL and Redis tests skip explicitly
when their respective integration environment variables are unset.

Coverage includes validation, health/readiness, redirects, expiry, disabled and
one-time links, A/B behavior, QR generation, counts above 1,000 clicks, authentication,
token revocation, ownership isolation, soft deletion, legacy adoption, failure-injected
SQLite migration rollback/retry, concurrent registration/logout/rehashing, and
PATCH-versus-redemption/deletion races.

### Real PostgreSQL and Redis integrations

Use disposable integration services, not a retained production database or Redis
instance. Supply a host-reachable PostgreSQL URL with permission to create/drop
schemas, and a Redis URL including any required authentication/TLS settings:

```bash
# For a host test runner, use localhost/a reachable host rather than Compose's db name.
export TEST_POSTGRES_URL="$DATABASE_URL"
# Export TEST_REDIS_URL from your disposable Redis service configuration.
python -m pytest -ra
```

When using Compose's database from the host, first replace `@db:` with
`@localhost:` in the PostgreSQL URL. The PostgreSQL fixtures create and remove only
randomly named test schemas and fail on connection/setup errors once enabled.
Redis tests reset the application's limiter namespace: never point them at a service
sharing production SmartLink counters. Tests use independent app instances to verify
shared counters and equivalent-domain normalization.

For focused verification:

```bash
python -m pytest tests/test_postgresql.py tests/test_auth_databases.py -v
python -m pytest tests/test_auth.py -v
```

The PostgreSQL tests exercise migrations, account/ownership behavior, parallel
regular and one-time redirects, password rehash conflicts, and stale PATCH races
against a real server. Offline PostgreSQL SQL compilation is not an integration
test. Report actual passed/failed/skipped counts; setting a URL alone does not prove
integration success. The merged authentication audit fixes were verified with
220 passing tests, zero failures and zero skips using PostgreSQL 16 and Redis 7;
that historical result does not replace running the suite in your environment.

## Known limitations

- **Accounts:** email ownership is unverified. Duplicate registration intentionally
  returns 409, exposing account existence; throttling reduces but does not eliminate
  enumeration. Login failures remain generic. Password recovery, email changes,
  account deletion, MFA, administrative roles, and refresh tokens are not implemented.
- **Public links:** user ownership does not make a redirect secret. Anyone who knows
  the slug can redeem it, including a one-time link. Anonymous links cannot be claimed
  or managed through the API. Disabling anonymous capabilities changes compatibility.
- **Storage and concurrency:** SQLite serializes writers and depends on reliable
  local file locking. A separate file per instance cannot coordinate one-time links.
  PostgreSQL is the shared-database option; cross-engine data transfer is manual.
- **Delivery:** a redirect already in progress cannot be recalled by disable/delete.
  A committed one-time redemption remains consumed if response delivery fails.
- **Analytics:** all clicks are aggregated without a 1,000-row cap, but device/country
  classification is approximate. The three aggregation queries can observe different
  concurrent snapshots under PostgreSQL READ COMMITTED; totals and groups are not
  guaranteed to represent one identical point in time during active traffic.
- **Operations:** production guards require `APP_ENV=production`; development defaults
  remain permissive. Ingress verification is an operator assertion, not automatic
  validation. Already-partial migrations require careful recovery on a copy.
- **Errors and URL generation:** some legacy errors lack stable JSON codes. QR URLs
  use the request origin; trusted forwarded scheme/host support is not enabled.
