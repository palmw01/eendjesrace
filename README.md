# Eendjesrace – Lotenverkoop applicatie

Een webapplicatie voor de lotenverkoop van de **Badeendjesrace Wapenveld** (30 mei 2026).
Gebouwd met **Python/Flask**, **Mollie** (iDEAL-betalingen), **SQLite** en **Resend** (e-mail).

---

## Wat doet de app?

| Functie | |
|---|---|
| Bestelformulier voor kopers | ✅ |
| Live prijsberekening in de browser | ✅ |
| Optionele iDEAL-transactiekosten (€0,32) door koper | ✅ |
| iDEAL-betaling via Mollie | ✅ |
| Automatische lotnummer-toewijzing na betaling | ✅ |
| Bevestigingsmail met lotnummers via Resend | ✅ |
| Beheerpagina met statistieken, filter en CSV-export | ✅ |
| Instellingen beheren via admin (max. eendjes, max. per bestelling) | ✅ |

---

## Lokaal draaien

### 1. Pakketten installeren

```bash
pip install -r requirements.txt
```

### 2. Configuratie

Maak een `config.json` aan in de projectmap (wordt automatisch ingelezen):

```json
{
  "MOLLIE_API_KEY": "test_xxxxxxxxxxxxxxxxxxxx",
  "BASE_URL": "http://localhost:5000",
  "RESEND_API_KEY": "re_xxxxxxxxxxxxxxxxxxxx",
  "RESEND_FROM": "noreply@jouwdomein.nl",
  "ADMIN_USER": "admin",
  "ADMIN_PASS": "kieseen sterk wachtwoord",
  "SECRET_KEY": "willekeurige lange string"
}
```

### 3. Starten

```bash
python app.py
```

De app draait op http://localhost:5000. De SQLite-database (`eendjes.db`) wordt automatisch aangemaakt.

---

## Deployen op Railway

1. Maak een account op [railway.app](https://railway.app)
2. Nieuw project → **Deploy from GitHub** → selecteer deze repository
3. Stel onderstaande omgevingsvariabelen in via **Settings → Variables**
4. Kopieer de publieke Railway-URL en zet die als `BASE_URL`

### Omgevingsvariabelen

| Variabele | Verplicht | Omschrijving |
|---|---|---|
| `MOLLIE_API_KEY` | Ja | Mollie API-sleutel (`test_…` of `live_…`) |
| `BASE_URL` | Ja | Publieke URL van de app (bijv. `https://xxx.railway.app`) |
| `RESEND_API_KEY` | Ja | Resend API-sleutel voor transactionele e-mail |
| `ADMIN_PASS` | Ja | Wachtwoord voor de beheerpagina (minimaal 12 tekens) |
| `SECRET_KEY` | Ja | Willekeurige geheime sleutel voor sessies (gebruik een lange random string) |
| `RESEND_FROM` | Ja | Geverifieerd afzenderadres (bijv. `noreply@jouwdomein.nl`) |
| `ADMIN_USER` | Nee | Gebruikersnaam admin (standaard: `admin`) |
| `DATABASE` | Nee | Pad naar de SQLite-database (standaard: `eendjes.db`) |
| `HTTPS` | Nee | Zet op `true` in productie — beveiligt sessie-cookies |
| `TZ` | Nee | Tijdzone voor juiste timestamps (bijv. `Europe/Amsterdam`) |
| `MAX_EENDJES` | Nee | Beginstaat totaal beschikbare eendjes (standaard: `3000`). Alleen relevant bij de allereerste start — daarna via de admin te wijzigen. |
| `SECURITY_CONTACT` | Nee | Contactadres voor `/.well-known/security.txt` (bijv. `mailto:admin@jouwdomein.nl`). Valt terug op `RESEND_FROM`. |

> **Mollie webhook:** Railway geeft automatisch een publieke URL. Zet deze als `BASE_URL` zodat Mollie betalingsstatussen kan terugsturen. Gebruik de `live_`-sleutel pas zodra de app live staat.

> **Resend:** Verifieer je domein in het Resend-dashboard. Zonder geverifieerd domein werkt `onboarding@resend.dev` tijdelijk als afzender, maar dan gaan mails alleen naar je eigen Resend-accountadres.

---

## Pagina's en routes

| URL | Omschrijving |
|---|---|
| `/` | Bestelformulier voor kopers |
| `/betaald/<id>` | Bevestigingspagina na betaling |
| `/api/prijs` | Live prijsberekening (JSON) |
| `/api/beschikbaar` | Actueel aantal beschikbare eendjes (JSON, elke 30s door homepage gebruikt) |
| `/admin` | Beheerpagina — statistieken, bestellingen, filter op status |
| `/admin/export-csv` | Download alle bestellingen als CSV |
| `/admin/bestelling/<id>/wijzigen` | Bewerk naam, e-mail, telefoon, status of mailstatus |
| `/admin/instellingen` | Wijzig totaal beschikbare eendjes en maximum per bestelling |
| `/admin/opruimen` | Verwijder verlopen/mislukte/geannuleerde bestellingen zonder lotnummers |
| `/admin/reset` | Reset volledige database (vereist 'RESET'-bevestiging) |
| `/.well-known/security.txt` | Beveiligingscontactinformatie (RFC 9116) |

---

## Projectstructuur

```
eendjesrace/
├── app.py                  # Flask backend (alle logica)
├── requirements.txt        # Python-pakketten
├── Procfile                # Railway/gunicorn startcommando
├── eendjes.db              # SQLite database (automatisch aangemaakt)
├── README.md
├── CLAUDE.md               # Instructies voor Claude Code
└── templates/
    ├── index.html          # Bestelformulier
    ├── betaald.html        # Bevestigingspagina
    ├── admin.html          # Beheerpagina
    ├── wijzigen.html       # Bestelling bewerken
    ├── login.html          # Admin-login
    └── fout.html           # Foutpagina's (404, 500, …)
```

---

## Prijsberekening

| Aantal | Prijs |
|---|---|
| 1–4 stuks | €2,50 per stuk |
| 5 stuks | €10,00 (bundel) |
| Meer dan 5 | Combinatie van bundels en losse stuks |

Optioneel kan de koper de iDEAL-transactiekosten (€0,32) zelf betalen, zodat het volledige bedrag naar het goede doel gaat.

---

## Tests uitvoeren

```bash
python -m pytest tests/test_app.py -v
```

De testsuite stubt Mollie, Resend, Flask-WTF en Flask-Limiter — alleen Flask hoeft geïnstalleerd te zijn.
