# 🦆 Eendjesrace – Lotenverkoop applicatie

Een complete webapplicatie voor de lotenverkoop van de eendjesrace.  
Gebouwd met **Python/Flask** + **Mollie** (iDEAL) + **SQLite**.

---

## ✅ Wat doet de app automatisch?

| Stap | Handmatig vroeger | Nu automatisch |
|------|------------------|----------------|
| Bestelling ontvangen | Google Forms | ✅ Ingebouwd formulier |
| Prijs berekenen | Handmatig | ✅ Live in de browser |
| Betaalverzoek sturen | Handmatig per persoon | ✅ Directe iDEAL-betaling |
| Lotnummers toewijzen | Handmatig in Excel | ✅ Direct na betaling |
| Bevestiging sturen | Handmatig e-mail | ✅ Automatische e-mail |
| Overzicht bijhouden | Excel bijwerken | ✅ Admin-pagina (/admin) |

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

# E-mail instellingen (bijv. Gmail)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=jouwemail@gmail.com
SMTP_PASS=jouw-app-wachtwoord   # Gebruik een Gmail App Password, niet je gewone wachtwoord
```

> **Gmail App Password aanmaken:**  
> Google-account → Beveiliging → 2-stapsverificatie → App-wachtwoorden

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
