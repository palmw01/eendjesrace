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
| Stille kopie-mail naar beheerder bij elke bestelling (optioneel) | ✅ |
| Beheerpagina met statistieken, zoeken, filter en CSV-export | ✅ |
| Instellingen beheren via admin (max. eendjes, max. per bestelling, prijzen, notificatieadres) | ✅ |
| Meerdere beheerdersaccounts aanmaken en verwijderen via admin | ✅ |
| Wachtwoord wijzigen via admin-topbar | ✅ |
| Automatische database-backup naar Cloudflare R2 via Litestream | ✅ |
| SEO-geoptimaliseerd (meta description, Open Graph, Twitter Card, JSON-LD, sitemap.xml, robots.txt) | ✅ |

---

## Lokaal draaien

### 1. Pakketten installeren

```bash
pip install -r requirements.txt
```

### 2. Configuratie

Maak een `.env`-bestand of stel omgevingsvariabelen in:

```bash
export MOLLIE_API_KEY="test_xxxxxxxxxxxxxxxxxxxx"
export BASE_URL="http://localhost:5000"
export RESEND_API_KEY="re_xxxxxxxxxxxxxxxxxxxx"
export RESEND_FROM="noreply@jouwdomein.nl"
export ADMIN_USER="admin"
export ADMIN_PASS="kieseen sterk wachtwoord"   # minimaal 12 tekens
export SECRET_KEY="willekeurige lange string"
```

> `ADMIN_USER` en `ADMIN_PASS` zijn alleen nodig bij de **eerste start** (lege database). Zodra er beheerdersaccounts in de database staan, kunnen ze worden weggelaten.

### 3. Starten

```bash
python app.py
```

De app draait op http://localhost:5000. De SQLite-database (`eendjes.db`) wordt automatisch aangemaakt.

---

## Deployen op Railway

1. Maak een account op [railway.app](https://railway.app)
2. Nieuw project → **Deploy from GitHub** → selecteer deze repository
3. Voeg een **Volume** toe via **Add Service → Volume** en koppel dit aan `/app/data`
4. Stel onderstaande omgevingsvariabelen in via **Settings → Variables**
5. Kopieer de publieke Railway-URL en zet die als `BASE_URL`
6. Zorg dat er **geen Custom Start Command** is ingesteld — Railway gebruikt dan automatisch de `Procfile` (`web: bash start.sh`)

### Omgevingsvariabelen

| Variabele | Verplicht | Omschrijving |
|---|---|---|
| `MOLLIE_API_KEY` | Ja | Mollie API-sleutel (`test_…` of `live_…`) |
| `BASE_URL` | Ja | Publieke URL van de app (bijv. `https://xxx.railway.app`) |
| `RESEND_API_KEY` | Ja | Resend API-sleutel voor transactionele e-mail |
| `SECRET_KEY` | Ja | Willekeurige geheime sleutel voor sessies (gebruik een lange random string) |
| `RESEND_FROM` | Ja | Geverifieerd afzenderadres (bijv. `noreply@jouwdomein.nl`) |
| `ADMIN_PASS` | Eerste start | Initieel admin-wachtwoord (minimaal 12 tekens). Alleen vereist als de database nog leeg is. Kan worden verwijderd zodra er een beheerdersaccount bestaat. |
| `ADMIN_USER` | Eerste start | Initiële admin-gebruikersnaam (standaard: `admin`). Kan worden verwijderd na eerste start. |
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
- **Database reset** verwijdert **geen** beheerdersaccounts — alleen bestellingen en webhook-log worden gewist

---

## Pagina's en routes

| URL | Omschrijving |
|---|---|
| `/` | Bestelformulier voor kopers |
| `/betaald/<id>` | Bevestigingspagina na betaling |
| `/privacy` | Privacyverklaring (AVG) |
| `/voorwaarden` | Algemene voorwaarden |
| `/api/prijs` | Live prijsberekening (JSON) |
| `/api/beschikbaar` | Actueel aantal beschikbare eendjes (JSON, elke 30s door homepage gebruikt) |
| `/robots.txt` | Crawler-instructies (blokkeert admin/bestellen/betaald, verwijst naar sitemap) |
| `/sitemap.xml` | XML-sitemap met openbare pagina's (`/`, `/privacy`, `/voorwaarden`) |
| `/admin` | Beheerpagina — statistieken, bestellingen, zoeken, filter, CSV-download |
| `/admin/export-csv` | Download alle bestellingen als CSV |
| `/admin/bestelling/<id>/wijzigen` | Bewerk naam, e-mail, telefoon, status of mailstatus |
| `/admin/instellingen` | Wijzig totaal beschikbare eendjes, maximum per bestelling, prijzen en notificatie-e-mailadres |
| `/admin/opruimen` | Verwijder verlopen/mislukte/geannuleerde bestellingen zonder lotnummers |
| `/admin/handmatig` | Maak handmatige bestelling aan (contant/overboeking) |
| `/admin/reset` | Reset volledige database — bestellingen en webhook-log (beheerdersaccounts blijven intact) |
| `/admin/beheerder-toevoegen` | Nieuw beheerdersaccount aanmaken |
| `/admin/beheerder-verwijderen/<id>` | Beheerdersaccount verwijderen |
| `/admin/wachtwoord-wijzigen` | Eigen wachtwoord wijzigen |
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
└── templates/
    ├── index.html          # Bestelformulier
    ├── betaald.html        # Bevestigingspagina
    ├── privacy.html        # Privacyverklaring (AVG)
    ├── voorwaarden.html    # Algemene voorwaarden
    ├── admin.html          # Beheerpagina
    ├── wijzigen.html       # Bestelling bewerken
    ├── admin_login.html    # Admin-login
    └── fout.html           # Foutpagina's (404, 500, …)
```

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
python -m pytest tests/test_app.py -v
# of zonder pytest:
python tests/test_app.py
```

De testsuite stubt Mollie, Resend, Flask-WTF en Flask-Limiter — alleen Flask en Werkzeug hoeven geïnstalleerd te zijn. 387 tests.
