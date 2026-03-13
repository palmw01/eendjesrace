# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Badeendjesrace** is a Dutch-language Flask web app for selling raffle tickets (lotnummers) for a duck race, built for Diaconie Hervormde gemeente te Wapenveld (KvK 76404862). It handles order entry, iDEAL payment processing via Mollie, atomic ticket number assignment, and confirmation emails.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (http://localhost:5000)
python app.py

# Run tests
python -m pytest tests/test_app.py -v
# or without pytest:
python tests/test_app.py

# Run a single test class or method
python -m pytest tests/test_app.py::TestWebhookStatussen -v
python -m pytest tests/test_app.py::TestBerekenBedrag::test_vijf_eendjes_aanbieding -v

# Production (Procfile)
gunicorn app:app
```

## Required Environment Variables

| Variable | Description |
|----------|-------------|
| `MOLLIE_API_KEY` | Mollie test/live API key |
| `BASE_URL` | Public domain (used for webhook + redirect URLs) |
| `RESEND_API_KEY` | Resend API key for transactional email |
| `ADMIN_PASS` | Admin password — minimum 12 characters (app refuses to start otherwise) |
| `ADMIN_USER` | Admin username (default: `admin`) |

Key optional variables: `RESEND_FROM` (verified sender address), `MAX_EENDJES` (default 3000, seeds the DB on first run), `DATABASE` (default `eendjes.db`), `HTTPS` (set `true` in production), `SECRET_KEY`, `LOG_DIR` (default `logs`), `FLASK_DEBUG` (set `true` for debug mode), `SECURITY_CONTACT` (e.g. `mailto:admin@example.com`, used in `/.well-known/security.txt`; falls back to `RESEND_FROM`), `PRIJS_PER_STUK` (default 2.50, seeds the DB on first run), `PRIJS_VIJF_STUKS` (default 10.00, seeds the DB on first run), `TRANSACTIEKOSTEN` (default 0.32, seeds the DB on first run). All three prices are editable via the admin panel after first run.

## Architecture

The entire backend lives in `app.py` (single file). Templates are in `templates/`. Tests are in `tests/test_app.py`.

### Payment & Order Flow

1. Public form (`/`, `templates/index.html`) → POST to `/bestellen`
2. `/bestellen` validates input, creates Mollie iDEAL payment, stores order with status `aangemaakt`, redirects user to Mollie
3. Mollie calls `/webhook` (async) on payment status change → assigns ticket numbers + sends confirmation email
4. `/betaald/<id>` is a fallback for when the webhook is delayed — polls Mollie directly

### Database (SQLite, `eendjes.db`, 3 tables)

- **`bestellingen`**: orders — `naam`, `email`, `telefoon`, `aantal`, `bedrag`, `transactiekosten` (0/1), `mollie_id`, `status` (aangemaakt/betaald/mislukt/geannuleerd/verlopen), `lot_van`/`lot_tot` (ticket range), `mail_verstuurd`, `pogingen`
- **`teller`**: single row with `volgend_lot` (next ticket number), `max_eendjes` (total available, editable via admin), `max_per_bestelling` (per-order limit, editable via admin), `prijs_per_stuk`, `prijs_vijf_stuks`, `transactiekosten` (all editable via admin, seeded from env on first run), `notificatie_email` (optional admin copy address, editable via admin, empty = disabled)
- **`webhook_log`**: audit log of webhook calls

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

`/admin` (protected by session login, timing-safe password check, session expires after 4 hours) shows order statistics (incl. openstaande/hangende bestellingen), lets admins resend confirmation emails for failed deliveries, filter orders by status, search orders by naam/e-mail/lotnummer (server-side, works across all pages), and offers a CSV export (`/admin/export-csv`) — semicolon-delimited with UTF-8 BOM for Excel compatibility; filename includes a datetime timestamp (e.g. `bestellingen_20260312_143022.csv`). Orders are paginated at 50 per page (`PAGINA_GROOTTE = 50`). Status filter and search term are preserved across pagination. Each order row has an edit button (`/admin/bestelling/<id>/wijzigen`) that allows updating naam, email, telefoon, status, and mail_verstuurd — **not** lotnummers.

Admin routes:
- `POST /admin/instellingen` — update `max_eendjes`, `max_per_bestelling`, `prijs_per_stuk`, `prijs_vijf_stuks`, `transactiekosten`, and/or `notificatie_email` in the `teller` table. Validates that `max_eendjes` cannot be set below the number of already sold tickets, prices must be > 0, transactiekosten >= 0, notificatie_email must be a valid address or empty. When set, a silent copy of every confirmation email is sent to this address with subject `[Kopie] Bestelling … – Badeendjesrace!`.
- `POST /admin/opruimen` — deletes orders with status `verlopen`/`mislukt`/`geannuleerd` where `lot_van IS NULL` (no tickets assigned). Safe to run at any time.
- `POST /admin/reset` — full database reset (wipes all orders + webhook_log, resets `volgend_lot` to 1, resets SQLite autoincrement so IDs start at 1 again). Requires typing `RESET` as confirmation. Has a JS confirm dialog as second safeguard.

`GET /api/beschikbaar` — public JSON endpoint returning `verkocht`, `beschikbaar`, `max_eendjes`, `max_per_bestelling`. Used by the homepage auto-refresh (every 30s).

`GET /privacy` — AVG-compliant privacy policy (organisation details, data categories, legal bases, processors Mollie + Resend, retention periods, data subject rights).

`GET /voorwaarden` — General terms and conditions (Mollie requirement: organisation details with KvK 76404862, event description, ticket rules, payment, no-refund policy, liability, prize award).

## Testing Notes

`tests/test_app.py` stubs out Mollie, Resend, Flask-WTF CSRF, and Flask-Limiter so tests run with only Flask installed. Tests cover pricing, input validation, database operations, atomic transactions, email sending, webhook processing, admin routes, `max_per_bestelling`/`max_eendjes` settings, price settings (`prijs_per_stuk`/`prijs_vijf_stuks`/`transactiekosten`) via admin, CSV injection escaping, CSV filename timestamp, email header content (afbeelding/kleur/datum), notification email (instellen/validatie/versturen), email validation in wijzig_bestelling, opruimen, paginering, server-side statusfilter, CSP nonces, Permissions-Policy, session permanence, `saniteer_log`, and legal pages (`/privacy`, `/voorwaarden`). The test database uses `/tmp/eendjes_test.db` (reset before each test class). `maak_db()` includes all teller columns. Total: 210 tests.

## Key Patterns

- All comments and variable/function names are in Dutch (e.g., `bestelling` = order, `eendjes` = ducks/tickets, `lotnummer` = ticket number, `betaald` = paid)
- Security headers (X-Frame-Options, CSP with per-request nonces + `base-uri`/`form-action 'self'`, Permissions-Policy, suppressed `Server` header, etc.), rate limiting (5/min on admin login), and ProxyFix (for Railway deployment) are all configured in `app.py`. `saniteer_log()` strips newlines from user input before logging to prevent log injection. `GET /.well-known/security.txt` serves an RFC 9116-compliant security contact file.
- Mollie webhook IP allowlisting is deliberately **not used** (Mollie advises against it — IP ranges change without notice). Security relies on the protocol: the webhook only delivers a payment ID (`tr_…`), and the app always retrieves the payment status via an authenticated Mollie API call.
- The database is auto-initialized on module import (`init_db()` called at module level)

### Mollie API v3

`mollie-api-python` v3 does **not** have `is_failed()`, `is_canceled()`, or `is_expired()` methods. Check payment status via `betaling.status` string directly (`"failed"`, `"canceled"`, `"expired"`). Only `is_paid()`, `is_pending()`, and `is_open()` exist.
