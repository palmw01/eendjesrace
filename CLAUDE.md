# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Badeendjesrace** is a Dutch-language Flask web app for selling raffle tickets (lotnummers) for a duck race, built for Diaconie Hervormde gemeente te Wapenveld (KvK 76404862). It handles order entry, iDEAL payment processing via Mollie, atomic ticket number assignment, and confirmation emails.

## Commands

```bash
# Install dependencies (first time)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest

# Run locally (http://localhost:5000)
python app.py

# Run tests (requires PYTHONPATH=. for the venv)
PYTHONPATH=. .venv/bin/pytest tests/test_app.py -v
# or without pytest:
PYTHONPATH=. .venv/bin/python tests/test_app.py

# Run a single test class or method
PYTHONPATH=. .venv/bin/pytest tests/test_app.py::TestWebhookStatussen -v
PYTHONPATH=. .venv/bin/pytest tests/test_app.py::TestBerekenBedrag::test_vijf_eendjes_aanbieding -v

# Production (Procfile)
gunicorn app:app
```

## Required Environment Variables

| Variable | Description |
|----------|-------------|
| `MOLLIE_API_KEY` | Mollie test/live API key |
| `BASE_URL` | Public domain (used for webhook + redirect URLs) |
| `RESEND_API_KEY` | Resend API key for transactional email |
| `ADMIN_PASS` | Initial admin password — minimum 12 characters. **Only required on first start** (when the `beheerders` table is empty). Can be removed from env once accounts exist in the DB. |
| `ADMIN_USER` | Initial admin username (default: `admin`). Only used on first start. |

Key optional variables: `RESEND_FROM` (verified sender address), `MAX_EENDJES` (default 3000, seeds the DB on first run), `DATABASE` (default `eendjes.db`), `HTTPS` (set `true` in production), `SECRET_KEY`, `LOG_DIR` (default `logs`), `FLASK_DEBUG` (set `true` for debug mode), `SECURITY_CONTACT` (e.g. `mailto:admin@example.com`, used in `/.well-known/security.txt`; falls back to `RESEND_FROM`), `PRIJS_PER_STUK` (default 2.50, seeds the DB on first run), `PRIJS_VIJF_STUKS` (default 10.00, seeds the DB on first run), `TRANSACTIEKOSTEN` (default 0.32, seeds the DB on first run). All three prices are editable via the admin panel after first run. `LITESTREAM_ACCESS_KEY_ID` + `LITESTREAM_SECRET_ACCESS_KEY` enable automatic SQLite backup to Cloudflare R2 via Litestream (see `start.sh` and `litestream.yml`).

## Architecture

The entire backend lives in `app.py` (single file). Templates are in `templates/`. Tests are in `tests/test_app.py`.

### Payment & Order Flow

1. Public form (`/`, `templates/index.html`) → POST to `/bestellen`
2. `/bestellen` validates input, creates Mollie iDEAL payment, stores order with status `aangemaakt`, redirects user to Mollie
3. Mollie calls `/webhook` (async) on payment status change → assigns ticket numbers + sends confirmation email
4. `/betaald/<id>` is a fallback for when the webhook is delayed — polls Mollie directly

### Database (SQLite, `eendjes.db`, 4 tables)

- **`bestellingen`**: orders — `voornaam`, `achternaam`, `email`, `telefoon`, `aantal`, `bedrag`, `transactiekosten` (0/1), `transactiekosten_bedrag`, `mollie_id`, `status` (aangemaakt/betaald/mislukt/geannuleerd/verlopen), `lot_van`/`lot_tot` (ticket range), `mail_verstuurd`, `pogingen`, `betaalwijze` (ideal/contant/overboeking)
- **`teller`**: single row with `volgend_lot` (next ticket number), `max_eendjes` (total available, editable via admin), `max_per_bestelling` (per-order limit, editable via admin), `prijs_per_stuk`, `prijs_vijf_stuks`, `transactiekosten` (all editable via admin, seeded from env on first run), `notificatie_email` (optional admin copy address, editable via admin, empty = disabled)
- **`webhook_log`**: audit log of webhook calls
- **`beheerders`**: admin accounts — `gebruikersnaam` (unique), `wachtwoord_hash` (Werkzeug PBKDF2), `aangemaakt_op`, `laatste_inlog` (nullable, set on each successful login). Seeded on first start from `ADMIN_USER`/`ADMIN_PASS` env vars **only if the table is empty**. Multiple accounts supported; manageable via the admin panel without redeployment. The `laatste_inlog` column is shown in the beheerders table on `/admin/beheer`.

### Atomic Ticket Assignment

`wijs_lotnummers_toe()` in `app.py` uses `BEGIN EXCLUSIVE` SQLite transaction to prevent overselling. It is idempotent — safe to call multiple times for the same order. The database connection uses `isolation_level=None` (autocommit) so all transaction control is explicit.

### Pricing

```python
def bereken_bedrag(aantal, prijs_per_stuk=None, prijs_vijf_stuks=None):
    # Uses DB values from get_prijs_per_stuk() / get_prijs_vijf_stuks() in routes
    vijftallen = aantal // 5
    rest = aantal % 5
    return round(vijftallen * p_vijf + rest * p_stuk, 2)
```

Prices (`prijs_per_stuk`, `prijs_vijf_stuks`, `transactiekosten`) are stored in the `teller` table and editable via the admin panel. Getter functions `get_prijs_per_stuk()`, `get_prijs_vijf_stuks()`, `get_transactiekosten()` read the live values from DB. The optional iDEAL transaction fee is added when the buyer checks the checkbox; stored in the boolean `transactiekosten` column of `bestellingen` and included in the Mollie total.

### Admin

The login page (`templates/admin_login.html`) has a subtle "← Terug naar de website" link below the white card, linking back to `/`.

`/admin` (protected by session login, DB-backed password check via `check_password_hash`, session expires after 4 hours) is the **orders page** (title: "Bestellingen"): shows order statistics (incl. openstaande/hangende bestellingen), lets admins resend confirmation emails for failed deliveries, filter orders by status, search orders by naam/e-mail/lotnummer (server-side, works across all pages), and offers a CSV export (`/admin/export-csv`) — semicolon-delimited with UTF-8 BOM for Excel compatibility; filename includes a datetime timestamp (e.g. `bestellingen_20260312_143022.csv`). The filter bar has two fixed rows: row 1 = zoekbalk (max-width 380px) + Zoeken + Wissen + CSV button (right-aligned via `margin-left:auto`); row 2 = status filter pills + CSV icon (mobile only). On mobile row 1 wraps (zoekbalk full-width, buttons below), row 2 shows CSV as a round dropdown icon. Orders are paginated at 50 per page (`PAGINA_GROOTTE = 50`). Status filter and search term are preserved across pagination. Each order row has an edit button (`/admin/bestelling/<id>/wijzigen`) that allows updating naam, email, telefoon, status, and mail_verstuurd — **not** lotnummers. The "Handmatige bestelling" card is collapsible via native HTML `<details>`/`<summary>` (no JavaScript). The logged-in username is shown in the topbar. A "⚙ Beheer" button links to `/admin/beheer`. A "🔑 Wachtwoord" button opens a `<dialog>` modal. Page title splits on all screen sizes: "Badeendjesrace" on line 1, "– Bestellingen" as subtitle on line 2 (`topbar align-items: flex-start`).

`/admin/beheer` — **settings management page** (separate from the orders page). Contains the "Instellingen", "Beheerders", and "Gevaarzone" sections that were previously on `/admin`. Has a "← Bestellingen" nav button linking back to `/admin`. Instellingen/beheerders changes and opruimen/reset redirects all go to `/admin/beheer`.

Admin routes:
- `GET /admin/beheer` — settings management page with instellingen, beheerders, and gevaarzone sections.
- `POST /admin/instellingen` — update `max_eendjes`, `max_per_bestelling`, `prijs_per_stuk`, `prijs_vijf_stuks`, `transactiekosten`, and/or `notificatie_email` in the `teller` table. Validates that `max_eendjes` cannot be set below the number of already sold tickets, prices must be > 0, transactiekosten >= 0, notificatie_email must be a valid address or empty. When set, a silent copy of every confirmation email is sent to this address with subject `[Kopie] Bestelling … – Badeendjesrace!`. Redirects to `/admin/beheer`.
- `POST /admin/opruimen` — deletes orders with status `verlopen`/`mislukt`/`geannuleerd` where `lot_van IS NULL` (no tickets assigned). Safe to run at any time. Redirects to `/admin/beheer`.
- `POST /admin/handmatig` — create a manual order (cash/bank transfer); atomically assigns ticket numbers, optionally sends confirmation email if address is provided. `betaalwijze` = `contant` or `overboeking`. Redirects to `/admin`.
- `POST /admin/reset` — full database reset (wipes all orders + webhook_log, resets `volgend_lot` to 1, resets SQLite autoincrement so IDs start at 1 again). **Does not delete admin accounts** (`beheerders` table is untouched). Requires typing `RESET` as confirmation. Has a JS confirm dialog as second safeguard. Redirects to `/admin/beheer`.
- `POST /admin/beheerder-toevoegen` — add a new admin account; validates username not empty, password ≥ 12 chars, passwords match, username unique. Redirects to `/admin/beheer`.
- `POST /admin/beheerder-verwijderen/<id>` — delete an admin account; blocked if only 1 account exists or if deleting own account. Redirects to `/admin/beheer`.
- `POST /admin/wachtwoord-wijzigen` — change the logged-in admin's own password; requires current password, new password ≥ 12 chars, and matching confirmation. Accessible via the "🔑 Wachtwoord" button in the topbar. Redirects to `/admin`.

`GET /api/beschikbaar` — public JSON endpoint returning `verkocht`, `beschikbaar`, `max_eendjes`, `max_per_bestelling`. Used by the homepage auto-refresh (every 30s).

`GET /robots.txt` — crawler directives: allows `/`, `/privacy`, `/voorwaarden`; blocks `/admin`, `/bestellen`, `/webhook`, `/api/`, `/betaald/`; references the sitemap.

`GET /sitemap.xml` — XML sitemap listing `/`, `/privacy`, `/voorwaarden` with `changefreq` and `priority`. Admin and transactional pages are excluded.

`GET /privacy` — AVG-compliant privacy policy (organisation details, data categories, legal bases, processors Mollie + Resend, retention periods, data subject rights).

`GET /voorwaarden` — General terms and conditions (Mollie requirement: organisation details with KvK 76404862, event description, ticket rules, payment, no-refund policy, liability, prize award).

### Database Backup (Litestream + Cloudflare R2)

`start.sh` downloads the Litestream binary at runtime (using `curl`, `wget`, or `python3` as fallback) and starts it as a supervisor process wrapping Gunicorn. Litestream replicates the SQLite WAL to Cloudflare R2 every second (`sync-interval: 1s`). Configuration is in `litestream.yml` (bucket, endpoint with hardcoded account ID, credentials from env). On startup, if `LITESTREAM_ACCESS_KEY_ID` is set and the database file does not exist, `start.sh` automatically restores it from R2 before starting Gunicorn. `nixpacks.toml` installs `curl` during the Railway build phase.

## Testing Notes

`tests/test_app.py` stubs out Mollie, Resend, Flask-WTF CSRF, and Flask-Limiter so tests run with only Flask installed. Tests cover pricing, input validation, database operations, atomic transactions, email sending, webhook processing, admin routes, `max_per_bestelling`/`max_eendjes` settings, price settings (`prijs_per_stuk`/`prijs_vijf_stuks`/`transactiekosten`) via admin, CSV injection escaping, CSV filename timestamp, CSV header columns, email header content (afbeelding/kleur/datum), notification email (instellen/validatie/versturen), email validation in wijzig_bestelling, opruimen, paginering, server-side statusfilter, CSP nonces, Permissions-Policy, session permanence, `saniteer_log`, legal pages (`/privacy`, `/voorwaarden`), footer content on `/betaald/<id>` and error pages (`fout.html`), collapsible `<details>` sections in admin, multi-account admin and password management (`TestBeheerderAccounts`, including `laatste_inlog` tests), security headers (`TestSecurityHeadersAanvullend`), email formatting variants (`TestBevestigingsmailOpmaakvarianten`), input boundary values (`TestValideerInvoerGrenswaardes`), atomic ticket assignment edge cases (`TestWijsLotnummersToeAanvullend`), admin page username display, password change persistence, betaald-page fallback errors, bestellen edge cases, admin settings validation, handmatige bestelling telefoon, webhook audit log, SEO meta-tags/robots.txt/sitemap.xml (`TestSeoEnRobots`), beheer-paginasplitsing en filterbalk-layout (`TestAdminBeheerPagina`). The test database uses `/tmp/eendjes_test.db` (reset before each test class). `maak_db()` includes all teller columns and seeds the `beheerders` table (incl. `laatste_inlog`). `conftest.py` cleans up the test database + WAL/SHM files before pytest starts (required for Python 3.14 + SQLite WAL mode). Total: 408 tests.

## Key Patterns

- All comments and variable/function names are in Dutch (e.g., `bestelling` = order, `eendjes` = ducks/tickets, `lotnummer` = ticket number, `betaald` = paid)
- Security headers (X-Frame-Options, CSP with per-request nonces + `base-uri`/`form-action 'self'`, Permissions-Policy, suppressed `Server` header, etc.), rate limiting (5/min on admin login), and ProxyFix (for Railway deployment) are all configured in `app.py`. `saniteer_log()` strips newlines from user input before logging to prevent log injection. `GET /.well-known/security.txt` serves an RFC 9116-compliant security contact file.
- **SEO**: `index.html` has `<meta name="description">`, `<link rel="canonical">`, Open Graph (`og:title/description/url/image/type/locale/site_name`), Twitter Card, and a JSON-LD `Event` schema block (with nonce). Admin, betaald, and fout pages carry `<meta name="robots" content="noindex, nofollow">`. Privacy/voorwaarden carry `noindex, follow` with a canonical. `inject_base_url()` context processor makes `{{ base_url }}` available in all templates. `/robots.txt` and `/sitemap.xml` are served as plain-text/XML routes.
- Mollie webhook IP allowlisting is deliberately **not used** (Mollie advises against it — IP ranges change without notice). Security relies on the protocol: the webhook only delivers a payment ID (`tr_…`), and the app always retrieves the payment status via an authenticated Mollie API call.
- The database is auto-initialized on module import (`init_db()` called at module level)
- Admin passwords are stored as Werkzeug PBKDF2 hashes in the `beheerders` table. `ADMIN_PASS` is only validated/used when the table is empty (first start); after that it can be removed from env vars.

### Mollie API v3

`mollie-api-python` v3 does **not** have `is_failed()`, `is_canceled()`, or `is_expired()` methods. Check payment status via `betaling.status` string directly (`"failed"`, `"canceled"`, `"expired"`). Only `is_paid()`, `is_pending()`, and `is_open()` exist.
