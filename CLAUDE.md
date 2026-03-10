# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Eendjesrace** is a Dutch-language Flask web app for selling raffle tickets (lotnummers) for a duck race, built for Hervormde Gemeente Wapenveld. It handles order entry, iDEAL payment processing via Mollie, atomic ticket number assignment, and confirmation emails.

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
| `ADMIN_PASS` | Admin password (app refuses to start without this) |
| `ADMIN_USER` | Admin username (default: `admin`) |

Key optional variables: `RESEND_FROM` (verified sender address), `MAX_EENDJES` (default 3000), `DATABASE` (default `eendjes.db`), `HTTPS` (set `true` in production), `SECRET_KEY`, `LOG_DIR` (default `logs`), `FLASK_DEBUG` (set `true` for debug mode).

## Architecture

The entire backend lives in `app.py` (single file). Templates are in `templates/`. Tests are in `tests/test_app.py`.

### Payment & Order Flow

1. Public form (`/`, `templates/index.html`) → POST to `/bestellen`
2. `/bestellen` validates input, creates Mollie iDEAL payment, stores order with status `aangemaakt`, redirects user to Mollie
3. Mollie calls `/webhook` (async) on payment status change → assigns ticket numbers + sends confirmation email
4. `/betaald/<id>` is a fallback for when the webhook is delayed — polls Mollie directly

### Database (SQLite, `eendjes.db`, 3 tables)

- **`bestellingen`**: orders — `naam`, `email`, `telefoon`, `aantal`, `bedrag`, `mollie_id`, `status` (aangemaakt/betaald/mislukt/geannuleerd/verlopen), `lot_van`/`lot_tot` (ticket range), `mail_verstuurd`, `pogingen`
- **`teller`**: single row tracking `volgend_lot` (next ticket number, starts at 1)
- **`webhook_log`**: audit log of webhook calls

### Atomic Ticket Assignment

`wijs_lotnummers_toe()` in `app.py` uses `BEGIN EXCLUSIVE` SQLite transaction to prevent overselling. It is idempotent — safe to call multiple times for the same order. The database connection uses `isolation_level=None` (autocommit) so all transaction control is explicit.

### Pricing

```python
def bereken_bedrag(aantal):
    vijftallen = aantal // 5   # bundles of 5 at €10.00
    rest = aantal % 5           # singles at €2.50
    return round(vijftallen * 10.00 + rest * 2.50, 2)
```

### Admin

`/admin` (protected by session login, timing-safe password check) shows order statistics and lets admins resend confirmation emails for failed deliveries.

## Testing Notes

`tests/test_app.py` stubs out Mollie, Resend, Flask-WTF CSRF, and Flask-Limiter so tests run with only Flask installed. Tests cover pricing, input validation, database operations, atomic transactions, email sending, and webhook processing. The test database uses `/tmp/eendjes_test.db` (reset before each test class).

## Key Patterns

- All comments and variable/function names are in Dutch (e.g., `bestelling` = order, `eendjes` = ducks/tickets, `lotnummer` = ticket number, `betaald` = paid)
- Security headers, rate limiting, ProxyFix (for Railway deployment), and Mollie webhook IP whitelisting are all configured in `app.py`
- The database is auto-initialized on module import (`init_db()` called at module level)

### Mollie API v3

`mollie-api-python` v3 does **not** have `is_failed()`, `is_canceled()`, or `is_expired()` methods. Check payment status via `betaling.status` string directly (`"failed"`, `"canceled"`, `"expired"`). Only `is_paid()`, `is_pending()`, and `is_open()` exist.
