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
import logging
import hmac
import secrets
import resend
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
MAX_EENDJES      = int(os.environ.get("MAX_EENDJES", 3000))
PRIJS_PER_STUK   = 2.50
PRIJS_VIJF_STUKS = 10.00
TRANSACTIEKOSTEN = 0.32  # iDEAL-transactiekosten Mollie

RESEND_API_KEY   = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM      = os.environ.get("RESEND_FROM", "")
AFZENDER_NAAM    = "Hervormde Gemeente Wapenveld"

ADMIN_GEBRUIKER  = os.environ.get("ADMIN_USER", "admin")
ADMIN_WACHTWOORD = os.environ.get("ADMIN_PASS", "")

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
TELEFOON_RE = re.compile(r"^[\d\s\+\-\(\)]{6,20}$")

def valideer_invoer(naam, telefoon, email, aantal, max_per_bestelling=100):
    fouten = []
    if not naam or len(naam.strip()) < 2:
        fouten.append("Vul een geldige naam in (minimaal 2 tekens).")
    if len(naam) > 100:
        fouten.append("Naam mag maximaal 100 tekens zijn.")
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
            naam            TEXT NOT NULL,
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
            transactiekosten INTEGER NOT NULL DEFAULT 0,
            aangemaakt_op   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            bijgewerkt_op   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS teller (
            id                  INTEGER PRIMARY KEY CHECK (id = 1),
            volgend_lot         INTEGER NOT NULL DEFAULT 1,
            max_eendjes         INTEGER NOT NULL DEFAULT 3000,
            max_per_bestelling  INTEGER NOT NULL DEFAULT 100
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
    # Migraties eerst — zodat kolommen bestaan vóór de INSERT
    try:
        conn.execute("ALTER TABLE bestellingen ADD COLUMN transactiekosten INTEGER NOT NULL DEFAULT 0")
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
    # Seed-rij: alleen aanmaken als nog niet bestaat (alle kolommen zijn nu gegarandeerd aanwezig)
    conn.execute(
        "INSERT OR IGNORE INTO teller (id, volgend_lot, max_eendjes, max_per_bestelling) VALUES (1, 1, ?, 100)",
        (MAX_EENDJES,)
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
                    naam            TEXT NOT NULL,
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
                    transactiekosten INTEGER NOT NULL DEFAULT 0,
                    aangemaakt_op   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    bijgewerkt_op   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                )
            """)
            conn.execute("INSERT INTO bestellingen SELECT * FROM _bestellingen_oud")
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


# ─── Business logica ──────────────────────────────────────────────────────────
def bereken_bedrag(aantal):
    vijftallen = aantal // 5
    rest       = aantal % 5
    return round(vijftallen * PRIJS_VIJF_STUKS + rest * PRIJS_PER_STUK, 2)


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
    except Exception:
        # Zorg dat de transactie altijd gesloten wordt bij een onverwachte fout
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return start, einde


def stuur_bevestigingsmail(naam, email, aantal, lot_van, lot_tot, bedrag, transactiekosten=False):
    """Geeft True bij succes, False bij fout — gooit nooit een exception."""
    naam = html.escape(naam)  # voorkom XSS via naam in HTML e-mail
    if lot_van == lot_tot:
        lotnr_tekst = f"lotnummer <strong>#{lot_van}</strong>"
    elif lot_tot - lot_van < 5:
        nummers = " &middot; ".join(f"#{n}" for n in range(lot_van, lot_tot + 1))
        lotnr_tekst = f"lotnummers <strong>{nummers}</strong>"
    else:
        lotnr_tekst = f"lotnummers <strong>#{lot_van} t/m #{lot_tot}</strong>"

    mail_html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;color:#333;">
      <div style="background:#FFD700;padding:24px;border-radius:12px 12px 0 0;text-align:center;">
        <h1 style="margin:0;color:#1a1a1a;">🦆 Eendjesrace!</h1>
        <p style="margin:4px 0 0;color:#444;">{AFZENDER_NAAM}</p>
      </div>
      <div style="background:#fffdf0;padding:32px;border:1px solid #eee;border-radius:0 0 12px 12px;">
        <p>Beste <strong>{naam}</strong>,</p>
        <p>Bedankt voor je bestelling! Je betaling van <strong>&euro;&nbsp;{bedrag:.2f}</strong> is ontvangen{' (incl. &euro;&nbsp;0,32 iDEAL-transactiekosten)' if transactiekosten else ''}.</p>
        <div style="background:#fff;border:2px solid #FFD700;border-radius:10px;padding:24px;margin:24px 0;text-align:center;">
          <p style="font-size:17px;margin:0 0 8px;">
            Je hebt <strong>{aantal}&nbsp;eend{'je' if aantal==1 else 'jes'}</strong> en ontvangt:
          </p>
          <p style="font-size:20px;margin:0;color:#0077B6;">{lotnr_tekst}</p>
        </div>
        <p>Op de dag van de race worden jouw eendjes te water gelaten. Bewaar je nummers goed!</p>
        <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
        <p style="font-size:.85rem;color:#999;">
          Dit is een automatisch bericht &mdash; niet beantwoorden.
        </p>
        <p>Met vriendelijke groet,<br><strong>{AFZENDER_NAAM}</strong></p>
      </div>
    </body></html>
    """
    try:
        resend.api_key = RESEND_API_KEY
        resend.Emails.send({
            "from":    f"{AFZENDER_NAAM} <{RESEND_FROM}>",
            "to":      [email],
            "subject": "🦆 Jouw lotnummers – Eendjesrace!",
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
        return f(*args, **kwargs)
    return wrapper


def saniteer_log(tekst):
    """Verwijder newlines uit gebruikersinvoer om log-injectie te voorkomen."""
    return str(tekst).replace("\n", " ").replace("\r", " ")


@app.before_request
def genereer_csp_nonce():
    g.csp_nonce = secrets.token_hex(16)


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
        "form-action 'self' https://www.mollie.com; "
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
        return render_template("index.html",
                               verkocht=betaald,
                               beschikbaar=max(0, max_eendjes - betaald),
                               max_eendjes=max_eendjes,
                               max_per_bestelling=get_max_per_bestelling())
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
        bedrag  = bereken_bedrag(aantal) + (TRANSACTIEKOSTEN if incl_tk else 0)
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


@app.route("/bestellen", methods=["POST"])
@limiter.limit("10 per minute")
def bestellen():
    naam             = request.form.get("naam", "").strip()
    telefoon         = request.form.get("telefoon", "").strip()
    email            = request.form.get("email", "").strip().lower()
    incl_tk          = request.form.get("transactiekosten") == "1"
    vorig            = {"naam": naam, "telefoon": telefoon, "email": email, "transactiekosten": incl_tk}

    try:
        aantal = int(request.form.get("aantal", 0))
        vorig["aantal"] = aantal
    except (ValueError, TypeError):
        return render_template("fout.html", code=400,
            titel="Ongeldig aantal",
            bericht="Het opgegeven aantal is niet geldig."), 400

    # Validatie
    max_per_bestelling = get_max_per_bestelling()
    max_eendjes        = get_max_eendjes()
    fouten = valideer_invoer(naam, telefoon, email, aantal, max_per_bestelling)
    if fouten:
        db          = get_db()
        betaald     = db.execute("SELECT COALESCE(SUM(aantal),0) AS n FROM bestellingen WHERE status='betaald'").fetchone()["n"]
        beschikbaar = max(0, max_eendjes - betaald)
        return render_template("index.html",
                               verkocht=betaald,
                               beschikbaar=beschikbaar,
                               max_eendjes=max_eendjes,
                               max_per_bestelling=max_per_bestelling,
                               fouten=fouten,
                               vorig=vorig), 422

    bedrag = round(bereken_bedrag(aantal) + (TRANSACTIEKOSTEN if incl_tk else 0), 2)

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
                                   fouten=[f"Er zijn nog maar {beschikbaar} eendjes beschikbaar."],
                                   vorig=vorig), 409

        cursor = db.execute(
            "INSERT INTO bestellingen (naam, telefoon, email, aantal, bedrag, transactiekosten) VALUES (?,?,?,?,?,?)",
            (naam, telefoon, email, aantal, bedrag, 1 if incl_tk else 0),
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
            "description": f"Eendjesrace – {aantal} eend{'je' if aantal==1 else 'jes'} ({naam})",
            "redirectUrl": f"{BASE_URL}/betaald/{bestelling_id}",
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
        return redirect(betaling.checkout_url)

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
@csrf.exempt
def webhook():
    client_ip = request.remote_addr
    mollie_id = request.form.get("id", "").strip()
    if not mollie_id or not mollie_id.startswith("tr_"):
        app.logger.warning(f"Webhook: ongeldig mollie_id '{saniteer_log(mollie_id)}'")
        return "", 400

    # Log de aanroep
    try:
        db = get_db()
        db.execute("INSERT INTO webhook_log (mollie_id, ip) VALUES (?,?)", (mollie_id, client_ip))
        db.commit()
    except sqlite3.Error:
        pass

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
                rij["naam"], rij["email"], rij["aantal"],
                lot_van, lot_tot, rij["bedrag"], bool(rij["transactiekosten"]),
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


@app.route("/betaald/<int:bestelling_id>")
def betaald(bestelling_id):
    """
    Landingspagina na terugkeer van Mollie.
    Webhook kan iets later komen dan deze redirect — dus fallback-check.
    """
    try:
        db  = get_db()
        rij = db.execute("SELECT * FROM bestellingen WHERE id=?", (bestelling_id,)).fetchone()
    except sqlite3.Error:
        abort(500)

    if not rij:
        return redirect(url_for("index"))

    # Fallback: als webhook nog niet binnengekomen is, check zelf bij Mollie
    if rij["status"] == "aangemaakt" and rij["mollie_id"] and MOLLIE_API_KEY:
        try:
            mollie   = maak_mollie_client()
            betaling = mollie.payments.get(rij["mollie_id"])
            if betaling.is_paid():
                lot_van, lot_tot = wijs_lotnummers_toe(db, bestelling_id, rij["aantal"])
                mail_ok = stuur_bevestigingsmail(
                    rij["naam"], rij["email"], rij["aantal"],
                    lot_van, lot_tot, rij["bedrag"], bool(rij["transactiekosten"]),
                )
                db.execute(
                    "UPDATE bestellingen SET mail_verstuurd=?, pogingen=pogingen+1 WHERE id=?",
                    (1 if mail_ok else 0, bestelling_id),
                )
                db.commit()
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
@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def admin_login():
    fout = None
    if request.method == "POST":
        gebruiker  = request.form.get("gebruiker", "")
        wachtwoord = request.form.get("wachtwoord", "")
        # hmac.compare_digest voorkomt timing-attacks
        ok_gebruiker  = hmac.compare_digest(gebruiker, ADMIN_GEBRUIKER)
        ok_wachtwoord = hmac.compare_digest(wachtwoord, ADMIN_WACHTWOORD)
        if ok_gebruiker and ok_wachtwoord and ADMIN_WACHTWOORD:
            session["admin_ingelogd"] = True
            session.permanent = True
            app.logger.info(f"Admin ingelogd vanaf {request.remote_addr}")
            return redirect(url_for("admin"))
        fout = "Onjuiste gebruikersnaam of wachtwoord."
        app.logger.warning(f"Mislukte admin-login vanaf {request.remote_addr}")
    return render_template("admin_login.html", fout=fout)


@app.route("/admin/logout")
@login_vereist
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


PAGINA_GROOTTE = 50


@app.route("/admin")
@login_vereist
def admin():
    try:
        db = get_db()
        pagina       = max(1, request.args.get("pagina", 1, type=int))
        offset       = (pagina - 1) * PAGINA_GROOTTE
        totaal       = db.execute("SELECT COUNT(*) FROM bestellingen").fetchone()[0]
        totaal_paginas = max(1, (totaal + PAGINA_GROOTTE - 1) // PAGINA_GROOTTE)
        pagina       = min(pagina, totaal_paginas)
        offset       = (pagina - 1) * PAGINA_GROOTTE
        bestellingen = db.execute(
            "SELECT * FROM bestellingen ORDER BY id DESC LIMIT ? OFFSET ?",
            (PAGINA_GROOTTE, offset)
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
                               max_per_bestelling=get_max_per_bestelling(),
                               pagina=pagina,
                               totaal_paginas=totaal_paginas,
                               totaal=totaal)
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout admin: {e}")
        abort(500)


@app.route("/admin/instellingen", methods=["POST"])
@login_vereist
def admin_instellingen():
    try:
        db   = get_db()
        meldingen = []
        fouten    = []

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
                db.execute("UPDATE teller SET max_eendjes = ? WHERE id = 1", (max_e,))
                meldingen.append(f"Totaal maximum bijgewerkt naar {max_e}.")

        # max_per_bestelling
        max_p_str = request.form.get("max_per_bestelling", "").strip()
        if max_p_str:
            max_p      = int(max_p_str)
            max_eendjes = int(max_e_str) if max_e_str and not fouten else get_max_eendjes()
            if not (1 <= max_p <= max_eendjes):
                fouten.append(f"Maximum per bestelling moet tussen 1 en {max_eendjes} liggen.")
            else:
                db.execute("UPDATE teller SET max_per_bestelling = ? WHERE id = 1", (max_p,))
                meldingen.append(f"Maximum per bestelling bijgewerkt naar {max_p}.")

        if fouten:
            for f in fouten:
                flash(f, "fout")
        if meldingen:
            flash(" ".join(meldingen), "info")

    except (ValueError, TypeError):
        flash("Ongeldig getal opgegeven.", "fout")
    return redirect(url_for("admin"))


@app.route("/admin/mail-opnieuw/<int:bestelling_id>", methods=["POST"])
@login_vereist
def mail_opnieuw(bestelling_id):
    """Stuur bevestigingsmail opnieuw — voor gevallen waarbij de mail eerder mislukte."""
    try:
        db  = get_db()
        rij = db.execute(
            "SELECT * FROM bestellingen WHERE id=? AND status='betaald'", (bestelling_id,)
        ).fetchone()
        if not rij:
            abort(404)
        ok = stuur_bevestigingsmail(
            rij["naam"], rij["email"], rij["aantal"],
            rij["lot_van"], rij["lot_tot"], rij["bedrag"],
        )
        db.execute(
            "UPDATE bestellingen SET mail_verstuurd=?, pogingen=pogingen+1 WHERE id=?",
            (1 if ok else 0, bestelling_id),
        )
        db.commit()
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
            "SELECT id, naam, email, telefoon, aantal, bedrag, transactiekosten, "
            "lot_van, lot_tot, status, mail_verstuurd, aangemaakt_op "
            "FROM bestellingen ORDER BY id ASC"
        ).fetchall()
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout export-csv: {e}")
        abort(500)

    uitvoer = io.StringIO()
    schrijver = csv.writer(uitvoer, delimiter=";")
    schrijver.writerow([
        "ID", "Naam", "E-mail", "Telefoon", "Aantal", "Bedrag (€)", "iDEAL-kosten",
        "Lot van", "Lot tot", "Status", "Mail verstuurd", "Aangemaakt op"
    ])
    for b in bestellingen:
        schrijver.writerow([
            b["id"], b["naam"], b["email"], b["telefoon"],
            b["aantal"], f"{b['bedrag']:.2f}", "ja" if b["transactiekosten"] else "nee",
            b["lot_van"] or "", b["lot_tot"] or "",
            b["status"], "ja" if b["mail_verstuurd"] else "nee",
            b["aangemaakt_op"],
        ])

    return Response(
        "\ufeff" + uitvoer.getvalue(),  # BOM voor correcte weergave in Excel
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=bestellingen.csv"}
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
        naam           = request.form.get("naam", "").strip()
        telefoon       = request.form.get("telefoon", "").strip()
        email          = request.form.get("email", "").strip().lower()
        status         = request.form.get("status", "").strip()
        mail_verstuurd = 1 if request.form.get("mail_verstuurd") == "1" else 0

        geldige_statussen = ("aangemaakt", "betaald", "mislukt", "geannuleerd", "verlopen")
        if len(naam) < 2:
            fouten.append("Naam is verplicht (minimaal 2 tekens).")
        if status not in geldige_statussen:
            fouten.append("Ongeldige status.")

        if not fouten:
            try:
                db.execute(
                    "UPDATE bestellingen SET naam=?, telefoon=?, email=?, status=?, "
                    "mail_verstuurd=?, bijgewerkt_op=datetime('now','localtime') WHERE id=?",
                    (naam, telefoon, email, status, mail_verstuurd, bestelling_id),
                )
                db.commit()
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
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout admin_opruimen: {e}")
        abort(500)
    return redirect(url_for("admin"))


@app.route("/admin/reset", methods=["POST"])
@login_vereist
def reset_database():
    """Verwijder alle bestellingen en reset de lotnummering."""
    bevestiging = request.form.get("bevestiging", "").strip()
    if bevestiging != "RESET":
        flash("Voer 'RESET' in om de database te wissen.", "fout")
        return redirect(url_for("admin"))
    try:
        db = get_db()
        db.execute("DELETE FROM bestellingen")
        db.execute("DELETE FROM webhook_log")
        db.execute("UPDATE teller SET volgend_lot=1")
        db.commit()
        app.logger.warning("Database volledig gereset door admin.")
    except sqlite3.Error as e:
        app.logger.error(f"DB-fout reset_database: {e}")
        abort(500)
    return redirect(url_for("admin"))


# ─── Database initialisatie (ook voor gunicorn) ───────────────────────────────
# BUG-FIX: init_db() stond alleen in if __name__ == "__main__", waardoor gunicorn
# (Procfile: gunicorn app:app) de tabellen nooit aanmaakte en direct crashte
# met "no such table: bestellingen".  Door het hier op module-niveau aan te
# roepen wordt de DB altijd geïnitialiseerd, ongeacht hoe de app gestart wordt.
with app.app_context():
    init_db()


if __name__ == "__main__":
    if not MOLLIE_API_KEY:
        raise SystemExit("❌  MOLLIE_API_KEY is niet ingesteld.")
    if not ADMIN_WACHTWOORD:
        raise SystemExit("❌  ADMIN_PASS is niet ingesteld. Kies een sterk wachtwoord.")
    if len(ADMIN_WACHTWOORD) < 12:
        raise SystemExit("❌  ADMIN_PASS moet minimaal 12 tekens lang zijn.")
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug, port=5000)
