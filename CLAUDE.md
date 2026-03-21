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
| `ADMIN_PASS` | Optional. If set (min. 12 chars) and the `beheerders` table is empty, the account is created automatically on first start. If not set and no accounts exist, the app starts in **setup mode**: a one-time token is printed to stdout and `GET /setup?token=<token>` shows a form to create the first account. Once an account exists this variable is no longer used. |
| `ADMIN_USER` | Initial admin username (default: `admin`). Only used on first start. |

Key optional variables: `RESEND_FROM` (verified sender address; a startup warning is logged if not set), `MAX_EENDJES` (default 3000, seeds the DB on first run), `DATABASE` (default `eendjes.db`), `HTTPS` (set `true` in production), `SECRET_KEY`, `LOG_DIR` (default `logs`), `FLASK_DEBUG` (set `true` for debug mode), `SECURITY_CONTACT` (e.g. `mailto:admin@example.com`, used in `/.well-known/security.txt`; falls back to `RESEND_FROM`), `PRIJS_PER_STUK` (default 2.50, seeds the DB on first run), `PRIJS_VIJF_STUKS` (default 10.00, seeds the DB on first run), `TRANSACTIEKOSTEN` (default 0.32, seeds the DB on first run). All three prices are editable via the admin panel after first run. `LITESTREAM_ACCESS_KEY_ID` + `LITESTREAM_SECRET_ACCESS_KEY` enable automatic SQLite backup to Cloudflare R2 via Litestream (see `start.sh` and `litestream.yml`).

## Architecture

The entire backend lives in `app.py` (single file). Templates are in `templates/`. Tests are in `tests/test_app.py`.

### Payment & Order Flow

1. Public form (`/`, `templates/index.html`) → POST to `/bestellen`
2. `/bestellen` validates input, creates Mollie iDEAL payment, stores order with status `aangemaakt`, redirects user to Mollie
3. Mollie calls `/webhook` (async) on payment status change → assigns ticket numbers + sends confirmation email
4. Mollie redirects user to `/betaald/r/<bestelling_id>` (tussenroute), which redirects to `/betaald/<mollie_id>`
5. `/betaald/<mollie_id>` is the confirmation page; also acts as fallback if the webhook is delayed — polls Mollie directly

**URL security**: The Mollie `redirectUrl` points to `/betaald/r/<bestelling_id>` (internal, never shown to users). That route looks up the `mollie_id` from the DB and redirects to `/betaald/<mollie_id>` (e.g. `tr_abc123xyz`). This prevents enumeration: integer order IDs are never exposed in the browser URL.

**Logging checkpoints** (all via `app.logger.info`):
- `/bestellen`: order received (naam, aantal, email, incl_tk)
- `/bestellen`: payment created (bestelling_id, mollie_id, bedrag)
- `/bestellen`: redirect to Mollie checkout
- `wijs_lotnummers_toe()`: ticket range assigned (start–einde) or idempotent return
- `/webhook`: all status changes (paid/pending/open/failed/canceled/expired)
- `/betaald/<mollie_id>`: fallback triggered + outcome

### Database (SQLite, `eendjes.db`, 4 tables)

- **`bestellingen`**: orders — `voornaam`, `achternaam`, `email`, `telefoon`, `aantal`, `bedrag`, `transactiekosten` (0/1), `transactiekosten_bedrag`, `mollie_id`, `status` (aangemaakt/betaald/mislukt/geannuleerd/verlopen), `lot_van`/`lot_tot` (ticket range), `mail_verstuurd`, `pogingen`, `betaalwijze` (ideal/contant/overboeking)
- **`teller`**: single row with `volgend_lot` (next ticket number), `max_eendjes` (total available, editable via admin), `max_per_bestelling` (per-order limit, editable via admin), `prijs_per_stuk`, `prijs_vijf_stuks`, `transactiekosten` (all editable via admin, seeded from env on first run), `onderhoudsmodus` (0/1, toggles maintenance mode, editable via admin)
- **`webhook_log`**: audit log of webhook calls
- **`beheerders`**: admin accounts — `gebruikersnaam` (unique), `wachtwoord_hash` (Werkzeug PBKDF2), `aangemaakt_op`, `laatste_inlog` (nullable, set on each successful login), `sessie_versie` (INTEGER, incremented on password change to invalidate all other sessions), `mislukte_pogingen` (INTEGER, consecutive failed login counter), `geblokkeerd_tot` (TEXT ISO timestamp, account locked for 15 min after 10 consecutive failures). Seeded on first start from `ADMIN_USER`/`ADMIN_PASS` env vars **only if the table is empty**. Multiple accounts supported; manageable via the admin panel without redeployment. The `laatste_inlog` column is shown in the beheerders table on `/admin/beheer`.

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
- `POST /admin/instellingen` — update `max_eendjes`, `max_per_bestelling`, `prijs_per_stuk`, `prijs_vijf_stuks`, `transactiekosten`, and/or `onderhoudsmodus` in the `teller` table. Validates that `max_eendjes` cannot be set below the number of already sold tickets, prices must be > 0, transactiekosten >= 0. `onderhoudsmodus` is a checkbox (present = on, absent = off). **All fields are validated before any DB write** — if any field is invalid, nothing is saved (atomic all-or-nothing). The DB writes are wrapped in an explicit `BEGIN`/`COMMIT`/`ROLLBACK` transaction so a mid-loop DB error also leaves the database unchanged; `sqlite3.Error` is caught and shown as a flash message rather than propagating as a 500. Redirects to `/admin/beheer`.
- `POST /admin/opruimen` — deletes orders with status `verlopen`/`mislukt`/`geannuleerd` where `lot_van IS NULL` (no tickets assigned). Safe to run at any time. Redirects to `/admin/beheer`.
- `POST /admin/handmatig` — create a manual order (cash/bank transfer); atomically assigns ticket numbers, optionally sends confirmation email if address is provided. `betaalwijze` = `contant` or `overboeking`. Redirects to `/admin`.
- `POST /admin/reset` — full database reset (wipes all orders + webhook_log, resets `volgend_lot` to 1, resets SQLite autoincrement so IDs start at 1 again). **Does not delete admin accounts** (`beheerders` table is untouched). Requires typing `RESET` as confirmation. Has a JS confirm dialog as second safeguard. Redirects to `/admin/beheer`.
- `POST /admin/beheerder-toevoegen` — add a new admin account; validates username not empty, password ≥ 12 chars, passwords match, username unique. Redirects to `/admin/beheer`.
- `POST /admin/beheerder-verwijderen/<id>` — delete an admin account; blocked if only 1 account exists or if deleting own account. Redirects to `/admin/beheer`.
- `POST /admin/wachtwoord-wijzigen` — change the logged-in admin's own password; requires current password, new password ≥ 12 chars, and matching confirmation. Increments `sessie_versie` in DB (invalidating all other active sessions of that user) while updating `session["admin_sessie_versie"]` for the current session so the changer stays logged in. Accessible via the "🔑 Wachtwoord" button in the topbar. Redirects to `/admin`.

`GET /api/beschikbaar` — public JSON endpoint returning `verkocht`, `beschikbaar`, `max_eendjes`, `max_per_bestelling`. Used by the homepage auto-refresh (every 30s).

`GET /robots.txt` — crawler directives: allows `/`, `/privacy`, `/voorwaarden`; blocks `/admin`, `/bestellen`, `/webhook`, `/api/`, `/betaald/`; references the sitemap.

`GET /sitemap.xml` — XML sitemap listing `/`, `/privacy`, `/voorwaarden` with `changefreq` and `priority`. Admin and transactional pages are excluded.

`GET /privacy` — AVG-compliant privacy policy (organisation details, data categories, legal bases, processors Mollie + Resend, retention periods, data subject rights).

`GET /voorwaarden` — General terms and conditions (Mollie requirement: organisation details with KvK 76404862, event description, ticket rules, payment, no-refund policy, liability, prize award).

### Database Backup (Litestream + Cloudflare R2)

`start.sh` downloads the Litestream binary at runtime (using `curl`, `wget`, or `python3` as fallback) and starts it as a supervisor process wrapping Gunicorn. Litestream replicates the SQLite WAL to Cloudflare R2 every second (`sync-interval: 1s`). Configuration is in `litestream.yml` (bucket, endpoint with hardcoded account ID, credentials from env). On startup, if `LITESTREAM_ACCESS_KEY_ID` is set and the database file does not exist, `start.sh` automatically restores it from R2 before starting Gunicorn. `nixpacks.toml` installs `curl` during the Railway build phase.

## Testing Notes

`tests/test_app.py` stubs out Mollie, Resend, Flask-WTF CSRF, and Flask-Limiter so tests run with only Flask installed. Tests cover pricing, input validation, database operations, atomic transactions, email sending, webhook processing, admin routes, `max_per_bestelling`/`max_eendjes` settings, price settings (`prijs_per_stuk`/`prijs_vijf_stuks`/`transactiekosten`) via admin, CSV injection escaping, CSV filename timestamp, CSV header columns, email header content (afbeelding/kleur/datum/locatie/tijd/praktische-info-icoon), notification email (instellen/validatie/versturen), email validation in wijzig_bestelling, opruimen, paginering, server-side statusfilter, CSP nonces, Permissions-Policy, session permanence, `saniteer_log`, legal pages (`/privacy`, `/voorwaarden`), footer content on `/betaald/<mollie_id>` and error pages (`fout.html`), collapsible `<details>` sections in admin, multi-account admin and password management (`TestBeheerderAccounts`, including `laatste_inlog` tests), security headers (`TestSecurityHeadersAanvullend`), email formatting variants (`TestBevestigingsmailOpmaakvarianten`), input boundary values (`TestValideerInvoerGrenswaardes`), atomic ticket assignment edge cases (`TestWijsLotnummersToeAanvullend`), admin page username display, password change persistence, betaald-page fallback errors, bestellen edge cases, admin settings validation, handmatige bestelling telefoon, webhook audit log, SEO meta-tags/robots.txt/sitemap.xml (`TestSeoEnRobots`), beheer-paginasplitsing en filterbalk-layout (`TestAdminBeheerPagina`), onderhoudsmodus aan/uit/503/admin-bypass/webhook-bypass (`TestOnderhoudsmodus`), sponsorstrip statisch/scroll/volgorde/bestandstype-filter (`TestSponsorStrip`), vallende eendjes animatie/accordion/afzendernaam/projectblokje (`TestRecenteWijzigingen`), setup-pagina token/validatie/aanmaken/invalidatie/tokenbestand (`TestSetupPagina`), betaald-tussenroute redirect/onbekend-id/null-mollie_id (`TestBetaaldRedirect`), instellingen transactie-atomiciteit en DB-foutafhandeling (`TestAdminInstellingenTransactie`). The test database uses `/tmp/eendjes_test.db` (reset before each test class). `maak_db()` includes all teller columns and seeds the `beheerders` table (incl. `laatste_inlog`). `conftest.py` cleans up the test database + WAL/SHM files before pytest starts (required for Python 3.14 + SQLite WAL mode). Total: 461 tests. Tests use `self.client.post("/admin/logout")` (POST-only since CSRF-logout fix).

### Sponsorstrip

Sponsors worden automatisch geladen vanuit `static/img/sponsors/`. Ondersteunde bestandstypen: `.png`, `.jpg`, `.jpeg`, `.svg`, `.webp`. Bestanden worden alfabetisch gesorteerd. De `index`-route scant de map en geeft `sponsor_bestanden` door aan de template.

- **≤ 4 sponsors**: statische gecentreerde rij (`sponsor-rij-statisch`), geen animatie. Minimumbreedte 160px per logo-blok zodat het er verzorgd uitziet met weinig sponsors.
- **≥ 5 sponsors**: oneindige CSS-scrollbanner (`sponsor-baan` + `sponsor-rij`), gedupliceerde lijst voor naadloze loop, pauzeert bij hover.
- **Geen sponsors**: sectie wordt volledig verborgen.

Logo's toevoegen: afbeelding in `static/img/sponsors/` plaatsen — geen code aanpassen nodig.

### Vallende eendjes animatie

Op de betaald-pagina (`/betaald/<mollie_id>`) vallen bij `status_klasse == 'succes'` 28 badeendjes van boven het scherm naar beneden. Geïmplementeerd via:
- CSS `@keyframes valEend` (altijd aanwezig in `<style>`)
- JavaScript (met CSP-nonce) dat `<img class="vallend-eendje">` elementen aanmaakt met willekeurige x-positie (%), grootte (28–48px), duur (2.4–4.6s) en vertraging (0–3s). Elementen verwijderen zichzelf na `animationend`.
- Animatie speelt eenmalig af (geen `infinite`), werkt op desktop en mobiel via `position: fixed` en `%`-breedte.

### Organisatiestructuur (naamgeving)

Consistent te gebruiken in alle communicatie:
- **Organisator race**: HGJB-commissie Hervormde Gemeente Wapenveld
- **Doel opbrengst**: diaconiaal project 'Ik geloof, ik deel'
- **Juridische entiteit** (footer, voorwaarden, privacy): Diaconie Hervormde gemeente te Wapenveld (KvK 76404862)
- **E-mail afzender** (`AFZENDER_NAAM`): `"Badeendjesrace Wapenveld"` (herkenbaarst in inbox)

## Key Patterns

- All comments and variable/function names are in Dutch (e.g., `bestelling` = order, `eendjes` = ducks/tickets, `lotnummer` = ticket number, `betaald` = paid)
- Security headers (X-Frame-Options, CSP with per-request nonces + `base-uri`/`form-action 'self' https://www.mollie.com https://pay.ideal.nl`, Permissions-Policy, suppressed `Server` header, etc.), rate limiting (5/min on admin login, 5/min on `/bestellen`, 30/min on `/betaald`, 60/min on `/webhook`), and ProxyFix (for Railway deployment) are all configured in `app.py`. **`pay.ideal.nl` must stay in `form-action`**: iDEAL 2.0 live payments redirect to `pay.ideal.nl`; browsers that enforce CSP `form-action` on redirect destinations (e.g. Firefox) block the navigation without it. `saniteer_log()` strips all ASCII control characters (0x00–0x1F) from user input before logging to prevent log/terminal injection. `GET /.well-known/security.txt` serves an RFC 9116-compliant security contact file.
- **Session security**: `session.clear()` is called before writing session variables on login (prevents session fixation). The `login_vereist` decorator validates `session["admin_sessie_versie"]` against the DB on every protected request — mismatches (e.g. after a password change) force re-login. Logout is POST-only (prevents CSRF logout via `<img>` tags). Admin login always runs a PBKDF2 hash check regardless of whether the username exists (prevents timing-based username enumeration). Accounts are locked for 15 minutes after 10 consecutive failed login attempts per username.
- **Env var validation**: `MAX_EENDJES`, `PRIJS_PER_STUK`, `PRIJS_VIJF_STUKS`, `TRANSACTIEKOSTEN` are validated as numeric with range checks on startup; a `ValueError` is raised immediately if an env var is invalid, preventing SQL interpolation with hostile values.
- **`/betaald/<mollie_id>`**: validates that `mollie_id` starts with `tr_` and is ≤ 64 chars before hitting the DB or Mollie API; returns 404 otherwise.
- **SEO**: `index.html` has `<meta name="description">`, `<link rel="canonical">`, Open Graph (`og:title/description/url/image/type/locale/site_name`), Twitter Card, and a JSON-LD `Event` schema block (with nonce). Admin, betaald, and fout pages carry `<meta name="robots" content="noindex, nofollow">`. Privacy/voorwaarden carry `noindex, follow` with a canonical. `inject_base_url()` context processor makes `{{ base_url }}` available in all templates. `/robots.txt` and `/sitemap.xml` are served as plain-text/XML routes.
- Mollie webhook IP allowlisting is deliberately **not used** (Mollie advises against it — IP ranges change without notice). Security relies on the protocol: the webhook only delivers a payment ID (`tr_…`), and the app always retrieves the payment status via an authenticated Mollie API call.
- The database is auto-initialized on module import (`init_db()` called at module level)
- Admin passwords are stored as Werkzeug PBKDF2 hashes in the `beheerders` table. `ADMIN_PASS` is only validated/used when the table is empty (first start); after that it can be removed from env vars. If `ADMIN_PASS` is not set and no accounts exist, the app starts in setup mode: a one-time token is generated, written to `.setup_token` (gitignored) so all gunicorn workers share the same token, and the URL is printed to stdout. `GET /setup?token=<token>` shows a form to create the first account. Once created, `_setup_token` is set to `None`, `.setup_token` is deleted, and the route returns 404.

### Mollie API v3

`mollie-api-python` v3 does **not** have `is_failed()`, `is_canceled()`, or `is_expired()` methods. Check payment status via `betaling.status` string directly (`"failed"`, `"canceled"`, `"expired"`). Only `is_paid()`, `is_pending()`, and `is_open()` exist.

**iDEAL 2.0 checkout redirect**: In live mode, `betaling.checkout_url` returns an iDEAL 2.0 URL of the form `https://pay.ideal.nl/transactions/https://tx.ideal.nl/...` — with a full URL embedded in the path. Flask's `redirect()` (via Werkzeug) percent-encodes the embedded `://` and `/`, producing a double-encoded broken URL. **Fix**: use `Response(status=302)` with `resp.headers["Location"] = betaling.checkout_url` directly, bypassing Werkzeug's URL encoding. This is already implemented in `/bestellen`.

**Price getters in 409 response**: The `/bestellen` 409 (sold out) render must use `get_transactiekosten()` / `get_prijs_per_stuk()` / `get_prijs_vijf_stuks()` — not the module-level constants — so live DB prices are shown after an admin update.

**`tk_bedrag` in fallback**: The `/betaald` fallback call to `stuur_bevestigingsmail()` must pass `tk_bedrag=rij["transactiekosten_bedrag"]` so the email shows the correct total when the iDEAL fee is included and the webhook was delayed.
