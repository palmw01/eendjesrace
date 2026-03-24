# Badeendjesrace – Lotenverkoop applicatie

Een webapplicatie voor de lotenverkoop van de **Badeendjesrace Wapenveld** (30 mei 2026).
Gebouwd met **Python/Flask**, **Mollie** (iDEAL-betalingen), **SQLite** en **Resend** (e-mail).

---

## Wat doet de app?

| Functie | |
|---|---|
| Bestelformulier voor kopers | ✅ |
| Live prijsberekening in de browser | ✅ |
| Optionele iDEAL-transactiekosten door koper | ✅ |
| iDEAL-betaling via Mollie | ✅ |
| Automatische lotnummer-toewijzing na betaling | ✅ |
| Bevestigingsmail met lotnummers via Resend | ✅ |
| Beheerpagina met statistieken, zoeken, filter en CSV-export | ✅ |
| Instellingen beheren via admin (max. eendjes, max. per bestelling, prijzen) | ✅ |
| Meerdere beheerdersaccounts aanmaken en verwijderen via admin | ✅ |
| Tweefactorauthenticatie (TOTP) per beheerdersaccount | ✅ |
| Audit-log van alle beheersacties (incl. IP-adres, kleurgecodeerd op ernst) | ✅ |
| Wachtwoord wijzigen via admin-topbar | ✅ |
| Handmatige bestellingen aanmaken (contant/overboeking) | ✅ |
| Automatische database-backup naar Cloudflare R2 via Litestream | ✅ |
| SEO-geoptimaliseerd (meta description, Open Graph, Twitter Card, JSON-LD, sitemap.xml, robots.txt) | ✅ |
| Sponsorstrip op homepage (automatisch geladen uit `static/img/sponsors/`, ≤4 statisch / ≥5 scrollend) | ✅ |
| Vallende badeendjes animatie op betaald-pagina na succesvolle betaling | ✅ |
| Onderhoudsmodus (admin toggle, toont 503 voor publieke routes) | ✅ |
| Bevestigingspagina met niet-raadbare URL (Mollie-ID) — bestellingen niet optelbaar | ✅ |
| Bestellingen-per-dag barchart in admin (laatste 30 dagen, puur CSS) | ✅ |
| Webhook-alarm stat in admin bij herhaalde webhook-verwerking (pogingen > 2) | ✅ |
| Dynamische browsertab-titel op betaald-pagina per betaalstatus | ✅ |

---

## Lokaal draaien

### 1. Virtualenv aanmaken en pakketten installeren

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
```

### 2. Configuratie via `config.json`

Maak een `config.json` aan in de projectmap (wordt automatisch ingeladen en staat in `.gitignore`):

```json
{
  "MOLLIE_API_KEY": "test_xxxxxxxxxxxxxxxxxxxx",
  "BASE_URL": "http://localhost:5000"
}
```

> Voor lokaal testen volstaat `MOLLIE_API_KEY: "test_dummy"` — betalingen werken dan niet maar de rest van de app wel.

### 3. Starten

```bash
.venv/bin/python app.py
```

De app draait op http://localhost:5000. De SQLite-database (`eendjes.db`) wordt automatisch aangemaakt.

### 4. Eerste beheerdersaccount instellen

Bij de **allereerste start** (lege database, geen `ADMIN_PASS` ingesteld) activeert de app automatisch de **setup-modus**. In de terminal verschijnt:

```
============================================================
⚠️  Geen beheerdersaccounts gevonden.
   Stel een initieel account in via:
   http://localhost:5000/setup?token=<eenmalig-token>
============================================================
```

Open de URL in je browser. Je ziet een formulier om een gebruikersnaam en wachtwoord (minimaal 12 tekens) in te stellen. Na het aanmaken word je doorgestuurd naar de login.

**Kenmerken van de setup-modus:**
- Het token wordt opgeslagen in `.setup_token` (staat in `.gitignore`) zodat alle gunicorn-workers hetzelfde token delen
- De setup-pagina geeft 404 zodra er een account bestaat — ook met het juiste token; `.setup_token` wordt dan automatisch verwijderd
- Alternatief: stel `ADMIN_PASS` (minimaal 12 tekens) in `config.json` in voor automatisch aanmaken bij eerste start

---

## Deployen op Railway

1. Maak een account op [railway.app](https://railway.app)
2. Nieuw project → **Deploy from GitHub** → selecteer deze repository
3. Voeg een **Volume** toe via **Add Service → Volume** en koppel dit aan `/app/data`
4. Stel onderstaande omgevingsvariabelen in via **Settings → Variables**
5. Kopieer de publieke Railway-URL en zet die als `BASE_URL`
6. Zorg dat er **geen Custom Start Command** is ingesteld — Railway gebruikt dan automatisch de `Procfile` (`web: bash start.sh`)

> **Eerste deploy zonder `ADMIN_PASS`:** De app start in setup-modus. Ga in Railway naar **Deployments → View Logs** en zoek naar de setup-URL (`/setup?token=…`). Open die URL in je browser om het eerste beheerdersaccount aan te maken. De URL is daarna ongeldig.

### Omgevingsvariabelen

| Variabele | Verplicht | Omschrijving |
|---|---|---|
| `MOLLIE_API_KEY` | Ja | Mollie API-sleutel (`test_…` of `live_…`) |
| `BASE_URL` | Ja | Publieke URL van de app (bijv. `https://xxx.railway.app`) |
| `RESEND_API_KEY` | Ja | Resend API-sleutel voor transactionele e-mail |
| `SECRET_KEY` | Ja | Willekeurige geheime sleutel voor sessies (gebruik een lange random string) |
| `RESEND_FROM` | Ja | Geverifieerd afzenderadres (bijv. `noreply@jouwdomein.nl`) |
| `ADMIN_PASS` | Nee | Initieel admin-wachtwoord (minimaal 12 tekens). Als dit niet ingesteld is én er geen accounts bestaan, start de app in setup-modus: een eenmalig token wordt gelogd naar de console en `/setup?token=…` toont een formulier. |
| `ADMIN_USER` | Nee | Initiële admin-gebruikersnaam bij automatisch aanmaken (standaard: `admin`). Alleen relevant als `ADMIN_PASS` ook ingesteld is. |
| `DATABASE` | Nee | Pad naar de SQLite-database. Zet op `/app/data/eendjes.db` als volume op `/app/data` gemount is. |
| `HTTPS` | Nee | Zet op `true` in productie — beveiligt sessie-cookies |
| `LITESTREAM_ACCESS_KEY_ID` | Nee | Cloudflare R2 Access Key ID voor automatische database-backup |
| `LITESTREAM_SECRET_ACCESS_KEY` | Nee | Cloudflare R2 Secret Access Key voor automatische database-backup |
| `TZ` | Nee | Tijdzone voor juiste timestamps (bijv. `Europe/Amsterdam`) |
| `MAX_EENDJES` | Nee | Totaal beschikbare eendjes bij eerste start (standaard: `3000`). Daarna via admin te wijzigen. |
| `PRIJS_PER_STUK` | Nee | Prijs per los eendje bij eerste start (standaard: `2.50`). Daarna via admin te wijzigen. |
| `PRIJS_VIJF_STUKS` | Nee | Prijs voor bundel van 5 eendjes bij eerste start (standaard: `10.00`). Daarna via admin te wijzigen. |
| `TRANSACTIEKOSTEN` | Nee | iDEAL-transactiekosten bij eerste start (standaard: `0.32`). Daarna via admin te wijzigen. |
| `SECURITY_CONTACT` | Nee | Contactadres voor `/.well-known/security.txt`. Valt terug op `RESEND_FROM`. |

---

## Database-backup (Litestream + Cloudflare R2)

De app maakt gebruik van [Litestream](https://litestream.io) voor near-realtime backup van de SQLite-database naar Cloudflare R2.

**Hoe het werkt:**
- `start.sh` downloadt de Litestream binary bij opstarten en start hem als supervisor-proces om Gunicorn heen
- Litestream repliceert de SQLite WAL elke seconde naar R2 (bucket `badeendjesracewapenveld`)
- Als bij een herstart de database ontbreekt (bijv. na volume-verlies), herstelt `start.sh` hem automatisch vanuit R2 vóór Gunicorn start

**R2-token aanmaken:**
1. Ga naar Cloudflare Dashboard → R2 → **Manage R2 API tokens**
2. Klik **Create API token**
3. Kies permission: **Object Read & Write**, scope op bucket `badeendjesracewapenveld`
4. Kopieer de **Access Key ID** en **Secret Access Key** direct — de secret wordt maar één keer getoond
5. Zet deze als `LITESTREAM_ACCESS_KEY_ID` en `LITESTREAM_SECRET_ACCESS_KEY` in Railway

> ⚠️ Gebruik een **R2-specifiek token** (via de R2-pagina), niet een algemeen Cloudflare API-token.

**Handmatig herstellen** (bijv. lokaal na een calamiteit):
```bash
LITESTREAM_ACCESS_KEY_ID=xxx \
LITESTREAM_SECRET_ACCESS_KEY=yyy \
DATABASE=/tmp/herstel.db \
litestream restore -config litestream.yml /tmp/herstel.db
```

---

## Beheerdersaccounts

De app ondersteunt meerdere beheerdersaccounts. Wachtwoorden worden gehasht opgeslagen (Werkzeug PBKDF2).

- **Accounts beheren**: inloggen → admin-paneel → sectie "Beheerders"
- **Wachtwoord wijzigen**: knop "🔑 Wachtwoord" rechtsboven in de topbar
- **2FA instellen**: inloggen → Beheer → sectie "Beheerders" → "2FA inschakelen". Scan de QR-code met Google Authenticator, Aegis, Authy of een andere TOTP-app. Na activering wordt bij elke login een 6-cijferige code gevraagd naast het wachtwoord.
- **Account lockout**: na 10 opeenvolgende mislukte loginpogingen wordt een account 15 minuten geblokkeerd
- **Database reset** verwijdert **geen** beheerdersaccounts — alleen bestellingen en webhook-log worden gewist

---

## Pagina's en routes

| URL | Omschrijving |
|---|---|
| `/` | Bestelformulier voor kopers |
| `/betaald/<mollie_id>` | Bevestigingspagina na betaling (URL bevat Mollie-betaal-ID, niet-raadbaar) |
| `/betaald/r/<id>` | Tussenroute voor Mollie redirect — stuurt door naar `/betaald/<mollie_id>` |
| `/privacy` | Privacyverklaring (AVG) |
| `/voorwaarden` | Algemene voorwaarden |
| `/api/prijs` | Live prijsberekening (JSON) |
| `/api/beschikbaar` | Actueel aantal beschikbare eendjes (JSON, elke 30s door homepage gebruikt) |
| `/robots.txt` | Crawler-instructies (blokkeert admin/bestellen/betaald, verwijst naar sitemap) |
| `/sitemap.xml` | XML-sitemap met openbare pagina's (`/`, `/privacy`, `/voorwaarden`) |
| `/admin` | Bestellingenpagina — statistieken, bestellingen, zoeken, filter, CSV-download |
| `/admin/beheer` | Beheerpagina — instellingen, beheerders (incl. 2FA), audit-log, gevaarzone |
| `/admin/export-csv` | Download alle bestellingen als CSV |
| `/admin/bestelling/<id>/wijzigen` | Bewerk naam, e-mail, telefoon, status of mailstatus |
| `/admin/instellingen` | Wijzig totaal beschikbare eendjes, maximum per bestelling en prijzen |
| `/admin/opruimen` | Verwijder verlopen/mislukte/geannuleerde bestellingen zonder lotnummers |
| `/admin/handmatig` | Maak handmatige bestelling aan (contant/overboeking) |
| `/admin/reset` | Reset volledige database — bestellingen en webhook-log (beheerdersaccounts blijven intact) |
| `/admin/beheerder-toevoegen` | Nieuw beheerdersaccount aanmaken |
| `/admin/beheerder-verwijderen/<id>` | Beheerdersaccount verwijderen |
| `/admin/wachtwoord-wijzigen` | Eigen wachtwoord wijzigen |
| `/admin/2fa/instellen` | 2FA-instelpagina met QR-code |
| `/admin/2fa/bevestigen` | Activeer 2FA na verificatie van de eerste code |
| `/admin/2fa/uitschakelen` | Schakel 2FA uit met huidige TOTP-code |
| `/admin/login/totp` | Tweede loginstap (TOTP-code) wanneer 2FA actief is |
| `/admin/beheerder-wachtwoord-reset/<id>` | Reset wachtwoord van een ander beheerdersaccount |
| `/admin/audit-wissen` | Wis de volledige audit-log |
| `/setup` | Eenmalig formulier voor aanmaken eerste beheerdersaccount (vereist token uit console) |
| `/health` | Health check endpoint — controleert DB en Mollie bereikbaarheid (JSON) |
| `/.well-known/security.txt` | Beveiligingscontactinformatie (RFC 9116) |

---

## Projectstructuur

```
eendjesrace/
├── app.py                  # Flask backend (alle logica)
├── requirements.txt        # Python-pakketten
├── Procfile                # Railway startcommando (roept start.sh aan)
├── start.sh                # Opstartscript: downloadt Litestream, herstel DB, start Gunicorn
├── litestream.yml          # Litestream-configuratie (R2-bucket, endpoint, credentials)
├── nixpacks.toml           # Railway build-config (installeert curl)
├── README.md
├── CLAUDE.md               # Instructies voor Claude Code
├── static/
│   └── img/
│       ├── sponsors/       # Sponsorlogo's (png/jpg/svg/webp) — automatisch geladen
│       └── eend.png        # Badeend-afbeelding
└── templates/
    ├── index.html          # Bestelformulier
    ├── betaald.html        # Bevestigingspagina (incl. vallende eendjes animatie)
    ├── privacy.html        # Privacyverklaring (AVG)
    ├── voorwaarden.html    # Algemene voorwaarden
    ├── admin.html          # Bestellingenpagina
    ├── admin_beheer.html   # Beheerpagina (instellingen, beheerders, audit-log, gevaarzone)
    ├── admin_login.html    # Admin-login (stap 1: gebruikersnaam + wachtwoord)
    ├── admin_login_totp.html  # Admin-login (stap 2: TOTP-code bij actieve 2FA)
    ├── admin_2fa_instellen.html  # 2FA-instelpagina met QR-code
    ├── wijzigen.html       # Bestelling bewerken
    ├── onderhoud.html      # Onderhoudspagina (503)
    └── fout.html           # Foutpagina's (404, 500, …)
```

---

## Beveiliging

| Aanvalsvector | Maatregel |
|---|---|
| **Order injection** (bestelling zonder betaling) | Lotnummers worden uitsluitend toegewezen na verificatie bij de Mollie API — nooit op basis van POST-data |
| **Webhook spoofing** | Webhook vertrouwt alleen op `mollie.payments.get()` via authenticated API-call; de POST-body bevat enkel het `id` |
| **Admin-toegang** | Wachtwoord + optionele TOTP-2FA; PBKDF2-hash; rate limiting (5/min op login én TOTP-stap); 4-uurs sessieverval; HTTPS-only cookie in productie |
| **Session fixation** | `session.clear()` bij elke succesvolle login voorkomt dat een aanvaller een bekende session-ID kan injecteren |
| **Sessie-invalidatie** | `sessie_versie` in DB wordt opgehoogd bij wachtwoordwijziging — alle andere actieve sessies van die gebruiker worden direct ongeldig |
| **Account lockout** | Na 10 opeenvolgende mislukte loginpogingen wordt het account 15 minuten geblokkeerd |
| **Timing-aanval op gebruikersnamen** | Login voert altijd een PBKDF2-hash-check uit, ook als de gebruikersnaam niet bestaat |
| **CSRF** | Flask-WTF CSRFProtect op alle formulieren; webhook is bewust uitgezonderd (Mollie kan geen token meesturen); logout is POST-only |
| **SQL injection** | Alle queries gebruiken parameterized statements (`?`); sorteervelden gewhitelisted |
| **XSS** | CSP met per-request nonce voor scripts; `html.escape()` op gebruikersinvoer in e-mails |
| **Clickjacking** | `X-Frame-Options: DENY` + `frame-ancestors 'none'` in CSP |
| **Log injection** | `saniteer_log()` verwijdert alle ASCII-stuurcodes (0x00–0x1F) uit gelogde gebruikersinvoer |
| **Fingerprinting** | `Server`-header onderdrukt |
| **iDEAL 2.0 redirect** | `pay.ideal.nl` in CSP `form-action` (Firefox blokkeert redirect anders) |
| **Bestellingenopsomming** | `/betaald/<mollie_id>` gebruikt het niet-raadbare Mollie-betaal-ID — oplopende order-IDs worden nooit blootgesteld in de browser-URL |
| **IP-adres achter Cloudflare** | `get_client_ip()` leest `CF-Connecting-IP` (Cloudflare) en valt terug op `remote_addr` (Railway load balancer via ProxyFix) |
| **Audit-trail** | Alle beheersacties worden gelogd in `audit_log` (tijdstip, gebruiker, actie, details, IP) — zichtbaar op `/admin/beheer` |

---

## Prijsberekening

| Aantal | Prijs |
|---|---|
| 1–4 stuks | €2,50 per stuk (instelbaar) |
| 5 stuks | €10,00 bundel (instelbaar) |
| Meer dan 5 | Combinatie van bundels en losse stuks |

Optioneel kan de koper de iDEAL-transactiekosten (standaard €0,32, instelbaar) zelf betalen. Alle prijzen zijn instelbaar via de beheerpagina of via omgevingsvariabelen bij eerste opstart.

---

## Tests uitvoeren

```bash
# Virtualenv aanmaken (eenmalig)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest

# Tests draaien
PYTHONPATH=. .venv/bin/pytest tests/test_app.py -v
# of zonder pytest:
PYTHONPATH=. .venv/bin/python tests/test_app.py
```

De testsuite stubt Mollie, Resend, Flask-WTF en Flask-Limiter. `conftest.py` zorgt voor automatische testdatabase-cleanup (vereist voor Python 3.14 + SQLite WAL mode). **507 tests.**
