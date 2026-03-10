# 🦆 Eendjesrace – Lotenverkoop applicatie

Een complete webapplicatie voor de lotenverkoop van de **Badeendjesrace Wapenveld** (30 mei 2026).
Gebouwd met **Python/Flask** + **Mollie** (iDEAL) + **SQLite** + **Resend** (e-mail).

---

## ✅ Wat doet de app automatisch?

| Stap | |
|------|-|
| Bestelling ontvangen | ✅ Ingebouwd formulier |
| Prijs berekenen | ✅ Live in de browser |
| Betaalverzoek sturen | ✅ Directe iDEAL-betaling |
| Lotnummers toewijzen | ✅ Direct na betaling |
| Bevestiging sturen | ✅ Automatische e-mail via Resend |
| Overzicht bijhouden | ✅ Admin-pagina (/admin) |

---

## 🚀 Installatie & starten

### 1. Python-pakketten installeren

```bash
pip install -r requirements.txt
```

### 2. Omgevingsvariabelen instellen

Maak een `.env`-bestand aan (of zet ze als omgevingsvariabele):

```env
# Mollie API-sleutel (haal op via mollie.com → Dashboard → API-sleutels)
# Begin met 'test_' om te testen, gebruik 'live_' voor echte betalingen
MOLLIE_API_KEY=test_xxxxxxxxxxxxxxxxxxxx

# De publieke URL van jouw server (belangrijk voor Mollie webhook!)
BASE_URL=https://jouwdomein.nl

# Resend e-mail (haal op via resend.com → API Keys)
RESEND_API_KEY=re_xxxxxxxxxxxxxxxxxxxx
RESEND_FROM=noreply@jouwdomein.nl
```

> **Resend instellen:**
> Maak een gratis account op [resend.com](https://resend.com), verifieer je domein en maak een API key aan.
> Zonder geverifieerd domein kun je tijdelijk `onboarding@resend.dev` gebruiken als `RESEND_FROM` (mails gaan dan alleen naar je eigen Resend-account e-mailadres).

### 3. App starten

```bash
python app.py
```

De app draait nu op http://localhost:5000

---

## 🌐 Online zetten (voor iDEAL)

Voor echte betalingen moet de app bereikbaar zijn via internet  
zodat Mollie de webhook kan aanroepen.

**Goedkope opties:**
- **Railway.app** – gratis tier, heel eenvoudig deployen
- **Render.com** – gratis tier, Python/Flask support
- **PythonAnywhere** – €5/maand, speciaal voor Python

**Stappenplan Railway:**
1. Maak account op railway.app
2. Nieuw project → Deploy from GitHub
3. Stel omgevingsvariabelen in via het dashboard
4. Kopieer de publieke URL → zet die als `BASE_URL`

---

## 📄 Pagina's

| URL | Omschrijving |
|-----|-------------|
| `/` | Bestelformulier voor kopers |
| `/admin` | Overzicht van alle bestellingen en omzet |
| `/betaald/<id>` | Bevestigingspagina na betaling |

> ⚠️ **Beveilig `/admin`** in productie met een wachtwoord!  
> Voeg Flask-Login of een simpele HTTP Basic Auth toe.

---

## 💰 Mollie-account aanmaken

1. Ga naar [mollie.com](https://mollie.com) en maak een gratis account
2. Verificeer je organisatie (KvK-nummer)
3. Ga naar **Dashboard → Ontwikkelaars → API-sleutels**
4. Gebruik eerst `test_` sleutel om te testen
5. Schakel over naar `live_` sleutel voor echte betalingen

**Kosten:** €0,29 per succesvolle iDEAL-transactie, geen maandelijkse kosten.

---

## 🔧 Aanpassen

In `app.py` bovenaan staan alle instellingen:

```python
MAX_EENDJES      = 3000      # Maximaal aantal te verkopen eendjes
PRIJS_PER_STUK   = 2.50      # Prijs per los eendje
PRIJS_VIJF_STUKS = 10.00     # Prijs voor 5 eendjes
```

---

## 📁 Projectstructuur

```
eendjes/
├── app.py                  # Flask backend (alle logica)
├── requirements.txt        # Python-pakketten
├── eendjes.db              # SQLite database (wordt automatisch aangemaakt)
├── README.md               # Dit bestand
└── templates/
    ├── index.html          # Bestelformulier
    ├── betaald.html        # Bevestigingspagina
    └── admin.html          # Beheerpagina
```
