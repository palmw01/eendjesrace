"""
Badeendjes Lotenverkoop - Flask Backend
Vereisten: pip install flask flask-wtf flask-limiter mollie-api-python
"""

import os
import csv
import html
import io
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

# ─── Laad config.json als omgevingsvariabelen (overschrijft geen bestaande vars)
_config_pad = os.path.join(os.path.dirname(__file__), "config.json")
if os.path.exists(_config_pad):
    with open(_config_pad) as _f:
        for _k, _v in json.load(_f).items():
            os.environ.setdefault(_k, str(_v))
import base64
import logging
import secrets
import pyotp
import qrcode
import resend
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from logging.handlers import RotatingFileHandler
from flask import (
    Flask, request, render_template, redirect,
    url_for, jsonify, session, abort, g, Response, flash, get_flashed_messages
)
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from mollie.api.client import Client
from mollie.api.error import Error as MollieError  # RequestSetupError/ResponseError bestaan niet in v3

# ─── App initialisatie ────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"]               = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"]  = True
app.config["SESSION_COOKIE_SAMESITE"]  = "Lax"
app.config["SESSION_COOKIE_SECURE"]    = os.environ.get("HTTPS", "false").lower() == "true"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=4)
app.config["WTF_CSRF_TIME_LIMIT"]      = 3600

csrf    = CSRFProtect(app)
# BUG-FIX: X-Forwarded-For spoofing — zonder ProxyFix kan een aanvaller
# 'X-Forwarded-For: 127.0.0.1' meesturen en zo de IP-whitelist omzeilen.
# ProxyFix(x_for=1) vertrouwt exact 1 proxy-hop en laat Flask's
# request.remote_addr automatisch de echte client-IP bevatten.
# Op Railway staat precies 1 proxy (hun load balancer) voor de app.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per hour"],
    storage_uri="memory://",
)

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_DIR = os.environ.get("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
formatter       = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
bestand_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "eendjes.log"), maxBytes=5 * 1024 * 1024, backupCount=5
)
bestand_handler.setFormatter(formatter)
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
app.logger.setLevel(logging.INFO)
app.logger.addHandler(bestand_handler)
app.logger.addHandler(console_handler)

# ─── Configuratie ────────────────────────────────────────────────────────────
MOLLIE_API_KEY   = os.environ.get("MOLLIE_API_KEY", "")
BASE_URL         = os.environ.get("BASE_URL", "http://localhost:5000")
try:
    MAX_EENDJES = int(os.environ.get("MAX_EENDJES", 3000))
    if MAX_EENDJES < 1:
        raise ValueError("MAX_EENDJES moet minimaal 1 zijn")
except (ValueError, TypeError) as _e:
    raise ValueError(f"Ongeldige waarde voor MAX_EENDJES: {_e}") from _e
try:
    PRIJS_PER_STUK = float(os.environ.get("PRIJS_PER_STUK", "2.50"))
    if PRIJS_PER_STUK <= 0:
        raise ValueError("PRIJS_PER_STUK moet groter dan 0 zijn")
except (ValueError, TypeError) as _e:
    raise ValueError(f"Ongeldige waarde voor PRIJS_PER_STUK: {_e}") from _e
try:
    PRIJS_VIJF_STUKS = float(os.environ.get("PRIJS_VIJF_STUKS", "10.00"))
    if PRIJS_VIJF_STUKS <= 0:
        raise ValueError("PRIJS_VIJF_STUKS moet groter dan 0 zijn")
except (ValueError, TypeError) as _e:
    raise ValueError(f"Ongeldige waarde voor PRIJS_VIJF_STUKS: {_e}") from _e
try:
    TRANSACTIEKOSTEN = float(os.environ.get("TRANSACTIEKOSTEN", "0.32"))  # iDEAL-transactiekosten Mollie
    if TRANSACTIEKOSTEN < 0:
        raise ValueError("TRANSACTIEKOSTEN mag niet negatief zijn")
except (ValueError, TypeError) as _e:
    raise ValueError(f"Ongeldige waarde voor TRANSACTIEKOSTEN: {_e}") from _e

RESEND_API_KEY   = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM      = os.environ.get("RESEND_FROM", "")
AFZENDER_NAAM    = "Badeendjesrace Wapenveld"

ADMIN_GEBRUIKER  = os.environ.get("ADMIN_USER", "admin")
ADMIN_WACHTWOORD = os.environ.get("ADMIN_PASS", "")

# Eenmalig setup-token; opgeslagen naast de database zodat alle gunicorn-workers
# hetzelfde token zien. None = geen setup nodig of al gedaan.
_setup_token: str | None = None
_SETUP_TOKEN_BESTAND = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".setup_token")

DATABASE         = os.environ.get("DATABASE", "eendjes.db")

# ─── Mollie client factory ────────────────────────────────────────────────────
def maak_mollie_client() -> Client:
    """
    Geeft een geconfigureerde Mollie Client terug.
    Gebruik deze functie overal in plaats van Client() direct aan te roepen —
    zo wordt de API-sleutel nooit per ongeluk hardcoded meegegeven.
    """
    if not MOLLIE_API_KEY:
        raise RuntimeError("MOLLIE_API_KEY is niet ingesteld")
    mollie = Client()
    mollie.set_api_key(MOLLIE_API_KEY)
    return mollie


# IP-allowlisting voor Mollie webhooks is bewust weggelaten.
# Mollie raadt dit zelf af (zie ip-ranges.mollie.com): IP-reeksen wijzigen zonder
# aankondiging. De beveiliging zit in het protocol zelf: de webhook levert alleen
# een betaal-ID (tr_…), en de app verifieert de status altijd via een
# geauthenticeerde API-aanroep naar Mollie — nooit op basis van de POST-data alleen.

# ─── Validatie ───────────────────────────────────────────────────────────────
EMAIL_RE    = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TELEFOON_RE = re.compile(r"^(?=.*\d)[\d\s\+\-\(\)]{6,20}$")

def valideer_invoer(voornaam, achternaam, telefoon, email, aantal, max_per_bestelling=100):
    fouten = []
    if not voornaam or len(voornaam.strip()) < 2:
        fouten.append("Vul een geldige voornaam in (minimaal 2 tekens).")
    if len(voornaam) > 100:
        fouten.append("Voornaam mag maximaal 100 tekens zijn.")
    if not achternaam or len(achternaam.strip()) < 2:
        fouten.append("Vul een geldige achternaam in (minimaal 2 tekens).")
    if len(achternaam) > 100:
        fouten.append("Achternaam mag maximaal 100 tekens zijn.")
    if not EMAIL_RE.match(email):
        fouten.append("Vul een geldig e-mailadres in.")
    if not TELEFOON_RE.match(telefoon):
        fouten.append("Vul een geldig telefoonnummer in.")
    if aantal < 1:
        fouten.append("Bestel minimaal 1 eendje.")
    if aantal > max_per_bestelling:
        fouten.append(f"Maximaal {max_per_bestelling} eendjes per bestelling.")
    return fouten

# ─── Database ─────────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        # isolation_level=None = autocommit-modus.
        # BUG-FIX: Python's sqlite3 begint impliciet transacties voor DML-statements
        # (INSERT/UPDATE/DELETE) wanneer isolation_level != None.  Als zo'n impliciete
        # transactie NIET gecommit wordt (bv. door een exception met 'pass'), crasht
        # een latere expliciete 'BEGIN EXCLUSIVE' met "cannot start a transaction within
        # a transaction".  Met isolation_level=None beheert de applicatie alle transacties
        # zelf via expliciete BEGIN/COMMIT/ROLLBACK, waardoor dit conflict onmogelijk is.
        conn = sqlite3.connect(DATABASE, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db

@app.teardown_appcontext
def sluit_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bestellingen (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            voornaam        TEXT NOT NULL DEFAULT '',
            achternaam      TEXT NOT NULL,
            telefoon        TEXT NOT NULL,
            email           TEXT NOT NULL,
            aantal          INTEGER NOT NULL CHECK (aantal >= 1),
            bedrag          REAL NOT NULL,
            mollie_id       TEXT UNIQUE,
            status          TEXT NOT NULL DEFAULT 'aangemaakt'
                            CHECK (status IN
                              ('aangemaakt','betaald','mislukt','geannuleerd','verlopen')),
            lot_van         INTEGER,
            lot_tot         INTEGER,
            mail_verstuurd  INTEGER NOT NULL DEFAULT 0,
            pogingen        INTEGER NOT NULL DEFAULT 0,
            transactiekosten         INTEGER NOT NULL DEFAULT 0,
            transactiekosten_bedrag  REAL    NOT NULL DEFAULT 0,
            betaalwijze     TEXT NOT NULL DEFAULT 'ideal',
            aangemaakt_op   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            bijgewerkt_op   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS teller (
            id                  INTEGER PRIMARY KEY CHECK (id = 1),
            volgend_lot         INTEGER NOT NULL DEFAULT 1,
            max_eendjes         INTEGER NOT NULL DEFAULT 3000,
            max_per_bestelling  INTEGER NOT NULL DEFAULT 100,
            prijs_per_stuk      REAL NOT NULL DEFAULT 2.50,
            prijs_vijf_stuks    REAL NOT NULL DEFAULT 10.00,
            transactiekosten    REAL NOT NULL DEFAULT 0.32
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS webhook_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mollie_id   TEXT,
            status      TEXT,
            ip          TEXT,
            ontvangen   TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            tijdstip  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            gebruiker TEXT,
            actie     TEXT NOT NULL,
            details   TEXT,
            ip        TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS beheerders (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            gebruikersnaam    TEXT NOT NULL UNIQUE,
            wachtwoord_hash   TEXT NOT NULL,
            aangemaakt_op     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            laatste_inlog     TEXT
        )
    """)
    if not conn.execute("SELECT 1 FROM beheerders LIMIT 1").fetchone():
        if ADMIN_WACHTWOORD and len(ADMIN_WACHTWOORD) >= 12:
            conn.execute(
                "INSERT INTO beheerders (gebruikersnaam, wachtwoord_hash) VALUES (?, ?)",
                (ADMIN_GEBRUIKER, generate_password_hash(ADMIN_WACHTWOORD))
            )
        else:
            global _setup_token
            # Lees bestaand token uit bestand (andere workers hebben het al aangemaakt)
            if os.path.exists(_SETUP_TOKEN_BESTAND):
                with open(_SETUP_TOKEN_BESTAND) as _f:
                    _setup_token = _f.read().strip() or None
            if _setup_token is None:
                _setup_token = secrets.token_urlsafe(32)
                with open(_SETUP_TOKEN_BESTAND, "w") as _f:
                    _f.write(_setup_token)
            print(
                f"\n{'=' * 60}\n"
                f"⚠️  Geen beheerdersaccounts gevonden.\n"
                f"   Stel een initieel account in via:\n"
                f"   {BASE_URL}/setup?token={_setup_token}\n"
                f"{'=' * 60}\n",
                flush=True,
            )
    # Migraties eerst — zodat kolommen bestaan vóór de INSERT
    try:
        conn.execute("ALTER TABLE bestellingen ADD COLUMN transactiekosten INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    try:
        conn.execute("ALTER TABLE bestellingen ADD COLUMN transactiekosten_bedrag REAL NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    try:
        conn.execute(f"ALTER TABLE teller ADD COLUMN max_eendjes INTEGER NOT NULL DEFAULT {MAX_EENDJES}")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    try:
        conn.execute("ALTER TABLE teller ADD COLUMN max_per_bestelling INTEGER NOT NULL DEFAULT 100")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    try:
        conn.execute(f"ALTER TABLE teller ADD COLUMN prijs_per_stuk REAL NOT NULL DEFAULT {PRIJS_PER_STUK}")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    try:
        conn.execute(f"ALTER TABLE teller ADD COLUMN prijs_vijf_stuks REAL NOT NULL DEFAULT {PRIJS_VIJF_STUKS}")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    try:
        conn.execute(f"ALTER TABLE teller ADD COLUMN transactiekosten REAL NOT NULL DEFAULT {TRANSACTIEKOSTEN}")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    try:
        conn.execute("ALTER TABLE bestellingen ADD COLUMN betaalwijze TEXT NOT NULL DEFAULT 'ideal'")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    try:
        conn.execute("ALTER TABLE beheerders ADD COLUMN laatste_inlog TEXT")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    try:
        conn.execute("ALTER TABLE beheerders ADD COLUMN sessie_versie INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    try:
        conn.execute("ALTER TABLE beheerders ADD COLUMN mislukte_pogingen INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    try:
        conn.execute("ALTER TABLE beheerders ADD COLUMN geblokkeerd_tot TEXT")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    try:
        conn.execute("ALTER TABLE beheerders ADD COLUMN totp_geheim TEXT")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    try:
        conn.execute("ALTER TABLE beheerders ADD COLUMN totp_actief INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    try:
        conn.execute("ALTER TABLE teller ADD COLUMN onderhoudsmodus INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
    # Migratie: naam → voornaam + achternaam
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(bestellingen)").fetchall()]
        if 'naam' in cols and 'voornaam' not in cols:
            conn.execute("BEGIN")
            conn.execute("ALTER TABLE bestellingen RENAME TO _bestellingen_oud")
            conn.execute("""CREATE TABLE bestellingen (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                voornaam        TEXT NOT NULL DEFAULT '',
                achternaam      TEXT NOT NULL,
                email           TEXT NOT NULL,
                telefoon        TEXT NOT NULL DEFAULT '',
                aantal          INTEGER NOT NULL CHECK (aantal >= 1 AND aantal <= 100),
                bedrag          REAL NOT NULL,
                transactiekosten INTEGER NOT NULL DEFAULT 0,
                mollie_id       TEXT,
                status          TEXT NOT NULL DEFAULT 'aangemaakt'
                                CHECK (status IN
                                  ('aangemaakt','betaald','mislukt','geannuleerd','verlopen')),
                lot_van         INTEGER,
                lot_tot         INTEGER,
                mail_verstuurd  INTEGER NOT NULL DEFAULT 0,
                pogingen        INTEGER NOT NULL DEFAULT 0,
                betaalwijze     TEXT NOT NULL DEFAULT 'ideal',
                aangemaakt_op   DATETIME DEFAULT (datetime('now','localtime')),
                bijgewerkt_op   DATETIME DEFAULT (datetime('now','localtime'))
            )""")
            conn.execute("""INSERT INTO bestellingen
                (id, voornaam, achternaam, email, telefoon, aantal, bedrag, transactiekosten,
                 mollie_id, status, lot_van, lot_tot, mail_verstuurd, pogingen, betaalwijze,
                 aangemaakt_op, bijgewerkt_op)
                SELECT id,
                    TRIM(CASE WHEN INSTR(naam, ' ') > 0 THEN SUBSTR(naam, 1, INSTR(naam, ' ') - 1) ELSE '' END),
                    TRIM(CASE WHEN INSTR(naam, ' ') > 0 THEN SUBSTR(naam, INSTR(naam, ' ') + 1) ELSE naam END),
                    email, telefoon, aantal, bedrag, transactiekosten,
                    mollie_id, status, lot_van, lot_tot, mail_verstuurd, pogingen, betaalwijze,
                    aangemaakt_op, bijgewerkt_op
                FROM _bestellingen_oud""")
            conn.execute("DROP TABLE _bestellingen_oud")
            conn.execute("COMMIT")
            app.logger.info("Migratie: naam gesplitst in voornaam + achternaam")
    except Exception as e:
        try: conn.execute("ROLLBACK")
        except Exception: pass
        app.logger.warning(f"Migratie naam→voornaam/achternaam mislukt: {e}")
    # Seed-rij: alleen aanmaken als nog niet bestaat (alle kolommen zijn nu gegarandeerd aanwezig)
    conn.execute(
        "INSERT OR IGNORE INTO teller (id, volgend_lot, max_eendjes, max_per_bestelling, "
        "prijs_per_stuk, prijs_vijf_stuks, transactiekosten) VALUES (1, 1, ?, 100, ?, ?, ?)",
        (MAX_EENDJES, PRIJS_PER_STUK, PRIJS_VIJF_STUKS, TRANSACTIEKOSTEN)
    )
    # Migratie: verwijder hardcoded CHECK (aantal <= 100) uit bestellingen-tabel
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='bestellingen'"
    ).fetchone()
    if schema and "aantal <= 100" in (schema["sql"] or ""):
        conn.commit()  # sluit eventuele impliciete transactie vóór EXCLUSIVE BEGIN
        conn.execute("BEGIN")
        try:
            conn.execute("ALTER TABLE bestellingen RENAME TO _bestellingen_oud")
            conn.execute("""
                CREATE TABLE bestellingen (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    voornaam        TEXT NOT NULL DEFAULT '',
                    achternaam      TEXT NOT NULL,
                    telefoon        TEXT NOT NULL,
                    email           TEXT NOT NULL,
                    aantal          INTEGER NOT NULL CHECK (aantal >= 1),
                    bedrag          REAL NOT NULL,
                    mollie_id       TEXT UNIQUE,
                    status          TEXT NOT NULL DEFAULT 'aangemaakt'
                                    CHECK (status IN
                                      ('aangemaakt','betaald','mislukt','geannuleerd','verlopen')),
                    lot_van         INTEGER,
                    lot_tot         INTEGER,
                    mail_verstuurd  INTEGER NOT NULL DEFAULT 0,
                    pogingen        INTEGER NOT NULL DEFAULT 0,
                    transactiekosten         INTEGER NOT NULL DEFAULT 0,
                    transactiekosten_bedrag  REAL    NOT NULL DEFAULT 0,
                    betaalwijze     TEXT NOT NULL DEFAULT 'ideal',
                    aangemaakt_op   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    bijgewerkt_op   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                )
            """)
            oud_cols = {r[1] for r in conn.execute("PRAGMA table_info(_bestellingen_oud)").fetchall()}
            insert_cols = [c for c in [
                "id", "voornaam", "achternaam", "telefoon", "email", "aantal", "bedrag",
                "mollie_id", "status", "lot_van", "lot_tot", "mail_verstuurd", "pogingen",
                "transactiekosten", "transactiekosten_bedrag", "betaalwijze",
                "aangemaakt_op", "bijgewerkt_op"
            ] if c in oud_cols]
            cols_str = ", ".join(insert_cols)
            conn.execute(f"INSERT INTO bestellingen ({cols_str}) SELECT {cols_str} FROM _bestellingen_oud")
            conn.execute("DROP TABLE _bestellingen_oud")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    conn.commit()
    conn.close()

def get_max_eendjes():
    """Leest het totale maximum aantal beschikbare eendjes uit de database."""
    row = get_db().execute("SELECT max_eendjes FROM teller WHERE id = 1").fetchone()
    return row["max_eendjes"] if row else MAX_EENDJES


def get_max_per_bestelling():
    """Leest het maximaal toegestane aantal eendjes per bestelling uit de database."""
    row = get_db().execute("SELECT max_per_bestelling FROM teller WHERE id = 1").fetchone()
    return row["max_per_bestelling"] if row else 100


def get_prijs_per_stuk():
    row = get_db().execute("SELECT prijs_per_stuk FROM teller WHERE id = 1").fetchone()
    return row["prijs_per_stuk"] if row else PRIJS_PER_STUK


def get_prijs_vijf_stuks():
    row = get_db().execute("SELECT prijs_vijf_stuks FROM teller WHERE id = 1").fetchone()
    return row["prijs_vijf_stuks"] if row else PRIJS_VIJF_STUKS


def get_transactiekosten():
    row = get_db().execute("SELECT transactiekosten FROM teller WHERE id = 1").fetchone()
    return row["transactiekosten"] if row else TRANSACTIEKOSTEN



def get_onderhoudsmodus():
    row = get_db().execute("SELECT onderhoudsmodus FROM teller WHERE id = 1").fetchone()
    return bool(row["onderhoudsmodus"]) if row else False


# ─── Business logica ──────────────────────────────────────────────────────────
def bereken_bedrag(aantal, prijs_per_stuk=None, prijs_vijf_stuks=None):
    p_stuk = prijs_per_stuk   if prijs_per_stuk   is not None else PRIJS_PER_STUK
    p_vijf = prijs_vijf_stuks if prijs_vijf_stuks is not None else PRIJS_VIJF_STUKS
    vijftallen = aantal // 5
    rest       = aantal % 5
    return round(vijftallen * p_vijf + rest * p_stuk, 2)


def wijs_lotnummers_toe(db, bestelling_id, aantal):
    """Atomische lotnummer-toewijzing via een EXCLUSIVE transactie.

    Raises ValueError als de race op te weinig beschikbare lotnummers uitkomt.
    Idempotent: als de bestelling al 'betaald' is, worden de bestaande
    lotnummers teruggegeven zonder opnieuw toe te wijzen.
    """
    db.execute("BEGIN EXCLUSIVE")
    try:
        # Idempotentie: voorkom dubbele toewijzing bij gelijktijdige webhook + fallback
        bestaand = db.execute(
            "SELECT status, lot_van, lot_tot FROM bestellingen WHERE id=?",
            (bestelling_id,)
        ).fetchone()
        if bestaand and bestaand["status"] == "betaald":
            db.execute("ROLLBACK")
            app.logger.info(f"Lotnummers al toegewezen (idempotent): bestelling {bestelling_id}, loten={bestaand['lot_van']}–{bestaand['lot_tot']}")
            return bestaand["lot_van"], bestaand["lot_tot"]

        teller     = db.execute("SELECT volgend_lot, max_eendjes FROM teller WHERE id = 1").fetchone()
        start      = teller["volgend_lot"]
        max_eendjes = teller["max_eendjes"]
        einde      = start + aantal - 1

        # BUG-FIX: controleer oversell vóór de UPDATE; zonder deze check konden
        # lotnummers boven max_eendjes worden uitgedeeld.
        if einde > max_eendjes:
            db.execute("ROLLBACK")
            raise ValueError(
                f"Onvoldoende lotnummers: gevraagd t/m {einde}, max={max_eendjes}"
            )

        db.execute("UPDATE teller SET volgend_lot = ? WHERE id = 1", (einde + 1,))
        db.execute(
            """UPDATE bestellingen
               SET lot_van=?, lot_tot=?, status='betaald',
                   bijgewerkt_op=datetime('now','localtime')
               WHERE id=?""",
            (start, einde, bestelling_id),
        )
        db.commit()
        app.logger.info(f"Lotnummers toegewezen: bestelling {bestelling_id}, loten={start}–{einde}")
    except Exception:
        # Zorg dat de transactie altijd gesloten wordt bij een onverwachte fout
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return start, einde


def stuur_bevestigingsmail(voornaam, achternaam, email, aantal, lot_van, lot_tot, bedrag, transactiekosten=False, tk_bedrag=0.0):
    """Geeft True bij succes, False bij fout — gooit nooit een exception."""
    naam = html.escape(f"{voornaam} {achternaam}".strip())  # voorkom XSS via naam in HTML e-mail
    if lot_van == lot_tot:
        lotnr_tekst = f"lotnummer <strong>#{lot_van}</strong>"
    elif lot_tot - lot_van < 5:
        nummers = " &middot; ".join(f"#{n}" for n in range(lot_van, lot_tot + 1))
        lotnr_tekst = f"lotnummers <strong>{nummers}</strong>"
    else:
        lotnr_tekst = f"lotnummers <strong>#{lot_van} t/m #{lot_tot}</strong>"

    mail_html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;color:#333;padding-top:24px;">
      <div style="background:#0077B6;padding:32px 24px;border-radius:12px 12px 0 0;text-align:center;">
        <img src="{BASE_URL}/static/img/eend.png" alt="Badeend" width="80" height="80" style="width:80px;height:80px;display:block;margin:0 auto 12px;">
        <h1 style="margin:0;color:#FFD700;font-size:28px;letter-spacing:1px;text-shadow:2px 2px 0 rgba(0,0,0,0.25);">Badeendjesrace!</h1>
        <p style="margin:6px 0 0;color:#90E0EF;font-size:15px;">{AFZENDER_NAAM}</p>
        <p style="margin:10px 0 0;display:inline-block;padding:4px 16px;background:rgba(255,255,255,0.15);border:1px solid rgba(255,255,255,0.3);border-radius:99px;color:#ffffff;font-size:13px;letter-spacing:0.5px;">📅 Zaterdag 30 mei 2026</p>
      </div>
      <div style="background:#fffdf0;padding:32px;border:1px solid #eee;border-radius:0 0 12px 12px;">
        <p>Beste <strong>{naam}</strong>,</p>
        <p>Bedankt voor je bestelling! Je betaling van <strong>&euro;&nbsp;{bedrag:.2f}</strong> is ontvangen{f' (incl. &euro;&nbsp;{tk_bedrag:.2f} iDEAL-transactiekosten)'.replace(".", ",") if transactiekosten else ''}.</p>
        <div style="background:#fff;border:2px solid #FFD700;border-radius:10px;padding:24px;margin:24px 0;text-align:center;">
          <p style="font-size:17px;margin:0 0 8px;">
            Je hebt <strong>{aantal}&nbsp;eend{'je' if aantal==1 else 'jes'}</strong> en ontvangt:
          </p>
          <p style="font-size:20px;margin:0;color:#0077B6;">{lotnr_tekst}</p>
        </div>
        <p>Op de dag van de race worden jouw eendjes te water gelaten. Bewaar je nummers goed!</p>
        <div style="background:#EFF8FF;border:1px solid #90CAF9;border-radius:10px;padding:16px 20px;margin:24px 0;">
          <p style="margin:0 0 6px;font-weight:bold;color:#0077B6;">&#x2139;&#xFE0F; Praktische informatie</p>
          <p style="margin:0;font-size:.95rem;color:#01579B;line-height:1.8;">
            &#x1F4C5; <strong>Datum:</strong> Zaterdag 30 mei 2026<br>
            &#x1F559; <strong>Tijd:</strong> Rond 19:30 uur<br>
            &#x1F4CD; <strong>Locatie:</strong> Het Apeldoorns Kanaal, bij de Manenbergerbrug / de Loswal in Wapenveld
          </p>
        </div>
        <div style="background:#E8F5E9;border:1px solid #A5D6A7;border-radius:10px;padding:16px 20px;margin:24px 0;">
          <p style="margin:0 0 6px;font-weight:bold;color:#2E7D32;">&#127758; Waarvoor gaat dit geld?</p>
          <p style="margin:0;font-size:.9rem;color:#1B5E20;line-height:1.6;">
            De opbrengst van de Badeendjesrace gaat naar het diaconale project
            <strong>'Ik geloof, ik deel'</strong> van de HGJB-commissie van de
            Hervormde Gemeente Wapenveld. Met dit project ondersteunen we
            christenen in Albani&euml; en maken we mensen bewust van wat het
            betekent om te delen vanuit geloof.<br>
            <a href="https://hervormdwapenveld.nl/werkgroepen/hgjb-diaconaal-project"
               style="color:#2E7D32;font-weight:bold;">Lees meer over het project &rarr;</a>
          </p>
        </div>
        <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
        <p style="font-size:.85rem;color:#999;">
          Dit is een automatisch bericht &mdash; niet beantwoorden.
        </p>
        <p>Met vriendelijke groet,<br><strong>HGJB-commissie Hervormde Gemeente Wapenveld</strong><br>
        <span style="font-size:.85rem;color:#999;">namens Diaconie Hervormde gemeente te Wapenveld</span></p>
      </div>
    </body></html>
    """
    try:
        resend.api_key = RESEND_API_KEY
        resend.Emails.send({
            "from":    f"{AFZENDER_NAAM} <{RESEND_FROM}>",
            "to":      [email],
            "subject": "Jouw lotnummers – Badeendjesrace!",
            "html":    mail_html,
        })
        app.logger.info(f"Mail verstuurd → {saniteer_log(email)}")
        return True
    except Exception as e:
        app.logger.error(f"Resend-fout: {e}")
    return False

# ─── Security helpers ─────────────────────────────────────────────────────────
def login_vereist(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_ingelogd"):
            return redirect(url_for("admin_login"))
        # Valideer sessie-versie: ongeldig na wachtwoordwijziging
        gebruiker = session.get("admin_gebruikersnaam")
        if gebruiker:
            db = get_db()
            rij = db.execute(
                "SELECT sessie_versie FROM beheerders WHERE gebruikersnaam = ?",
                (gebruiker,)
            ).fetchone()
            if not rij or rij["sessie_versie"] != session.get("admin_sessie_versie", -1):
                session.clear()
                return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


def saniteer_log(tekst):
    """Verwijder control characters uit gebruikersinvoer om log-injectie te voorkomen."""
    return "".join(c if ord(c) >= 32 else " " for c in str(tekst))


def get_client_ip():
    """Geef het echte client-IP terug.
    Als de app achter Cloudflare draait stuurt CF het originele IP mee via
    CF-Connecting-IP. Anders valt terug op request.remote_addr (dat ProxyFix
    al heeft gecorrigeerd voor de Railway load balancer).
    """
    return request.headers.get("CF-Connecting-IP") or request.remote_addr


def genereer_qr_base64(uri: str) -> str:
    """Genereer een QR-code voor de gegeven URI en geef een base64 PNG-string terug."""
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def schrijf_audit_log(actie, details=None, gebruiker=None, ip=None):
    """Schrijf een regel naar de audit_log tabel. Fouten worden gelogd maar niet gegooid."""
    try:
        db = get_db()
        db.execute(
            "INSERT INTO audit_log (actie, details, gebruiker, ip) VALUES (?, ?, ?, ?)",
            (actie, details, gebruiker, ip)
        )
        db.commit()
    except Exception as e:
        app.logger.error(f"audit_log-schrijffout: {e}")


@app.before_request
def genereer_csp_nonce():
    g.csp_nonce = secrets.token_hex(16)


@app.before_request
def controleer_onderhoudsmodus():
    """Toon onderhoudspagina (503) voor alle publieke routes als onderhoudsmodus aan is."""
    # Admin-routes, statische bestanden en de Mollie-webhook blijven altijd bereikbaar.
    if (request.path.startswith("/admin")
            or request.path.startswith("/static")
            or request.path.startswith("/setup")
            or request.path in ("/webhook", "/health", "/api/beschikbaar")):
        return
    try:
        if get_onderhoudsmodus():
            return render_template("onderhoud.html"), 503
    except Exception:
        pass  # Bij DB-fout gewoon doorgaan


@app.context_processor
def inject_base_url():
    return {"base_url": BASE_URL}


@app.after_request
def security_headers(response):
    nonce = getattr(g, "csp_nonce", "")
    response.headers["Server"]                 = ""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"]        = "DENY"
    response.headers["X-XSS-Protection"]       = "1; mode=block"
    response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"]     = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "img-src 'self' data:; "
        "base-uri 'self'; "
        "form-action 'self' https://www.mollie.com https://pay.ideal.nl; "
        "frame-ancestors 'none';"
    )
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# ─── security.txt (RFC 9116) ──────────────────────────────────────────────────
SECURITY_CONTACT = os.environ.get("SECURITY_CONTACT", "")

@app.route("/.well-known/security.txt")
def security_txt():
    contact = SECURITY_CONTACT or f"mailto:{os.environ.get('RESEND_FROM', 'admin@example.com')}"
    expires = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    inhoud = (
        f"Contact: {contact}\n"
        f"Expires: {expires}\n"
        f"Preferred-Languages: nl, en\n"
        f"Canonical: {BASE_URL}/.well-known/security.txt\n"
    )
    return Response(inhoud, mimetype="text/plain")


# ─── robots.txt + sitemap.xml ─────────────────────────────────────────────────
@app.route("/robots.txt")
def robots_txt():
    inhoud = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /privacy\n"
        "Allow: /voorwaarden\n"
        "Disallow: /admin\n"
        "Disallow: /bestellen\n"
        "Disallow: /webhook\n"
        "Disallow: /api/\n"
        "Disallow: /betaald/\n"
        f"Sitemap: {BASE_URL}/sitemap.xml\n"
    )
    return Response(inhoud, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    inhoud = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url>\n"
        f"    <loc>{BASE_URL}/</loc>\n"
        "    <changefreq>daily</changefreq>\n"
        "    <priority>1.0</priority>\n"
        "  </url>\n"
        "  <url>\n"
        f"    <loc>{BASE_URL}/privacy</loc>\n"
        "    <changefreq>monthly</changefreq>\n"
        "    <priority>0.3</priority>\n"
        "  </url>\n"
        "  <url>\n"
        f"    <loc>{BASE_URL}/voorwaarden</loc>\n"
        "    <changefreq>monthly</changefreq>\n"
        "    <priority>0.3</priority>\n"
        "  </url>\n"
        "</urlset>\n"
    )
    return Response(inhoud, mimetype="application/xml")


# ─── Foutpagina's ─────────────────────────────────────────────────────────────
@app.errorhandler(400)
def fout_400(e):
    return render_template("fout.html", code=400,
        titel="Ongeldige aanvraag",
        bericht="Er ontbreekt informatie of de invoer is ongeldig."), 400

@app.errorhandler(403)
def fout_403(e):
    return render_template("fout.html", code=403,
        titel="Geen toegang",
        bericht="Je hebt geen toegang tot deze pagina."), 403

@app.errorhandler(404)
def fout_404(e):
    return render_template("fout.html", code=404,
        titel="Pagina niet gevonden",
        bericht="Deze pagina bestaat niet."), 404

@app.errorhandler(429)
def fout_429(e):
    return render_template("fout.html", code=429,
        titel="Te veel verzoeken",
        bericht="Je hebt te veel verzoeken gedaan. Probeer het over een minuut opnieuw."), 429

@app.errorhandler(500)
def fout_500(e):
    app.logger.error(f"Interne fout: {e}", exc_info=True)
    return render_template("fout.html", code=500,
        titel="Technische fout",
        bericht="Er is iets misgegaan. Probeer het later opnieuw."), 500

# ─── Publieke routes ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    try:
        db      = get_db()
        betaald = db.execute(
            "SELECT COALESCE(SUM(aantal),0) AS n FROM bestellingen WHERE status='betaald'"
        ).fetchone()["n"]
        max_eendjes = get_max_eendjes()
        sponsors_map = os.path.join(app.static_folder, "img", "sponsors")
        sponsor_bestanden = sorted(
            f for f in os.listdir(sponsors_map)
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".svg", ".webp"))
        ) if os.path.isdir(sponsors_map) else []
        return render_template("index.html",
                               verkocht=betaald,
                               beschikbaar=max(0, max_eendjes - betaald),
                               max_eendjes=max_eendjes,
                               max_per_bestelling=get_max_per_bestelling(),
                               transactiekosten=get_transactiekosten(),
                               prijs_per_stuk=get_prijs_per_stuk(),
                               prijs_vijf_stuks=get_prijs_vijf_stuks(),
                               sponsor_bestanden=sponsor_bestanden)
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout index: {e}")
        abort(500)


@app.route("/api/prijs")
@limiter.limit("60 per minute")
def api_prijs():
    try:
        aantal = int(request.args.get("aantal", 0))
        max_per_bestelling = get_max_per_bestelling()
        if not (1 <= aantal <= max_per_bestelling):
            return jsonify({"fout": f"Aantal moet tussen 1 en {max_per_bestelling} liggen."}), 400
        incl_tk = request.args.get("transactiekosten", "0") == "1"
        bedrag  = bereken_bedrag(aantal, get_prijs_per_stuk(), get_prijs_vijf_stuks()) + (get_transactiekosten() if incl_tk else 0)
        return jsonify({"bedrag": round(bedrag, 2),
                        "bedrag_tekst": f"€ {bedrag:.2f}".replace(".", ",")})
    except (ValueError, TypeError):
        return jsonify({"fout": "Ongeldig aantal."}), 400


@app.route("/api/beschikbaar")
@limiter.limit("60 per minute")
def api_beschikbaar():
    """Geeft actueel aantal verkochte en beschikbare eendjes terug."""
    try:
        db          = get_db()
        betaald     = db.execute(
            "SELECT COALESCE(SUM(aantal),0) AS n FROM bestellingen WHERE status='betaald'"
        ).fetchone()["n"]
        max_eendjes        = get_max_eendjes()
        max_per_bestelling = get_max_per_bestelling()
        beschikbaar        = max(0, max_eendjes - betaald)
        return jsonify({
            "verkocht":           betaald,
            "beschikbaar":        beschikbaar,
            "max_eendjes":        max_eendjes,
            "max_per_bestelling": max_per_bestelling,
        })
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout api_beschikbaar: {e}")
        return jsonify({"fout": "Databasefout"}), 500


@app.route("/health")
def gezondheid():
    """Gezondheidscheck voor uptime monitoring. Controleert DB en Mollie-verbinding."""
    db_status     = "ok"
    mollie_status = "ok"

    try:
        get_db().execute("SELECT 1")
    except Exception:
        db_status = "fout"

    if MOLLIE_API_KEY:
        try:
            maak_mollie_client().payments.list(limit=1)
        except Exception:
            mollie_status = "fout"
    else:
        mollie_status = "niet_geconfigureerd"

    alles_ok = db_status == "ok" and mollie_status in ("ok", "niet_geconfigureerd")
    return jsonify({
        "status": "ok" if alles_ok else "fout",
        "db":     db_status,
        "mollie": mollie_status,
    }), 200 if alles_ok else 503


@app.route("/bestellen", methods=["POST"])
@limiter.limit("10 per minute")
def bestellen():
    voornaam         = request.form.get("voornaam", "").strip()
    achternaam       = request.form.get("achternaam", "").strip()
    telefoon         = request.form.get("telefoon", "").strip()
    email            = request.form.get("email", "").strip().lower()
    incl_tk          = request.form.get("transactiekosten") == "1"
    vorig            = {"voornaam": voornaam, "achternaam": achternaam, "telefoon": telefoon, "email": email, "transactiekosten": incl_tk}

    try:
        aantal = int(request.form.get("aantal", 0))
        vorig["aantal"] = aantal
    except (ValueError, TypeError):
        return render_template("fout.html", code=400,
            titel="Ongeldig aantal",
            bericht="Het opgegeven aantal is niet geldig."), 400

    app.logger.info(
        f"Bestelling ontvangen: {saniteer_log(voornaam)} {saniteer_log(achternaam)}, "
        f"aantal={aantal}, email={saniteer_log(email)}, incl_tk={incl_tk}"
    )

    # Validatie
    max_per_bestelling = get_max_per_bestelling()
    max_eendjes        = get_max_eendjes()
    fouten = valideer_invoer(voornaam, achternaam, telefoon, email, aantal, max_per_bestelling)
    if fouten:
        db          = get_db()
        betaald     = db.execute("SELECT COALESCE(SUM(aantal),0) AS n FROM bestellingen WHERE status='betaald'").fetchone()["n"]
        beschikbaar = max(0, max_eendjes - betaald)
        return render_template("index.html",
                               verkocht=betaald,
                               beschikbaar=beschikbaar,
                               max_eendjes=max_eendjes,
                               max_per_bestelling=max_per_bestelling,
                               transactiekosten=get_transactiekosten(),
                               prijs_per_stuk=get_prijs_per_stuk(),
                               prijs_vijf_stuks=get_prijs_vijf_stuks(),
                               fouten=fouten,
                               vorig=vorig), 422

    bedrag = round(bereken_bedrag(aantal, get_prijs_per_stuk(), get_prijs_vijf_stuks()) + (get_transactiekosten() if incl_tk else 0), 2)

    # Controleer beschikbaarheid en sla op — atomisch
    try:
        db = get_db()
        db.execute("BEGIN EXCLUSIVE")
        betaald = db.execute(
            "SELECT COALESCE(SUM(aantal),0) AS n FROM bestellingen WHERE status='betaald'"
        ).fetchone()["n"]

        if betaald + aantal > max_eendjes:
            db.execute("ROLLBACK")
            beschikbaar = max(0, max_eendjes - betaald)
            return render_template("index.html",
                                   verkocht=betaald,
                                   beschikbaar=beschikbaar,
                                   max_eendjes=max_eendjes,
                                   max_per_bestelling=max_per_bestelling,
                                   transactiekosten=get_transactiekosten(),
                                   prijs_per_stuk=get_prijs_per_stuk(),
                                   prijs_vijf_stuks=get_prijs_vijf_stuks(),
                                   fouten=[f"Er zijn nog maar {beschikbaar} eendjes beschikbaar."],
                                   vorig=vorig), 409

        tk_bedrag = get_transactiekosten() if incl_tk else 0
        cursor = db.execute(
            "INSERT INTO bestellingen (voornaam, achternaam, telefoon, email, aantal, bedrag, transactiekosten, transactiekosten_bedrag, betaalwijze) VALUES (?,?,?,?,?,?,?,?,?)",
            (voornaam, achternaam, telefoon, email, aantal, bedrag, 1 if incl_tk else 0, tk_bedrag, "ideal"),
        )
        bestelling_id = cursor.lastrowid
        db.commit()

    except sqlite3.Error as e:
        # BUG-FIX: zonder expliciete ROLLBACK bleef een openstaande EXCLUSIVE
        # transactie actief totdat de verbinding bij teardown gesloten werd.
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        app.logger.error(f"DB-fout bij aanmaken bestelling: {e}")
        abort(500)

    # Mollie betaling aanmaken
    if not MOLLIE_API_KEY:
        app.logger.error("MOLLIE_API_KEY niet ingesteld!")
        abort(500)

    try:
        mollie   = maak_mollie_client()
        betaling = mollie.payments.create({
            "amount":      {"currency": "EUR", "value": f"{bedrag:.2f}"},
            "description": f"Badeendjesrace – {aantal} eend{'je' if aantal==1 else 'jes'} ({f'{voornaam} {achternaam}'.strip()})",
            "redirectUrl": f"{BASE_URL}/betaald/r/{bestelling_id}",
            "webhookUrl":  f"{BASE_URL}/webhook",
            "metadata":    {"bestelling_id": str(bestelling_id)},
        })
        db.execute(
            "UPDATE bestellingen SET mollie_id=? WHERE id=?",
            (betaling.id, bestelling_id),
        )
        db.commit()
        app.logger.info(
            f"Betaling aangemaakt: id={bestelling_id}, mollie={betaling.id}, €{bedrag}"
        )
        checkout_url = betaling.checkout_url
        app.logger.info(f"Redirect naar Mollie checkout: id={bestelling_id}")
        resp = Response(status=302)
        resp.headers["Location"] = checkout_url
        return resp

    except MollieError as e:
        app.logger.error(f"Mollie API-fout: {e}")
        try:
            db.execute("UPDATE bestellingen SET status='mislukt' WHERE id=?", (bestelling_id,))
            db.commit()
        except sqlite3.Error:
            pass
        return render_template("fout.html", code=503,
            titel="Betaalsysteem niet beschikbaar",
            bericht="Het betaalsysteem is tijdelijk niet bereikbaar. Probeer het over een paar minuten opnieuw."), 503


@app.route("/webhook", methods=["POST"])
@limiter.limit("60 per minute")
@csrf.exempt
def webhook():
    client_ip = get_client_ip()
    mollie_id = request.form.get("id", "").strip()
    if not mollie_id or not mollie_id.startswith("tr_"):
        app.logger.warning(f"Webhook: ongeldig mollie_id '{saniteer_log(mollie_id)}'")
        return "", 400

    # Log de aanroep
    try:
        db = get_db()
        db.execute("INSERT INTO webhook_log (mollie_id, ip) VALUES (?,?)", (mollie_id, client_ip))
        db.commit()
    except sqlite3.Error as log_err:
        app.logger.warning(f"Webhook: auditlog-schrijffout voor {saniteer_log(mollie_id)}: {log_err}")

    # Haal betaalstatus op bij Mollie (we vertrouwen nooit alleen op de POST-data)
    try:
        mollie   = maak_mollie_client()
        betaling = mollie.payments.get(mollie_id)
    except MollieError as e:
        app.logger.error(f"Webhook: Mollie API-fout {mollie_id}: {e}")
        return "", 500  # Mollie herprobeert bij 5xx

    try:
        db  = get_db()
        rij = db.execute(
            "SELECT * FROM bestellingen WHERE mollie_id=?", (mollie_id,)
        ).fetchone()

        if not rij:
            app.logger.warning(f"Webhook: geen bestelling voor mollie_id={mollie_id}")
            return "", 200

        bestelling_id = rij["id"]

        if betaling.is_paid():
            if rij["status"] == "betaald":
                app.logger.info(f"Webhook: bestelling {bestelling_id} was al betaald")
                return "", 200

            # BUG-FIX: vang ValueError uit wijs_lotnummers_toe() op (oversell of
            # onverwacht DB-probleem) en markeer de bestelling als mislukt zodat
            # de beheerder het kan zien in het admin-paneel.
            try:
                lot_van, lot_tot = wijs_lotnummers_toe(db, bestelling_id, rij["aantal"])
            except ValueError as e:
                app.logger.error(
                    f"Oversell geblokkeerd voor bestelling {bestelling_id}: {e}"
                )
                db.execute(
                    "UPDATE bestellingen SET status='mislukt', "
                    "bijgewerkt_op=datetime('now','localtime') WHERE id=?",
                    (bestelling_id,),
                )
                db.commit()
                return "", 200

            app.logger.info(
                f"Betaald: id={bestelling_id}, loten={lot_van}–{lot_tot}, €{rij['bedrag']}"
            )
            mail_ok = stuur_bevestigingsmail(
                rij["voornaam"], rij["achternaam"], rij["email"], rij["aantal"],
                lot_van, lot_tot, rij["bedrag"], bool(rij["transactiekosten"]),
                tk_bedrag=rij["transactiekosten_bedrag"],
            )
            db.execute(
                "UPDATE bestellingen SET mail_verstuurd=?, pogingen=pogingen+1 WHERE id=?",
                (1 if mail_ok else 0, bestelling_id),
            )
            db.commit()
            if not mail_ok:
                app.logger.error(
                    f"⚠️  Mail NIET verstuurd voor bestelling {bestelling_id} — "
                    "gebruik /admin om opnieuw te verzenden."
                )

        elif betaling.is_pending() or betaling.is_open():
            # Nog niet afgerond — niets doen, Mollie stuurt later opnieuw
            app.logger.info(f"Betaling nog open/pending: id={bestelling_id}, status={saniteer_log(betaling.status)}")

        else:
            # Alles wat niet paid/pending/open is = afgebroken (failed, canceled, expired)
            # BUG-FIX: is_failed(), is_canceled(), is_expired() bestaan NIET in mollie-api-python v3.
            # De juiste aanpak is betaling.status direct te lezen.
            MOLLIE_NAAR_DB = {
                "failed":   "mislukt",
                "canceled": "geannuleerd",
                "expired":  "verlopen",
            }
            nieuwe_status = MOLLIE_NAAR_DB.get(betaling.status, "mislukt")
            db.execute(
                "UPDATE bestellingen SET status=?, bijgewerkt_op=datetime('now','localtime') WHERE id=?",
                (nieuwe_status, bestelling_id),
            )
            db.commit()
            app.logger.info(f"Betaling {nieuwe_status} (Mollie: {saniteer_log(betaling.status)}): id={bestelling_id}")

    except sqlite3.Error as e:
        app.logger.error(f"DB-fout in webhook: {e}")
        return "", 500

    return "", 200


@app.route("/betaald/r/<int:bestelling_id>")
def betaald_redirect(bestelling_id):
    """
    Tussenlandingspagina voor Mollie-redirectUrl: stuurt door naar /betaald/<mollie_id>
    zodat de publieke URL niet-raadbaar is.
    """
    try:
        db  = get_db()
        rij = db.execute("SELECT mollie_id FROM bestellingen WHERE id=?", (bestelling_id,)).fetchone()
    except sqlite3.Error:
        abort(500)
    if not rij or not rij["mollie_id"]:
        return redirect(url_for("index"))
    return redirect(url_for("betaald", mollie_id=rij["mollie_id"]))


@app.route("/betaald/<mollie_id>")
@limiter.limit("30 per minute")
def betaald(mollie_id):
    """
    Landingspagina na terugkeer van Mollie.
    Webhook kan iets later komen dan deze redirect — dus fallback-check.
    """
    if not mollie_id.startswith("tr_") or len(mollie_id) > 64:
        abort(404)
    try:
        db  = get_db()
        rij = db.execute("SELECT * FROM bestellingen WHERE mollie_id=?", (mollie_id,)).fetchone()
    except sqlite3.Error:
        abort(500)

    if not rij:
        return redirect(url_for("index"))

    bestelling_id = rij["id"]

    # Fallback: als webhook nog niet binnengekomen is, check zelf bij Mollie
    if rij["status"] == "aangemaakt" and rij["mollie_id"] and MOLLIE_API_KEY:
        app.logger.info(f"Fallback statuscheck gestart: bestelling {bestelling_id}, mollie={rij['mollie_id']}")
        try:
            mollie   = maak_mollie_client()
            betaling = mollie.payments.get(rij["mollie_id"])
            if betaling.is_paid():
                lot_van, lot_tot = wijs_lotnummers_toe(db, bestelling_id, rij["aantal"])
                mail_ok = stuur_bevestigingsmail(
                    rij["voornaam"], rij["achternaam"], rij["email"], rij["aantal"],
                    lot_van, lot_tot, rij["bedrag"], bool(rij["transactiekosten"]),
                    tk_bedrag=rij["transactiekosten_bedrag"],
                )
                db.execute(
                    "UPDATE bestellingen SET mail_verstuurd=?, pogingen=pogingen+1 WHERE id=?",
                    (1 if mail_ok else 0, bestelling_id),
                )
                db.commit()
                app.logger.info(f"Fallback succesvol: bestelling {bestelling_id}, loten={lot_van}–{lot_tot}")
                rij = db.execute("SELECT * FROM bestellingen WHERE id=?", (bestelling_id,)).fetchone()
        except ValueError as e:
            # wijs_lotnummers_toe deed zelf al ROLLBACK; status blijft 'aangemaakt'
            # zodat de webhook dit later correct afhandelt als 'mislukt'.
            app.logger.warning(f"Fallback: oversell of idempotente toewijzing geblokkeerd: {e}")
        except Exception as e:
            # wijs_lotnummers_toe doet intern al ROLLBACK bij onverwachte fouten,
            # dus hier hoeven we alleen te loggen.
            app.logger.warning(f"Fallback statuscheck mislukt: {e}")

    status_map = {
        "betaald":     ("succes",  "✅ Betaling ontvangen!"),
        "mislukt":     ("fout",    "❌ Betaling mislukt"),
        "geannuleerd": ("waarsch", "↩️ Betaling geannuleerd"),
        "verlopen":    ("waarsch", "⏰ Betaling verlopen"),
        "aangemaakt":  ("wacht",   "⏳ Betaling wordt verwerkt…"),
    }
    status_klasse, status_label = status_map.get(rij["status"], ("wacht", "⏳ Status onbekend"))
    return render_template("betaald.html",
                           bestelling=rij,
                           status_klasse=status_klasse,
                           status_label=status_label)

# ─── Admin routes ─────────────────────────────────────────────────────────────
def _voltooi_login(gebruiker: str, sessie_versie):
    """Schrijf sessievariabelen en audit-log na succesvol inloggen (na wachtwoord én eventuele 2FA)."""
    db = get_db()
    db.execute(
        "UPDATE beheerders SET laatste_inlog = datetime('now','localtime') WHERE gebruikersnaam = ?",
        (gebruiker,)
    )
    db.commit()
    session.clear()
    session["admin_ingelogd"]       = True
    session["admin_gebruikersnaam"] = gebruiker
    session["admin_sessie_versie"]  = sessie_versie if sessie_versie is not None else 0
    session.permanent = True
    app.logger.info(f"Admin ingelogd: {saniteer_log(gebruiker)} vanaf {get_client_ip()}")
    schrijf_audit_log("login_succes", gebruiker=gebruiker, ip=get_client_ip())


@app.route("/admin/login/totp", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def admin_login_totp():
    """Tweede stap van het inlogproces: TOTP-code invoeren."""
    gebruiker = session.get("totp_pending_gebruiker")
    if not gebruiker:
        return redirect(url_for("admin_login"))
    fout = None
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        db = get_db()
        rij = db.execute(
            "SELECT totp_geheim, totp_actief, sessie_versie FROM beheerders WHERE gebruikersnaam = ?",
            (gebruiker,)
        ).fetchone()
        if rij and rij["totp_actief"] and rij["totp_geheim"]:
            totp = pyotp.TOTP(rij["totp_geheim"])
            if totp.verify(code, valid_window=1):
                versie = session.get("totp_pending_versie", rij["sessie_versie"])
                _voltooi_login(gebruiker, versie)
                return redirect(url_for("admin"))
        schrijf_audit_log("login_mislukt_totp", gebruiker=gebruiker, ip=get_client_ip())
        fout = "Ongeldige code. Controleer de tijd op je telefoon en probeer opnieuw."
    return render_template("admin_login_totp.html", fout=fout)


@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def admin_login():
    fout = None
    if request.method == "POST":
        gebruiker  = request.form.get("gebruiker", "")
        wachtwoord = request.form.get("wachtwoord", "")
        db  = get_db()
        rij = db.execute(
            "SELECT wachtwoord_hash, sessie_versie, mislukte_pogingen, geblokkeerd_tot, totp_actief, totp_geheim FROM beheerders WHERE gebruikersnaam = ?",
            (gebruiker,)
        ).fetchone()

        # Controleer account-blokkering
        if rij and rij["geblokkeerd_tot"]:
            from datetime import datetime as _dt
            try:
                geblokkeerd_tot_dt = _dt.fromisoformat(rij["geblokkeerd_tot"])
                if _dt.now() < geblokkeerd_tot_dt:
                    resterende = int((geblokkeerd_tot_dt - _dt.now()).total_seconds())
                    fout = f"Account tijdelijk geblokkeerd. Probeer over {resterende // 60 + 1} minuten opnieuw."
                    app.logger.warning(f"Login geblokkeerd voor '{saniteer_log(gebruiker)}' vanaf {get_client_ip()}")
                    return render_template("admin_login.html", fout=fout)
                else:
                    db.execute(
                        "UPDATE beheerders SET mislukte_pogingen = 0, geblokkeerd_tot = NULL WHERE gebruikersnaam = ?",
                        (gebruiker,)
                    )
                    db.commit()
            except (ValueError, TypeError):
                pass

        # Timing-safe hash check — altijd uitvoeren om username-enumeratie via responstijd te voorkomen
        _te_controleren_hash = rij["wachtwoord_hash"] if rij else generate_password_hash("dummy-constant-waarde-tegen-timing")
        _hash_klopt = check_password_hash(_te_controleren_hash, wachtwoord)

        if rij and _hash_klopt:
            db.execute(
                "UPDATE beheerders SET mislukte_pogingen = 0, geblokkeerd_tot = NULL WHERE gebruikersnaam = ?",
                (gebruiker,)
            )
            db.commit()
            # Als 2FA actief is: ga naar de TOTP-stap (login nog niet compleet)
            if rij["totp_actief"]:
                session.clear()
                session["totp_pending_gebruiker"] = gebruiker
                session["totp_pending_versie"]    = rij["sessie_versie"] if rij["sessie_versie"] is not None else 0
                return redirect(url_for("admin_login_totp"))
            # Geen 2FA: login direct afronden
            _voltooi_login(gebruiker, rij["sessie_versie"])
            return redirect(url_for("admin"))

        # Mislukte login: verhoog teller, blokkeer na 10 opeenvolgende pogingen
        if rij:
            nieuwe_pogingen = (rij["mislukte_pogingen"] or 0) + 1
            if nieuwe_pogingen >= 10:
                from datetime import datetime as _dt, timedelta as _td
                geblokkeerd_tot = (_dt.now() + _td(minutes=15)).isoformat(timespec="seconds")
                db.execute(
                    "UPDATE beheerders SET mislukte_pogingen = ?, geblokkeerd_tot = ? WHERE gebruikersnaam = ?",
                    (nieuwe_pogingen, geblokkeerd_tot, gebruiker)
                )
                app.logger.warning(f"Account geblokkeerd na {nieuwe_pogingen} pogingen: {saniteer_log(gebruiker)} vanaf {get_client_ip()}")
                schrijf_audit_log("account_geblokkeerd", details=f"{nieuwe_pogingen} mislukte pogingen", gebruiker=gebruiker, ip=get_client_ip())
            else:
                db.execute(
                    "UPDATE beheerders SET mislukte_pogingen = ? WHERE gebruikersnaam = ?",
                    (nieuwe_pogingen, gebruiker)
                )
            db.commit()

        fout = "Onjuiste gebruikersnaam of wachtwoord."
        app.logger.warning(f"Mislukte admin-login voor '{saniteer_log(gebruiker)}' vanaf {get_client_ip()}")
        schrijf_audit_log("login_mislukt", gebruiker=gebruiker if gebruiker else None, ip=get_client_ip())
    return render_template("admin_login.html", fout=fout)


@app.route("/admin/logout", methods=["POST"])
@login_vereist
def admin_logout():
    gebruiker = session.get("admin_gebruikersnaam", "onbekend")
    schrijf_audit_log("logout", gebruiker=gebruiker, ip=get_client_ip())
    session.clear()
    app.logger.info(f"Admin uitgelogd: {saniteer_log(gebruiker)}")
    return redirect(url_for("admin_login"))


PAGINA_GROOTTE = 50


@app.route("/admin")
@login_vereist
def admin():
    GELDIGE_STATUSSEN = {"betaald", "aangemaakt", "mislukt", "verlopen", "geannuleerd"}
    try:
        db = get_db()
        status_filter = request.args.get("status", "")
        if status_filter not in GELDIGE_STATUSSEN:
            status_filter = ""
        zoekterm = request.args.get("zoek", "").strip()[:100]

        # Bouw WHERE-clausule op basis van actieve filters
        where_delen = []
        params = []
        if status_filter:
            where_delen.append("status=?")
            params.append(status_filter)
        if zoekterm:
            p = f"%{zoekterm}%"
            zoek_nummer = None
            zoek_clean = zoekterm.lstrip("#")
            if zoek_clean.isdigit():
                zoek_nummer = int(zoek_clean)
            if zoek_nummer is not None:
                where_delen.append(
                    "((voornaam LIKE ? OR achternaam LIKE ? OR (voornaam || ' ' || achternaam) LIKE ?) OR email LIKE ? OR CAST(lot_van AS TEXT) LIKE ? OR CAST(lot_tot AS TEXT) LIKE ? OR (lot_van <= ? AND lot_tot >= ?))"
                )
                params.extend([p, p, p, p, p, p, zoek_nummer, zoek_nummer])
            else:
                where_delen.append(
                    "((voornaam LIKE ? OR achternaam LIKE ? OR (voornaam || ' ' || achternaam) LIKE ?) OR email LIKE ? OR CAST(lot_van AS TEXT) LIKE ? OR CAST(lot_tot AS TEXT) LIKE ?)"
                )
                params.extend([p, p, p, p, p, p])
        where_sql = ("WHERE " + " AND ".join(where_delen)) if where_delen else ""

        GELDIGE_SORTERINGEN = {
            "id": "id", "voornaam": "voornaam", "achternaam": "achternaam", "datum": "aangemaakt_op",
            "aantal": "aantal", "bedrag": "bedrag", "lot": "lot_van", "status": "status",
        }
        sorter   = request.args.get("sorter", "id")
        richting = request.args.get("richting", "desc")
        if sorter not in GELDIGE_SORTERINGEN:
            sorter = "id"
        if richting not in ("asc", "desc"):
            richting = "desc"
        order_sql = f"ORDER BY {GELDIGE_SORTERINGEN[sorter]} {richting.upper()}"

        totaal = db.execute(
            f"SELECT COUNT(*) FROM bestellingen {where_sql}", params
        ).fetchone()[0]
        pagina       = max(1, request.args.get("pagina", 1, type=int))
        totaal_paginas = max(1, (totaal + PAGINA_GROOTTE - 1) // PAGINA_GROOTTE)
        pagina       = min(pagina, totaal_paginas)
        offset       = (pagina - 1) * PAGINA_GROOTTE
        bestellingen = db.execute(
            f"SELECT * FROM bestellingen {where_sql} {order_sql} LIMIT ? OFFSET ?",
            params + [PAGINA_GROOTTE, offset]
        ).fetchall()
        stats = db.execute("""
            SELECT
                COUNT(*)                                                          AS totaal_bestellingen,
                COALESCE(SUM(CASE WHEN status='betaald' THEN aantal END), 0)     AS verkochte_eendjes,
                COALESCE(SUM(CASE WHEN status='betaald' THEN bedrag END), 0)     AS totaal_omzet,
                COALESCE(SUM(CASE WHEN status='betaald'
                                   AND mail_verstuurd=0 THEN 1 END), 0)          AS mails_mislukt,
                COALESCE(SUM(CASE WHEN status='aangemaakt' THEN 1 END), 0)       AS openstaand
            FROM bestellingen
        """).fetchone()
        return render_template("admin.html",
                               bestellingen=bestellingen,
                               stats=stats,
                               max_eendjes=get_max_eendjes(),
                               pagina=pagina,
                               totaal_paginas=totaal_paginas,
                               totaal=totaal,
                               status_filter=status_filter,
                               zoekterm=zoekterm,
                               sorter=sorter,
                               richting=richting,
                               huidige_gebruiker=session.get("admin_gebruikersnaam"))
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout admin: {e}")
        abort(500)


@app.route("/admin/beheer")
@login_vereist
def admin_beheer():
    """Beheerpagina met instellingen, beheerders en gevaarzone."""
    try:
        db = get_db()
        beheerders_lijst = db.execute(
            "SELECT id, gebruikersnaam, aangemaakt_op, laatste_inlog, totp_actief FROM beheerders ORDER BY id"
        ).fetchall()
        audit_regels = db.execute(
            "SELECT tijdstip, gebruiker, actie, details, ip FROM audit_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        huidige_gebruiker = session.get("admin_gebruikersnaam")
        eigen_rij = db.execute(
            "SELECT totp_actief FROM beheerders WHERE gebruikersnaam = ?",
            (huidige_gebruiker,)
        ).fetchone()
        eigen_totp_actief = bool(eigen_rij["totp_actief"]) if eigen_rij else False
        return render_template("admin_beheer.html",
                               max_eendjes=get_max_eendjes(),
                               max_per_bestelling=get_max_per_bestelling(),
                               prijs_per_stuk=get_prijs_per_stuk(),
                               prijs_vijf_stuks=get_prijs_vijf_stuks(),
                               transactiekosten=get_transactiekosten(),
                               onderhoudsmodus=get_onderhoudsmodus(),
                               beheerders=beheerders_lijst,
                               audit_regels=audit_regels,
                               eigen_totp_actief=eigen_totp_actief,
                               huidige_gebruiker=huidige_gebruiker)
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout admin_beheer: {e}")
        abort(500)


@app.route("/admin/instellingen", methods=["POST"])
@login_vereist
def admin_instellingen():
    try:
        db   = get_db()
        meldingen = []
        fouten    = []

        # Verzamel en valideer alle waarden eerst; sla pas op als er geen fouten zijn
        updates = {}

        # max_eendjes
        max_e_str = request.form.get("max_eendjes", "").strip()
        if max_e_str:
            max_e = int(max_e_str)
            huidig_verkocht = db.execute(
                "SELECT COALESCE(SUM(aantal),0) AS n FROM bestellingen WHERE status='betaald'"
            ).fetchone()["n"]
            if max_e < huidig_verkocht:
                fouten.append(f"Totaal maximum kan niet lager dan het aantal al verkochte eendjes ({huidig_verkocht}) worden.")
            elif max_e < 1:
                fouten.append("Totaal maximum moet minimaal 1 zijn.")
            else:
                updates["max_eendjes"] = max_e
                meldingen.append(f"Totaal maximum bijgewerkt naar {max_e}.")

        # max_per_bestelling
        max_p_str = request.form.get("max_per_bestelling", "").strip()
        if max_p_str:
            max_p      = int(max_p_str)
            max_eendjes = updates.get("max_eendjes") or get_max_eendjes()
            if not (1 <= max_p <= max_eendjes):
                fouten.append(f"Maximum per bestelling moet tussen 1 en {max_eendjes} liggen.")
            else:
                updates["max_per_bestelling"] = max_p
                meldingen.append(f"Maximum per bestelling bijgewerkt naar {max_p}.")

        # prijs_per_stuk
        prijs_stuk_str = request.form.get("prijs_per_stuk", "").strip()
        if prijs_stuk_str:
            prijs_stuk = float(prijs_stuk_str)
            if prijs_stuk <= 0:
                fouten.append("Prijs per eendje moet groter dan 0 zijn.")
            else:
                updates["prijs_per_stuk"] = prijs_stuk
                meldingen.append(f"Prijs per eendje bijgewerkt naar € {prijs_stuk:.2f}.")

        # prijs_vijf_stuks
        prijs_vijf_str = request.form.get("prijs_vijf_stuks", "").strip()
        if prijs_vijf_str:
            prijs_vijf = float(prijs_vijf_str)
            if prijs_vijf <= 0:
                fouten.append("Prijs voor 5 eendjes moet groter dan 0 zijn.")
            else:
                updates["prijs_vijf_stuks"] = prijs_vijf
                meldingen.append(f"Prijs voor 5 eendjes bijgewerkt naar € {prijs_vijf:.2f}.")

        # transactiekosten
        tk_str = request.form.get("transactiekosten", "").strip()
        if tk_str:
            tk = float(tk_str)
            if tk < 0:
                fouten.append("Transactiekosten mogen niet negatief zijn.")
            else:
                updates["transactiekosten"] = tk
                meldingen.append(f"Transactiekosten bijgewerkt naar € {tk:.2f}.")

        # onderhoudsmodus (checkbox: aanwezig = aan, afwezig = uit)
        modus = 1 if request.form.get("onderhoudsmodus") else 0
        updates["onderhoudsmodus"] = modus
        meldingen.append("Onderhoudsmodus ingeschakeld." if modus else "Onderhoudsmodus uitgeschakeld.")

        if fouten:
            for f in fouten:
                flash(f, "fout")
        else:
            # Alleen opslaan als alle velden geldig zijn — atomisch
            _TOEGESTANE_KOLOMMEN = {
                "max_eendjes", "max_per_bestelling", "prijs_per_stuk",
                "prijs_vijf_stuks", "transactiekosten", "onderhoudsmodus",
            }
            for _kolom in updates:
                if _kolom not in _TOEGESTANE_KOLOMMEN:
                    raise ValueError(f"Ongeldige kolomnaam: {_kolom}")
            try:
                db.execute("BEGIN")
                for kolom, waarde in updates.items():
                    db.execute(f"UPDATE teller SET {kolom} = ? WHERE id = 1", (waarde,))
                db.execute("COMMIT")
                if meldingen:
                    flash(" ".join(meldingen), "info")
                    schrijf_audit_log("instellingen_gewijzigd", details="; ".join(meldingen), gebruiker=session.get("admin_gebruikersnaam"), ip=get_client_ip())
            except sqlite3.Error as db_err:
                try:
                    db.execute("ROLLBACK")
                except Exception:
                    pass
                app.logger.error(f"Fout bij opslaan instellingen: {db_err}")
                flash("Fout bij opslaan instellingen.", "fout")

    except (ValueError, TypeError):
        flash("Ongeldig getal opgegeven.", "fout")
    return redirect(url_for("admin_beheer"))


@app.route("/admin/beheerder-toevoegen", methods=["POST"])
@login_vereist
def beheerder_toevoegen():
    """Voeg een nieuw beheerdersaccount toe."""
    gebruikersnaam     = request.form.get("gebruikersnaam", "").strip()
    wachtwoord         = request.form.get("wachtwoord", "")
    wachtwoord_bevestiging = request.form.get("wachtwoord_bevestiging", "")

    if not gebruikersnaam:
        flash("Gebruikersnaam is verplicht.", "fout")
        return redirect(url_for("admin_beheer"))
    if len(wachtwoord) < 12:
        flash("Wachtwoord moet minimaal 12 tekens lang zijn.", "fout")
        return redirect(url_for("admin_beheer"))
    if wachtwoord != wachtwoord_bevestiging:
        flash("Wachtwoorden komen niet overeen.", "fout")
        return redirect(url_for("admin_beheer"))

    try:
        db = get_db()
        db.execute(
            "INSERT INTO beheerders (gebruikersnaam, wachtwoord_hash) VALUES (?, ?)",
            (gebruikersnaam, generate_password_hash(wachtwoord))
        )
        db.commit()
        app.logger.info(f"Nieuw beheerdersaccount aangemaakt: {saniteer_log(gebruikersnaam)} door {saniteer_log(session.get('admin_gebruikersnaam'))} (IP: {saniteer_log(get_client_ip())})")
        schrijf_audit_log("beheerder_aangemaakt", details=f"Nieuw account: {gebruikersnaam}", gebruiker=session.get("admin_gebruikersnaam"), ip=get_client_ip())
        flash(f"Account '{gebruikersnaam}' aangemaakt.", "info")
    except sqlite3.IntegrityError:
        flash(f"Gebruikersnaam '{gebruikersnaam}' bestaat al.", "fout")
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout beheerder_toevoegen: {e}")
        abort(500)
    return redirect(url_for("admin_beheer"))


@app.route("/admin/beheerder-verwijderen/<int:beheerder_id>", methods=["POST"])
@login_vereist
def beheerder_verwijderen(beheerder_id):
    """Verwijder een beheerdersaccount."""
    try:
        db = get_db()
        aantal = db.execute("SELECT COUNT(*) FROM beheerders").fetchone()[0]
        if aantal <= 1:
            flash("Het laatste account kan niet worden verwijderd.", "fout")
            return redirect(url_for("admin_beheer"))
        rij = db.execute(
            "SELECT gebruikersnaam FROM beheerders WHERE id = ?", (beheerder_id,)
        ).fetchone()
        if not rij:
            abort(404)
        if rij["gebruikersnaam"] == session.get("admin_gebruikersnaam"):
            flash("Je kunt je eigen account niet verwijderen.", "fout")
            return redirect(url_for("admin_beheer"))
        db.execute("DELETE FROM beheerders WHERE id = ?", (beheerder_id,))
        db.commit()
        app.logger.info(f"Beheerdersaccount verwijderd: {saniteer_log(rij['gebruikersnaam'])} door {saniteer_log(session.get('admin_gebruikersnaam'))} (IP: {saniteer_log(get_client_ip())})")
        schrijf_audit_log("beheerder_verwijderd", details=f"Account: {rij['gebruikersnaam']}", gebruiker=session.get("admin_gebruikersnaam"), ip=get_client_ip())
        flash(f"Account '{rij['gebruikersnaam']}' verwijderd.", "info")
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout beheerder_verwijderen: {e}")
        abort(500)
    return redirect(url_for("admin_beheer"))


@app.route("/admin/beheerder-wachtwoord-reset/<int:beheerder_id>", methods=["POST"])
@login_vereist
def beheerder_wachtwoord_reset(beheerder_id):
    """Reset het wachtwoord van een ander beheerdersaccount."""
    huidig_gebruiker = session.get("admin_gebruikersnaam")
    nieuw = request.form.get("nieuw_wachtwoord", "")
    bevestiging = request.form.get("nieuw_wachtwoord_bevestiging", "")
    try:
        db = get_db()
        rij = db.execute(
            "SELECT id, gebruikersnaam FROM beheerders WHERE id = ?", (beheerder_id,)
        ).fetchone()
        if not rij:
            abort(404)
        if rij["gebruikersnaam"] == huidig_gebruiker:
            flash("Gebruik de 'Wachtwoord wijzigen' knop voor je eigen account.", "fout")
            return redirect(url_for("admin_beheer"))
        if len(nieuw) < 12:
            flash("Nieuw wachtwoord moet minimaal 12 tekens zijn.", "fout")
            return redirect(url_for("admin_beheer"))
        if nieuw != bevestiging:
            flash("Wachtwoorden komen niet overeen.", "fout")
            return redirect(url_for("admin_beheer"))
        db.execute(
            "UPDATE beheerders SET wachtwoord_hash = ?, sessie_versie = sessie_versie + 1,"
            " mislukte_pogingen = 0, geblokkeerd_tot = NULL WHERE id = ?",
            (generate_password_hash(nieuw), beheerder_id)
        )
        db.commit()
        app.logger.info(f"Wachtwoord gereset voor: {saniteer_log(rij['gebruikersnaam'])} door {saniteer_log(huidig_gebruiker)} (IP: {saniteer_log(get_client_ip())})")
        schrijf_audit_log("wachtwoord_reset", details=f"Account: {rij['gebruikersnaam']}", gebruiker=huidig_gebruiker, ip=get_client_ip())
        flash(f"Wachtwoord van '{rij['gebruikersnaam']}' succesvol opnieuw ingesteld.", "info")
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout beheerder_wachtwoord_reset: {e}")
        abort(500)
    return redirect(url_for("admin_beheer"))


@app.route("/admin/wachtwoord-wijzigen", methods=["POST"])
@login_vereist
def wachtwoord_wijzigen():
    """Wijzig het wachtwoord van de ingelogde beheerder."""
    huidig = request.form.get("huidig_wachtwoord", "")
    nieuw = request.form.get("nieuw_wachtwoord", "")
    bevestiging = request.form.get("nieuw_wachtwoord_bevestiging", "")
    gebruiker = session.get("admin_gebruikersnaam")
    try:
        db = get_db()
        rij = db.execute(
            "SELECT id, wachtwoord_hash FROM beheerders WHERE gebruikersnaam = ?",
            (gebruiker,)
        ).fetchone()
        if not rij or not check_password_hash(rij["wachtwoord_hash"], huidig):
            flash("Huidig wachtwoord is onjuist.", "fout")
            return redirect(url_for("admin"))
        if len(nieuw) < 12:
            flash("Nieuw wachtwoord moet minimaal 12 tekens zijn.", "fout")
            return redirect(url_for("admin"))
        if nieuw != bevestiging:
            flash("Nieuwe wachtwoorden komen niet overeen.", "fout")
            return redirect(url_for("admin"))
        db.execute(
            "UPDATE beheerders SET wachtwoord_hash = ?, sessie_versie = sessie_versie + 1 WHERE id = ?",
            (generate_password_hash(nieuw), rij["id"])
        )
        db.commit()
        # Bijwerken van de huidige sessie naar de nieuwe versie zodat de eigen sessie geldig blijft;
        # alle andere sessies van deze gebruiker worden daarmee ongeldig.
        nieuwe_versie = db.execute(
            "SELECT sessie_versie FROM beheerders WHERE id = ?", (rij["id"],)
        ).fetchone()["sessie_versie"]
        session["admin_sessie_versie"] = nieuwe_versie
        app.logger.info(f"Wachtwoord gewijzigd voor: {saniteer_log(gebruiker)} (IP: {saniteer_log(get_client_ip())})")
        schrijf_audit_log("wachtwoord_gewijzigd", gebruiker=gebruiker, ip=get_client_ip())
        flash("Wachtwoord succesvol gewijzigd.", "info")
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout wachtwoord_wijzigen: {e}")
        abort(500)
    return redirect(url_for("admin"))


@app.route("/admin/2fa/instellen", methods=["GET"])
@login_vereist
def admin_2fa_instellen():
    """Toon de 2FA-instellingenpagina met QR-code."""
    gebruiker = session.get("admin_gebruikersnaam")
    db = get_db()
    rij = db.execute(
        "SELECT totp_geheim, totp_actief FROM beheerders WHERE gebruikersnaam = ?",
        (gebruiker,)
    ).fetchone()
    geheim   = rij["totp_geheim"]
    actief   = bool(rij["totp_actief"])
    qr_data  = None
    totp_uri = None
    if geheim and not actief:
        totp_uri = pyotp.TOTP(geheim).provisioning_uri(name=gebruiker, issuer_name="Badeendjesrace")
        qr_data  = genereer_qr_base64(totp_uri)
    return render_template("admin_2fa_instellen.html",
                           actief=actief,
                           geheim=geheim,
                           qr_data=qr_data,
                           totp_uri=totp_uri,
                           huidige_gebruiker=gebruiker)


@app.route("/admin/2fa/nieuw-geheim", methods=["POST"])
@login_vereist
def admin_2fa_nieuw_geheim():
    """Genereer een nieuw TOTP-geheim (nog niet actief — vereist bevestiging)."""
    gebruiker = session.get("admin_gebruikersnaam")
    geheim = pyotp.random_base32()
    try:
        db = get_db()
        db.execute(
            "UPDATE beheerders SET totp_geheim = ?, totp_actief = 0 WHERE gebruikersnaam = ?",
            (geheim, gebruiker)
        )
        db.commit()
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout 2fa nieuw geheim: {e}")
        abort(500)
    return redirect(url_for("admin_2fa_instellen"))


@app.route("/admin/2fa/bevestigen", methods=["POST"])
@login_vereist
def admin_2fa_bevestigen():
    """Activeer 2FA na verificatie van de eerste TOTP-code."""
    gebruiker = session.get("admin_gebruikersnaam")
    code = request.form.get("code", "").strip()
    db = get_db()
    rij = db.execute(
        "SELECT totp_geheim FROM beheerders WHERE gebruikersnaam = ?",
        (gebruiker,)
    ).fetchone()
    if not rij or not rij["totp_geheim"]:
        flash("Geen 2FA-geheim gevonden. Genereer eerst een nieuw geheim.", "fout")
        return redirect(url_for("admin_2fa_instellen"))
    totp = pyotp.TOTP(rij["totp_geheim"])
    if totp.verify(code, valid_window=1):
        try:
            db.execute(
                "UPDATE beheerders SET totp_actief = 1 WHERE gebruikersnaam = ?",
                (gebruiker,)
            )
            db.commit()
        except sqlite3.Error as e:
            app.logger.error(f"DB-fout 2fa bevestigen: {e}")
            abort(500)
        schrijf_audit_log("2fa_ingeschakeld", gebruiker=gebruiker, ip=get_client_ip())
        app.logger.info(f"2FA ingeschakeld voor {saniteer_log(gebruiker)}")
        flash("Tweefactorauthenticatie ingeschakeld.", "info")
        return redirect(url_for("admin_beheer"))
    flash("Ongeldige code. Scan de QR-code opnieuw en probeer het nog eens.", "fout")
    return redirect(url_for("admin_2fa_instellen"))


@app.route("/admin/2fa/uitschakelen", methods=["POST"])
@login_vereist
def admin_2fa_uitschakelen():
    """Schakel 2FA uit na verificatie van de huidige TOTP-code."""
    gebruiker = session.get("admin_gebruikersnaam")
    code = request.form.get("code", "").strip()
    db = get_db()
    rij = db.execute(
        "SELECT totp_geheim, totp_actief FROM beheerders WHERE gebruikersnaam = ?",
        (gebruiker,)
    ).fetchone()
    if not rij or not rij["totp_actief"]:
        flash("2FA is al uitgeschakeld.", "info")
        return redirect(url_for("admin_beheer"))
    totp = pyotp.TOTP(rij["totp_geheim"])
    if totp.verify(code, valid_window=1):
        try:
            db.execute(
                "UPDATE beheerders SET totp_actief = 0, totp_geheim = NULL WHERE gebruikersnaam = ?",
                (gebruiker,)
            )
            db.commit()
        except sqlite3.Error as e:
            app.logger.error(f"DB-fout 2fa uitschakelen: {e}")
            abort(500)
        schrijf_audit_log("2fa_uitgeschakeld", gebruiker=gebruiker, ip=get_client_ip())
        app.logger.info(f"2FA uitgeschakeld voor {saniteer_log(gebruiker)}")
        flash("Tweefactorauthenticatie uitgeschakeld.", "info")
        return redirect(url_for("admin_beheer"))
    flash("Ongeldige code. 2FA blijft ingeschakeld.", "fout")
    return redirect(url_for("admin_beheer"))


@app.route("/admin/mail-opnieuw/<int:bestelling_id>", methods=["POST"])
@login_vereist
def mail_opnieuw(bestelling_id):
    """Stuur bevestigingsmail opnieuw — werkt voor alle betaalde bestellingen."""
    try:
        db  = get_db()
        rij = db.execute(
            "SELECT * FROM bestellingen WHERE id=? AND status='betaald'", (bestelling_id,)
        ).fetchone()
        if not rij:
            abort(404)
        ok = stuur_bevestigingsmail(
            rij["voornaam"], rij["achternaam"], rij["email"], rij["aantal"],
            rij["lot_van"], rij["lot_tot"], rij["bedrag"],
            bool(rij["transactiekosten"]),
            tk_bedrag=rij["transactiekosten_bedrag"],
        )
        db.execute(
            "UPDATE bestellingen SET mail_verstuurd=?, pogingen=pogingen+1 WHERE id=?",
            (1 if ok else 0, bestelling_id),
        )
        db.commit()
        app.logger.info(f"Mail opnieuw verstuurd voor bestelling {bestelling_id}: {'ok' if ok else 'mislukt'}")
        schrijf_audit_log("mail_opnieuw", details=f"Bestelling #{bestelling_id} – {'verstuurd' if ok else 'mislukt'}", gebruiker=session.get("admin_gebruikersnaam"), ip=get_client_ip())
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout mail-opnieuw: {e}")
        abort(500)
    return redirect(url_for("admin"))


@app.route("/admin/export-csv")
@login_vereist
def export_csv():
    """Download alle bestellingen als CSV-bestand."""
    try:
        db = get_db()
        bestellingen = db.execute(
            "SELECT id, voornaam, achternaam, email, telefoon, aantal, bedrag, transactiekosten, "
            "lot_van, lot_tot, status, mail_verstuurd, aangemaakt_op, betaalwijze "
            "FROM bestellingen ORDER BY id ASC"
        ).fetchall()
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout export-csv: {e}")
        abort(500)

    def csv_escape(waarde):
        """Voorkom CSV/formula-injectie: prefixeer gevaarlijke starttekens met een apostrof."""
        tekst = str(waarde) if waarde is not None else ""
        if tekst and tekst[0] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + tekst
        return tekst

    uitvoer = io.StringIO()
    schrijver = csv.writer(uitvoer, delimiter=";", quoting=csv.QUOTE_ALL)
    schrijver.writerow([
        "ID", "Voornaam", "Achternaam", "E-mail", "Telefoon", "Aantal", "Bedrag (€)", "iDEAL-kosten",
        "Lot van", "Lot tot", "Status", "Betaalwijze", "Mail verstuurd", "Aangemaakt op"
    ])
    for b in bestellingen:
        schrijver.writerow([
            b["id"], csv_escape(b["voornaam"]), csv_escape(b["achternaam"]),
            csv_escape(b["email"]), csv_escape(b["telefoon"]),
            b["aantal"], f"{b['bedrag']:.2f}", "ja" if b["transactiekosten"] else "nee",
            b["lot_van"] or "", b["lot_tot"] or "",
            b["status"], b["betaalwijze"] or "ideal",
            "ja" if b["mail_verstuurd"] else "nee",
            b["aangemaakt_op"],
        ])

    return Response(
        "\ufeff" + uitvoer.getvalue(),  # BOM voor correcte weergave in Excel
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=bestellingen_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"}
    )


@app.route("/admin/bestelling/<int:bestelling_id>/wijzigen", methods=["GET", "POST"])
@login_vereist
def wijzig_bestelling(bestelling_id):
    """Bewerk naam, contact en status van een bestelling."""
    db  = get_db()
    rij = db.execute("SELECT * FROM bestellingen WHERE id=?", (bestelling_id,)).fetchone()
    if not rij:
        abort(404)

    fouten = []
    if request.method == "POST":
        voornaam       = request.form.get("voornaam", "").strip()
        achternaam     = request.form.get("achternaam", "").strip()
        telefoon       = request.form.get("telefoon", "").strip()
        email          = request.form.get("email", "").strip().lower()
        status         = request.form.get("status", "").strip()
        mail_verstuurd = 1 if request.form.get("mail_verstuurd") == "1" else 0

        geldige_statussen = ("aangemaakt", "betaald", "mislukt", "geannuleerd", "verlopen")
        if len(voornaam) < 2:
            fouten.append("Voornaam is verplicht (minimaal 2 tekens).")
        if len(achternaam) < 2:
            fouten.append("Achternaam is verplicht (minimaal 2 tekens).")
        if email and not EMAIL_RE.match(email):
            fouten.append("Vul een geldig e-mailadres in.")
        if status not in geldige_statussen:
            fouten.append("Ongeldige status.")

        if not fouten:
            try:
                db.execute(
                    "UPDATE bestellingen SET voornaam=?, achternaam=?, telefoon=?, email=?, status=?, "
                    "mail_verstuurd=?, bijgewerkt_op=datetime('now','localtime') WHERE id=?",
                    (voornaam, achternaam, telefoon, email, status, mail_verstuurd, bestelling_id),
                )
                db.commit()
                schrijf_audit_log("bestelling_gewijzigd", details=f"Bestelling #{bestelling_id}", gebruiker=session.get("admin_gebruikersnaam"), ip=get_client_ip())
            except sqlite3.Error as e:
                app.logger.error(f"DB-fout wijzig_bestelling: {e}")
                abort(500)
            return redirect(url_for("admin"))

    return render_template("wijzigen.html", bestelling=rij, fouten=fouten)


@app.route("/admin/opruimen", methods=["POST"])
@login_vereist
def admin_opruimen():
    """Verwijder verlopen/mislukte/geannuleerde bestellingen zonder lotnummers."""
    try:
        db      = get_db()
        aantal  = db.execute(
            "SELECT COUNT(*) AS n FROM bestellingen "
            "WHERE status IN ('verlopen','mislukt','geannuleerd') AND lot_van IS NULL"
        ).fetchone()["n"]
        db.execute(
            "DELETE FROM bestellingen "
            "WHERE status IN ('verlopen','mislukt','geannuleerd') AND lot_van IS NULL"
        )
        db.commit()
        flash(f"{aantal} ongeldige bestelling(en) verwijderd.", "info")
        app.logger.info(f"Admin opruimen: {aantal} bestellingen verwijderd.")
        schrijf_audit_log("opruimen", details=f"{aantal} bestelling(en) verwijderd", gebruiker=session.get("admin_gebruikersnaam"), ip=get_client_ip())
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout admin_opruimen: {e}")
        abort(500)
    return redirect(url_for("admin_beheer"))


@app.route("/admin/audit-wissen", methods=["POST"])
@login_vereist
def audit_wissen():
    """Wis de volledige audit-log."""
    try:
        db = get_db()
        db.execute("DELETE FROM audit_log")
        db.execute("DELETE FROM sqlite_sequence WHERE name='audit_log'")
        db.commit()
        schrijf_audit_log("audit_log_gewist", gebruiker=session.get("admin_gebruikersnaam"), ip=get_client_ip())
        app.logger.warning(f"Audit-log gewist door {saniteer_log(session.get('admin_gebruikersnaam'))} vanaf {get_client_ip()}")
        flash("Audit-log gewist.", "info")
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout audit_wissen: {e}")
        abort(500)
    return redirect(url_for("admin_beheer"))


@app.route("/admin/reset", methods=["POST"])
@login_vereist
def reset_database():
    """Verwijder alle bestellingen en reset de lotnummering."""
    bevestiging = request.form.get("bevestiging", "").strip()
    if bevestiging != "RESET":
        flash("Voer 'RESET' in om de database te wissen.", "fout")
        return redirect(url_for("admin_beheer"))
    try:
        db = get_db()
        db.execute("DELETE FROM bestellingen")
        db.execute("DELETE FROM sqlite_sequence WHERE name='bestellingen'")
        db.execute("DELETE FROM webhook_log")
        db.execute("DELETE FROM sqlite_sequence WHERE name='webhook_log'")
        db.execute("UPDATE teller SET volgend_lot=1")
        # Schrijf reset naar audit_log vóór het wissen (audit_log zelf wordt niet gewist)
        schrijf_audit_log("reset", details="Volledige database-reset: bestellingen en webhook_log gewist", gebruiker=session.get("admin_gebruikersnaam"), ip=get_client_ip())
        db.commit()
        app.logger.warning("Database volledig gereset door admin.")
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout reset_database: {e}")
        abort(500)
    return redirect(url_for("admin_beheer"))


@app.route("/admin/handmatig", methods=["POST"])
@login_vereist
def handmatige_bestelling():
    """Maak een handmatige bestelling aan (contant of overboeking) vanuit de admin."""
    voornaam    = request.form.get("h_voornaam", "").strip()
    achternaam  = request.form.get("h_achternaam", "").strip()
    email       = request.form.get("email", "").strip().lower()
    telefoon    = request.form.get("telefoon", "").strip()
    betaalwijze = request.form.get("betaalwijze", "contant").strip()

    try:
        aantal = int(request.form.get("aantal", 0))
    except (ValueError, TypeError):
        flash("Ongeldig aantal opgegeven.", "fout")
        return redirect(url_for("admin"))

    if betaalwijze not in ("contant", "overboeking"):
        betaalwijze = "contant"

    fouten = []
    if not voornaam or len(voornaam.strip()) < 2:
        fouten.append("Voornaam is verplicht (minimaal 2 tekens).")
    if len(voornaam) > 100:
        fouten.append("Voornaam mag maximaal 100 tekens zijn.")
    if not achternaam or len(achternaam.strip()) < 2:
        fouten.append("Achternaam is verplicht (minimaal 2 tekens).")
    if len(achternaam) > 100:
        fouten.append("Achternaam mag maximaal 100 tekens zijn.")
    if email and not EMAIL_RE.match(email):
        fouten.append("Ongeldig e-mailadres.")
    if telefoon and not TELEFOON_RE.match(telefoon):
        fouten.append("Ongeldig telefoonnummer.")
    if aantal < 1:
        fouten.append("Aantal moet minimaal 1 zijn.")
    max_per = get_max_per_bestelling()
    if aantal > max_per:
        fouten.append(f"Maximaal {max_per} eendjes per bestelling.")

    if fouten:
        for f in fouten:
            flash(f, "fout")
        return redirect(url_for("admin"))

    bedrag = bereken_bedrag(aantal, get_prijs_per_stuk(), get_prijs_vijf_stuks())

    try:
        db = get_db()
        cursor = db.execute(
            "INSERT INTO bestellingen (voornaam, achternaam, telefoon, email, aantal, bedrag, betaalwijze) "
            "VALUES (?,?,?,?,?,?,?)",
            (voornaam, achternaam, telefoon or "", email or "", aantal, bedrag, betaalwijze),
        )
        bestelling_id = cursor.lastrowid
        db.commit()
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout handmatige_bestelling INSERT: {e}")
        abort(500)

    try:
        lot_van, lot_tot = wijs_lotnummers_toe(db, bestelling_id, aantal)
    except ValueError:
        try:
            db.execute("DELETE FROM bestellingen WHERE id=?", (bestelling_id,))
            db.commit()
        except sqlite3.Error:
            pass
        flash("Niet genoeg lotnummers beschikbaar.", "fout")
        return redirect(url_for("admin"))
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout handmatige_bestelling lotnummers: {e}")
        abort(500)

    app.logger.info(
        f"Handmatige bestelling aangemaakt: id={bestelling_id}, "
        f"voornaam={saniteer_log(voornaam)}, achternaam={saniteer_log(achternaam)}, lotnummers={lot_van}-{lot_tot}, betaalwijze={betaalwijze}"
    )
    schrijf_audit_log("handmatige_bestelling", details=f"Bestelling #{bestelling_id} – {voornaam} {achternaam}, loten {lot_van}–{lot_tot}, {betaalwijze}", gebruiker=session.get("admin_gebruikersnaam"), ip=get_client_ip())

    if email:
        mail_ok = stuur_bevestigingsmail(voornaam, achternaam, email, aantal, lot_van, lot_tot, bedrag)
        if mail_ok:
            try:
                db.execute("UPDATE bestellingen SET mail_verstuurd=1 WHERE id=?", (bestelling_id,))
                db.commit()
            except sqlite3.Error:
                pass
    else:
        # Geen e-mail opgegeven — markeer als verstuurd zodat de waarschuwing niet verschijnt
        try:
            db.execute("UPDATE bestellingen SET mail_verstuurd=1 WHERE id=?", (bestelling_id,))
            db.commit()
        except sqlite3.Error:
            pass

    flash(
        f"Bestelling #{bestelling_id} aangemaakt — lotnummers {lot_van}–{lot_tot} ({betaalwijze}).",
        "info",
    )
    return redirect(url_for("admin"))


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/voorwaarden")
def voorwaarden():
    return render_template("voorwaarden.html")


@app.route("/setup", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def setup():
    """Eenmalige setup-pagina voor het aanmaken van het eerste beheerdersaccount."""
    global _setup_token
    if _setup_token is None:
        abort(404)
    token = request.args.get("token", "") or request.form.get("token", "")
    if not token or not secrets.compare_digest(token, _setup_token):
        abort(404)

    fouten = []
    if request.method == "POST":
        gebruikersnaam = request.form.get("gebruikersnaam", "").strip()
        wachtwoord     = request.form.get("wachtwoord", "")
        bevestiging    = request.form.get("bevestiging", "")
        if not gebruikersnaam:
            fouten.append("Gebruikersnaam is verplicht.")
        if len(wachtwoord) < 12:
            fouten.append("Wachtwoord moet minimaal 12 tekens lang zijn.")
        if wachtwoord and wachtwoord != bevestiging:
            fouten.append("Wachtwoorden komen niet overeen.")
        if not fouten:
            db = get_db()
            db.execute(
                "INSERT INTO beheerders (gebruikersnaam, wachtwoord_hash) VALUES (?, ?)",
                (gebruikersnaam, generate_password_hash(wachtwoord))
            )
            _setup_token = None
            if os.path.exists(_SETUP_TOKEN_BESTAND):
                os.remove(_SETUP_TOKEN_BESTAND)
            app.logger.info(
                f"Initieel beheerdersaccount aangemaakt via setup: {saniteer_log(gebruikersnaam)}"
            )
            flash("Account aangemaakt! Je kunt nu inloggen.", "succes")
            return redirect(url_for("admin_login"))

    return render_template("setup.html", token=token, fouten=fouten)


# ─── Database initialisatie (ook voor gunicorn) ───────────────────────────────
# BUG-FIX: init_db() stond alleen in if __name__ == "__main__", waardoor gunicorn
# (Procfile: gunicorn app:app) de tabellen nooit aanmaakte en direct crashte
# met "no such table: bestellingen".  Door het hier op module-niveau aan te
# roepen wordt de DB altijd geïnitialiseerd, ongeacht hoe de app gestart wordt.
if not RESEND_FROM:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "RESEND_FROM is niet ingesteld. Bevestigingsmails zullen falen."
    )

if not MOLLIE_API_KEY:
    if __name__ == "__main__":
        raise SystemExit("❌  MOLLIE_API_KEY is niet ingesteld.")
    else:
        # Gunicorn-start: waarschuw maar crash niet — betalingen zullen falen
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "MOLLIE_API_KEY is niet ingesteld. Betalingen via iDEAL zullen falen."
        )

with app.app_context():
    init_db()

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug, port=5000)
