"""
Testsuite — Badeendjes Lotenverkoop
====================================
Uitvoeren (geen extra packages nodig):
    python -m pytest tests/test_app.py -v
    # of zonder pytest:
    python tests/test_app.py

Vereisten: alleen Python stdlib + Flask (pip install flask)

Strategie: flask_wtf, flask_limiter en mollie worden met sys.modules
uitgestubbed vóór de import van app, zodat de suite ook draait zonder
die packages geïnstalleerd.
"""

import os
import sys
import html
import sqlite3
import unittest
from unittest.mock import MagicMock, patch
from types import ModuleType
from werkzeug.security import generate_password_hash


# ─── Stub ontbrekende packages vóór app-import ───────────────────────────────

def _stub_module(name, attrs=None):
    m = ModuleType(name)
    if attrs:
        for k, v in attrs.items():
            setattr(m, k, v)
    sys.modules[name] = m
    return m

# flask_wtf
_wtf = _stub_module("flask_wtf")

def _csrf_init(self, app=None):
    if app is not None:
        # Registreer csrf_token als Jinja2-global zodat templates niet crashen
        app.jinja_env.globals.setdefault("csrf_token", lambda: "")

_wtf.CSRFProtect = type("CSRFProtect", (), {
    "__init__": _csrf_init,
    "exempt":   lambda self, f: f,
})

# flask_limiter
_lim = _stub_module("flask_limiter")
_lim_util = _stub_module("flask_limiter.util")

def _get_remote_address():
    from flask import request
    return request.remote_addr or "127.0.0.1"

_lim_util.get_remote_address = _get_remote_address

class _FakeLimiter:
    def __init__(self, *a, **kw): pass
    def limit(self, *a, **kw):
        return lambda f: f
    def init_app(self, app): pass

_lim.Limiter = _FakeLimiter

# mollie
_stub_module("mollie")
_stub_module("mollie.api")
_mollie_client  = _stub_module("mollie.api.client")
_mollie_error   = _stub_module("mollie.api.error")

# resend
_resend = _stub_module("resend")
_resend.api_key = ""
_resend.Emails  = type("Emails", (), {"send": staticmethod(lambda params: {"id": "test"})})

class _FakeMollieClient:
    def __init__(self):
        self.payments = MagicMock()
    def set_api_key(self, key): pass

class _FakeMollieError(Exception): pass

_mollie_client.Client = _FakeMollieClient
_mollie_error.Error   = _FakeMollieError

# ─── Omgevingsvariabelen ──────────────────────────────────────────────────────
os.environ["MOLLIE_API_KEY"] = "test_sleutel"
os.environ["ADMIN_USER"]     = "admin"
os.environ["ADMIN_PASS"]     = "testpass"
os.environ["SECRET_KEY"]     = "testgeheim-32tekens-voor-signing!"
os.environ["LOG_DIR"]        = "/tmp/eendjes_test_logs"
os.environ["DATABASE"]       = "/tmp/eendjes_test.db"

# ─── Import app ──────────────────────────────────────────────────────────────
import app as App
from app import (
    bereken_bedrag, valideer_invoer, wijs_lotnummers_toe,
    stuur_bevestigingsmail, init_db, MAX_EENDJES,
)
MollieError = _FakeMollieError


# ─── Test-helpers ─────────────────────────────────────────────────────────────

def maak_db():
    """Verse in-memory SQLite met schema, isolation_level=None."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    for ddl in [
        """CREATE TABLE bestellingen (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            naam TEXT NOT NULL, telefoon TEXT NOT NULL, email TEXT NOT NULL,
            aantal INTEGER NOT NULL, bedrag REAL NOT NULL,
            mollie_id TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'aangemaakt',
            lot_van INTEGER, lot_tot INTEGER,
            mail_verstuurd INTEGER NOT NULL DEFAULT 0,
            pogingen INTEGER NOT NULL DEFAULT 0,
            transactiekosten INTEGER NOT NULL DEFAULT 0,
            transactiekosten_bedrag REAL NOT NULL DEFAULT 0,
            betaalwijze TEXT NOT NULL DEFAULT 'ideal',
            aangemaakt_op TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            bijgewerkt_op TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )""",
        """CREATE TABLE teller (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            volgend_lot INTEGER NOT NULL DEFAULT 1,
            max_eendjes INTEGER NOT NULL DEFAULT 3000,
            max_per_bestelling INTEGER NOT NULL DEFAULT 100,
            prijs_per_stuk REAL NOT NULL DEFAULT 2.50,
            prijs_vijf_stuks REAL NOT NULL DEFAULT 10.00,
            transactiekosten REAL NOT NULL DEFAULT 0.32
        )""",
        """CREATE TABLE webhook_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mollie_id TEXT, status TEXT, ip TEXT,
            ontvangen TEXT DEFAULT (datetime('now','localtime'))
        )""",
        """CREATE TABLE beheerders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gebruikersnaam TEXT NOT NULL UNIQUE,
            wachtwoord_hash TEXT NOT NULL,
            aangemaakt_op TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )""",
        f"INSERT INTO teller (id, volgend_lot, max_eendjes, max_per_bestelling, prijs_per_stuk, prijs_vijf_stuks, transactiekosten) VALUES (1, 1, {MAX_EENDJES}, 100, 2.50, 10.00, 0.32)",
        f"INSERT INTO beheerders (gebruikersnaam, wachtwoord_hash) VALUES ('admin', '{generate_password_hash('testpass')}')",
    ]:
        conn.execute(ddl)
    return conn


def maak_flask_client():
    App.app.config["TESTING"]          = True
    App.app.config["WTF_CSRF_ENABLED"] = False
    # Frisse database voor elke test — verwijder vorige testdata
    if os.path.exists(App.DATABASE):
        os.unlink(App.DATABASE)
    client = App.app.test_client()
    ctx    = App.app.app_context()
    ctx.push()
    init_db()
    return client, ctx


def simuleer_mollie_betaling(status: str) -> MagicMock:
    mock = MagicMock()
    mock.status       = status
    mock.is_paid      = MagicMock(return_value=(status == "paid"))
    mock.is_pending   = MagicMock(return_value=(status == "pending"))
    mock.is_open      = MagicMock(return_value=(status == "open"))
    mock.id           = "tr_test001"
    mock.checkout_url = "https://mollie.com/checkout/test"
    return mock


def doe_bestelling(client, naam="Jan Jansen", telefoon="0612345678",
                   email="jan@test.nl", aantal=2, transactiekosten=False):
    mock_b = simuleer_mollie_betaling("open")
    with patch("app.maak_mollie_client") as mc:
        mc.return_value.payments.create.return_value = mock_b
        data = {"naam": naam, "telefoon": telefoon,
                "email": email, "aantal": str(aantal)}
        if transactiekosten:
            data["transactiekosten"] = "1"
        return client.post("/bestellen", data=data)


def doe_webhook(client, mollie_id: str, status: str, ip="127.0.0.1"):
    mock_b = simuleer_mollie_betaling(status)
    with patch("app.maak_mollie_client") as mc, \
         patch("app.stuur_bevestigingsmail", return_value=True):
        mc.return_value.payments.get.return_value = mock_b
        return client.post(
            "/webhook",
            data={"id": mollie_id},
            environ_base={"REMOTE_ADDR": ip},
        )


def stel_mollie_id_in(bestelling_id=1, mollie_id="tr_test001"):
    App.get_db().execute(
        "UPDATE bestellingen SET mollie_id=? WHERE id=?",
        (mollie_id, bestelling_id)
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. PRIJSBEREKENING
# ══════════════════════════════════════════════════════════════════════════════

class TestBerekenBedrag(unittest.TestCase):

    def test_een_eendje(self):
        self.assertEqual(bereken_bedrag(1), 2.50)

    def test_twee_eendjes(self):
        self.assertEqual(bereken_bedrag(2), 5.00)

    def test_vier_eendjes_los(self):
        self.assertEqual(bereken_bedrag(4), 10.00)

    def test_vijf_eendjes_aanbieding(self):
        self.assertEqual(bereken_bedrag(5), 10.00)

    def test_zes_eendjes(self):
        self.assertEqual(bereken_bedrag(6), 12.50)

    def test_tien_eendjes(self):
        self.assertEqual(bereken_bedrag(10), 20.00)

    def test_elf_eendjes(self):
        self.assertEqual(bereken_bedrag(11), 22.50)

    def test_honderd_eendjes(self):
        self.assertEqual(bereken_bedrag(100), 200.00)

    def test_geen_floating_point_drift(self):
        self.assertEqual(bereken_bedrag(7), 15.00)

    def test_mollie_bedrag_altijd_twee_decimalen(self):
        for n in range(1, 11):
            s = f"{bereken_bedrag(n):.2f}"
            self.assertEqual(len(s.split(".")[1]), 2, f"n={n}: '{s}'")


# ══════════════════════════════════════════════════════════════════════════════
# 2. INVOERVALIDATIE
# ══════════════════════════════════════════════════════════════════════════════

class TestValideerInvoer(unittest.TestCase):

    def _ok(self, naam="Jan Jansen", tel="0612345678",
            email="jan@test.nl", aantal=3):
        return valideer_invoer(naam, tel, email, aantal)

    def test_geldige_invoer_geen_fouten(self):
        self.assertEqual(self._ok(), [])

    def test_naam_te_kort(self):
        self.assertGreater(len(self._ok(naam="A")), 0)

    def test_naam_leeg(self):
        self.assertGreater(len(self._ok(naam="")), 0)

    def test_naam_te_lang(self):
        fouten = self._ok(naam="A" * 101)
        self.assertTrue(any("100" in f for f in fouten))

    def test_email_zonder_at(self):
        self.assertGreater(len(self._ok(email="geenatsign")), 0)

    def test_email_zonder_domein(self):
        self.assertGreater(len(self._ok(email="x@")), 0)

    def test_telefoon_met_letters(self):
        fouten = self._ok(tel="abc-xyz")
        self.assertTrue(any("telefoon" in f.lower() for f in fouten))

    def test_telefoon_te_kort(self):
        self.assertGreater(len(self._ok(tel="123")), 0)

    def test_telefoon_alleen_spaties_geeft_fout(self):
        fouten = self._ok(tel="       ")
        self.assertTrue(any("telefoon" in f.lower() for f in fouten))

    def test_aantal_nul(self):
        fouten = self._ok(aantal=0)
        self.assertTrue(any("minimaal 1" in f.lower() for f in fouten))

    def test_aantal_negatief(self):
        self.assertGreater(len(self._ok(aantal=-1)), 0)

    def test_aantal_te_groot(self):
        fouten = self._ok(aantal=101)
        self.assertTrue(any("100" in f for f in fouten))

    def test_meerdere_fouten_tegelijk(self):
        fouten = valideer_invoer("", "", "geen-email", 0)
        self.assertGreaterEqual(len(fouten), 3)


# ══════════════════════════════════════════════════════════════════════════════
# 3. LOTNUMMER-TOEWIJZING
# ══════════════════════════════════════════════════════════════════════════════

class TestWijsLotnummersToe(unittest.TestCase):

    def setUp(self):
        self.db = maak_db()
        self.db.execute(
            "INSERT INTO bestellingen (naam,telefoon,email,aantal,bedrag) "
            "VALUES (?,?,?,?,?)", ("Jan", "06", "jan@t.nl", 3, 7.50)
        )

    def tearDown(self):
        self.db.close()

    def test_eerste_bestelling_krijgt_lot_1_tot_3(self):
        start, einde = wijs_lotnummers_toe(self.db, 1, 3)
        self.assertEqual(start, 1)
        self.assertEqual(einde, 3)

    def test_status_wordt_betaald(self):
        wijs_lotnummers_toe(self.db, 1, 3)
        rij = self.db.execute("SELECT status FROM bestellingen WHERE id=1").fetchone()
        self.assertEqual(rij["status"], "betaald")

    def test_lot_van_en_lot_tot_opgeslagen(self):
        wijs_lotnummers_toe(self.db, 1, 3)
        rij = self.db.execute("SELECT lot_van, lot_tot FROM bestellingen WHERE id=1").fetchone()
        self.assertEqual(rij["lot_van"], 1)
        self.assertEqual(rij["lot_tot"], 3)

    def test_opeenvolgende_bestellingen_unieke_nummers(self):
        self.db.execute(
            "INSERT INTO bestellingen (naam,telefoon,email,aantal,bedrag) "
            "VALUES (?,?,?,?,?)", ("Piet", "06", "piet@t.nl", 2, 5.00)
        )
        s1, e1 = wijs_lotnummers_toe(self.db, 1, 3)
        s2, e2 = wijs_lotnummers_toe(self.db, 2, 2)
        self.assertEqual(e1 + 1, s2)
        overlap = set(range(s1, e1 + 1)) & set(range(s2, e2 + 1))
        self.assertEqual(overlap, set())

    def test_oversell_gooit_value_error(self):
        self.db.execute("UPDATE teller SET volgend_lot=? WHERE id=1", (MAX_EENDJES,))
        with self.assertRaises(ValueError):
            wijs_lotnummers_toe(self.db, 1, 5)

    def test_na_oversell_teller_ongewijzigd(self):
        self.db.execute("UPDATE teller SET volgend_lot=? WHERE id=1", (MAX_EENDJES,))
        try:
            wijs_lotnummers_toe(self.db, 1, 5)
        except ValueError:
            pass
        rij = self.db.execute("SELECT volgend_lot FROM teller WHERE id=1").fetchone()
        self.assertEqual(rij["volgend_lot"], MAX_EENDJES)

    def test_na_oversell_status_ongewijzigd(self):
        self.db.execute("UPDATE teller SET volgend_lot=? WHERE id=1", (MAX_EENDJES,))
        try:
            wijs_lotnummers_toe(self.db, 1, 5)
        except ValueError:
            pass
        rij = self.db.execute("SELECT status FROM bestellingen WHERE id=1").fetchone()
        self.assertEqual(rij["status"], "aangemaakt")


# ══════════════════════════════════════════════════════════════════════════════
# 4. E-MAIL VEILIGHEID
# ══════════════════════════════════════════════════════════════════════════════

class TestEmailVeiligheid(unittest.TestCase):

    def _stuur_en_vang_body(self, naam):
        verzonden = {}
        def nep_send(params):
            verzonden["html"] = params.get("html", "")
            return {"id": "test"}
        with patch("resend.Emails.send", side_effect=nep_send):
            stuur_bevestigingsmail(naam, "test@test.nl", 2, 1, 2, 5.00)
        return verzonden.get("html", "")

    def test_scripttag_in_naam_geescaped(self):
        body = self._stuur_en_vang_body('<script>alert(1)</script>')
        self.assertNotIn("<script>", body)

    def test_html_entiteiten_aanwezig(self):
        body = self._stuur_en_vang_body('<b>Naam</b>')
        self.assertIn("&lt;b&gt;", body)

    def test_gewone_naam_verschijnt_in_body(self):
        body = self._stuur_en_vang_body("Jan Jansen")
        self.assertIn("Jan Jansen", body)

    def test_resend_fout_geeft_false_geen_exception(self):
        with patch("resend.Emails.send", side_effect=Exception("API fout")):
            resultaat = stuur_bevestigingsmail("Jan", "jan@t.nl", 1, 1, 1, 2.50)
        self.assertFalse(resultaat)

    def test_resend_geeft_true_bij_succes(self):
        with patch("resend.Emails.send", return_value={"id": "abc123"}):
            resultaat = stuur_bevestigingsmail("Jan", "jan@t.nl", 1, 1, 1, 2.50)
        self.assertTrue(resultaat)


# ══════════════════════════════════════════════════════════════════════════════
# 5. /api/prijs
# ══════════════════════════════════════════════════════════════════════════════

class TestApiPrijs(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_een_eendje(self):
        r = self.client.get("/api/prijs?aantal=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["bedrag"], 2.50)

    def test_vijf_eendjes_aanbieding(self):
        self.assertEqual(self.client.get("/api/prijs?aantal=5").get_json()["bedrag"], 10.00)

    def test_bedrag_tekst_gebruikt_komma(self):
        self.assertIn(",", self.client.get("/api/prijs?aantal=1").get_json()["bedrag_tekst"])

    def test_nul_geeft_400(self):
        self.assertEqual(self.client.get("/api/prijs?aantal=0").status_code, 400)

    def test_101_geeft_400(self):
        self.assertEqual(self.client.get("/api/prijs?aantal=101").status_code, 400)

    def test_tekst_geeft_400(self):
        self.assertEqual(self.client.get("/api/prijs?aantal=abc").status_code, 400)

    def test_geen_param_geeft_400(self):
        self.assertEqual(self.client.get("/api/prijs").status_code, 400)


# ══════════════════════════════════════════════════════════════════════════════
# 6. /bestellen
# ══════════════════════════════════════════════════════════════════════════════

class TestBestellen(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_geldige_bestelling_redirect_naar_mollie(self):
        r = doe_bestelling(self.client)
        self.assertEqual(r.status_code, 302)
        self.assertIn("mollie.com", r.headers["Location"])

    def test_lege_naam_geeft_422(self):
        self.assertEqual(doe_bestelling(self.client, naam="").status_code, 422)

    def test_ongeldig_email_geeft_422(self):
        self.assertEqual(doe_bestelling(self.client, email="geen-email").status_code, 422)

    def test_aantal_nul_geeft_422(self):
        self.assertEqual(doe_bestelling(self.client, aantal=0).status_code, 422)

    def test_aantal_101_geeft_422(self):
        self.assertEqual(doe_bestelling(self.client, aantal=101).status_code, 422)

    def test_aantal_als_tekst_geeft_400(self):
        r = self.client.post("/bestellen", data={
            "naam": "Jan", "telefoon": "0612345678",
            "email": "jan@test.nl", "aantal": "veel",
        })
        self.assertEqual(r.status_code, 400)

    def test_mollie_fout_geeft_503(self):
        with patch("app.maak_mollie_client") as mc:
            mc.return_value.payments.create.side_effect = MollieError("down")
            r = self.client.post("/bestellen", data={
                "naam": "Jan", "telefoon": "0612345678",
                "email": "jan@test.nl", "aantal": "1",
            })
        self.assertEqual(r.status_code, 503)

    def test_uitverkocht_geeft_409(self):
        App.get_db().execute("UPDATE teller SET max_eendjes=0 WHERE id=1")
        r = doe_bestelling(self.client, aantal=1)
        self.assertEqual(r.status_code, 409)


# ══════════════════════════════════════════════════════════════════════════════
# 7. WEBHOOK — alle betaalstatussen
# ══════════════════════════════════════════════════════════════════════════════

class TestWebhookStatussen(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        doe_bestelling(self.client)
        stel_mollie_id_in(1, "tr_test001")

    def tearDown(self):
        self.ctx.pop()

    def _db_rij(self):
        return App.get_db().execute(
            "SELECT status, lot_van, lot_tot, mail_verstuurd "
            "FROM bestellingen WHERE mollie_id='tr_test001'"
        ).fetchone()

    # ── paid ──────────────────────────────────────────────────────────────────

    def test_paid_http_200(self):
        self.assertEqual(doe_webhook(self.client, "tr_test001", "paid").status_code, 200)

    def test_paid_status_wordt_betaald(self):
        doe_webhook(self.client, "tr_test001", "paid")
        self.assertEqual(self._db_rij()["status"], "betaald")

    def test_paid_lotnummers_toegewezen(self):
        doe_webhook(self.client, "tr_test001", "paid")
        rij = self._db_rij()
        self.assertIsNotNone(rij["lot_van"])
        self.assertGreaterEqual(rij["lot_tot"], rij["lot_van"])

    def test_paid_mail_verstuurd_vlag(self):
        doe_webhook(self.client, "tr_test001", "paid")
        self.assertEqual(self._db_rij()["mail_verstuurd"], 1)

    def test_paid_dubbele_webhook_wijzigt_loten_niet(self):
        doe_webhook(self.client, "tr_test001", "paid")
        eerste_lot = self._db_rij()["lot_van"]
        doe_webhook(self.client, "tr_test001", "paid")
        self.assertEqual(self._db_rij()["lot_van"], eerste_lot)

    # ── pending ───────────────────────────────────────────────────────────────

    def test_pending_http_200(self):
        self.assertEqual(doe_webhook(self.client, "tr_test001", "pending").status_code, 200)

    def test_pending_status_ongewijzigd(self):
        doe_webhook(self.client, "tr_test001", "pending")
        self.assertEqual(self._db_rij()["status"], "aangemaakt")

    def test_pending_geen_lotnummers(self):
        doe_webhook(self.client, "tr_test001", "pending")
        self.assertIsNone(self._db_rij()["lot_van"])

    # ── open ──────────────────────────────────────────────────────────────────

    def test_open_http_200(self):
        self.assertEqual(doe_webhook(self.client, "tr_test001", "open").status_code, 200)

    def test_open_status_ongewijzigd(self):
        doe_webhook(self.client, "tr_test001", "open")
        self.assertEqual(self._db_rij()["status"], "aangemaakt")

    def test_open_geen_lotnummers(self):
        doe_webhook(self.client, "tr_test001", "open")
        self.assertIsNone(self._db_rij()["lot_van"])

    # ── failed ────────────────────────────────────────────────────────────────

    def test_failed_http_200(self):
        self.assertEqual(doe_webhook(self.client, "tr_test001", "failed").status_code, 200)

    def test_failed_status_wordt_mislukt(self):
        doe_webhook(self.client, "tr_test001", "failed")
        self.assertEqual(self._db_rij()["status"], "mislukt")

    def test_failed_geen_lotnummers(self):
        doe_webhook(self.client, "tr_test001", "failed")
        self.assertIsNone(self._db_rij()["lot_van"])

    # ── canceled ──────────────────────────────────────────────────────────────

    def test_canceled_http_200(self):
        self.assertEqual(doe_webhook(self.client, "tr_test001", "canceled").status_code, 200)

    def test_canceled_status_wordt_geannuleerd(self):
        doe_webhook(self.client, "tr_test001", "canceled")
        self.assertEqual(self._db_rij()["status"], "geannuleerd")

    def test_canceled_geen_lotnummers(self):
        doe_webhook(self.client, "tr_test001", "canceled")
        self.assertIsNone(self._db_rij()["lot_van"])

    # ── expired ───────────────────────────────────────────────────────────────

    def test_expired_http_200(self):
        self.assertEqual(doe_webhook(self.client, "tr_test001", "expired").status_code, 200)

    def test_expired_status_wordt_verlopen(self):
        doe_webhook(self.client, "tr_test001", "expired")
        self.assertEqual(self._db_rij()["status"], "verlopen")

    def test_expired_geen_lotnummers(self):
        doe_webhook(self.client, "tr_test001", "expired")
        self.assertIsNone(self._db_rij()["lot_van"])

    # ── onbekende status ──────────────────────────────────────────────────────

    def test_onbekende_status_valt_terug_op_mislukt(self):
        doe_webhook(self.client, "tr_test001", "charged_back")
        self.assertEqual(self._db_rij()["status"], "mislukt")

    # ── security ──────────────────────────────────────────────────────────────

    def test_onbekend_ip_wordt_niet_geblokkeerd(self):
        # IP-allowlisting is verwijderd op advies van Mollie; elk IP mag webhook sturen.
        # Beveiliging zit in het ophalen van de betaalstatus via geauthenticeerde API.
        r = doe_webhook(self.client, "tr_test001", "paid", ip="1.2.3.4")
        self.assertEqual(r.status_code, 200)

    def test_ongeldig_mollie_id_geeft_400(self):
        r = self.client.post("/webhook", data={"id": "ongeldig"},
                             environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 400)

    def test_leeg_id_geeft_400(self):
        r = self.client.post("/webhook", data={},
                             environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 400)

    def test_onbekende_bestelling_geeft_200(self):
        mock_b = simuleer_mollie_betaling("paid")
        with patch("app.maak_mollie_client") as mc, \
             patch("app.stuur_bevestigingsmail", return_value=True):
            mc.return_value.payments.get.return_value = mock_b
            r = self.client.post("/webhook", data={"id": "tr_bestaat_niet"},
                                 environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 200)

    def test_mollie_api_fout_geeft_500(self):
        with patch("app.maak_mollie_client") as mc:
            mc.return_value.payments.get.side_effect = MollieError("timeout")
            r = self.client.post("/webhook", data={"id": "tr_test001"},
                                 environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 500)

    def test_oversell_geblokkeerd_status_mislukt(self):
        App.get_db().execute(
            "UPDATE teller SET volgend_lot=? WHERE id=1", (MAX_EENDJES + 1,)
        )
        doe_webhook(self.client, "tr_test001", "paid")
        self.assertEqual(self._db_rij()["status"], "mislukt")
        self.assertIsNone(self._db_rij()["lot_van"])


# ══════════════════════════════════════════════════════════════════════════════
# 8. /betaald/<id>
# ══════════════════════════════════════════════════════════════════════════════

class TestBetaaldPagina(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def _zet_status(self, status, lot_van=None, lot_tot=None):
        doe_bestelling(self.client)
        db = App.get_db()
        db.execute(
            "UPDATE bestellingen SET status=?, mollie_id='tr_x', "
            "lot_van=?, lot_tot=? WHERE id=1",
            (status, lot_van, lot_tot)
        )

    def test_betaald_geeft_200(self):
        self._zet_status("betaald", lot_van=1, lot_tot=2)
        self.assertEqual(self.client.get("/betaald/1").status_code, 200)

    def test_betaald_bevat_succes(self):
        self._zet_status("betaald", lot_van=1, lot_tot=2)
        self.assertIn(b"succes", self.client.get("/betaald/1").data)

    def test_mislukt_geeft_200(self):
        self._zet_status("mislukt")
        self.assertEqual(self.client.get("/betaald/1").status_code, 200)

    def test_mislukt_bevat_fout(self):
        self._zet_status("mislukt")
        self.assertIn(b"fout", self.client.get("/betaald/1").data)

    def test_geannuleerd_geeft_200(self):
        self._zet_status("geannuleerd")
        self.assertEqual(self.client.get("/betaald/1").status_code, 200)

    def test_verlopen_geeft_200(self):
        self._zet_status("verlopen")
        self.assertEqual(self.client.get("/betaald/1").status_code, 200)

    def test_aangemaakt_wacht_pagina(self):
        self._zet_status("aangemaakt")
        mock_b = simuleer_mollie_betaling("open")
        with patch("app.maak_mollie_client") as mc:
            mc.return_value.payments.get.return_value = mock_b
            r = self.client.get("/betaald/1")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"wacht", r.data)

    def test_niet_bestaand_id_redirect(self):
        r = self.client.get("/betaald/99999")
        self.assertEqual(r.status_code, 302)

    def test_fallback_paid_wijst_loten_toe(self):
        doe_bestelling(self.client, aantal=1)
        stel_mollie_id_in(1, "tr_fallback")
        mock_b = simuleer_mollie_betaling("paid")
        with patch("app.maak_mollie_client") as mc, \
             patch("app.stuur_bevestigingsmail", return_value=True):
            mc.return_value.payments.get.return_value = mock_b
            self.client.get("/betaald/1")
        rij = App.get_db().execute(
            "SELECT status, lot_van FROM bestellingen WHERE id=1"
        ).fetchone()
        self.assertEqual(rij["status"], "betaald")
        self.assertIsNotNone(rij["lot_van"])

    def test_betaald_footer_bevat_organisatienaam(self):
        self._zet_status("betaald", lot_van=1, lot_tot=1)
        r = self.client.get("/betaald/1")
        self.assertIn("Diaconie Hervormde gemeente te Wapenveld".encode(), r.data)

    def test_betaald_footer_bevat_kvk(self):
        self._zet_status("betaald", lot_van=1, lot_tot=1)
        r = self.client.get("/betaald/1")
        self.assertIn(b"76404862", r.data)

    def test_betaald_footer_bevat_contactgegevens(self):
        self._zet_status("betaald", lot_van=1, lot_tot=1)
        r = self.client.get("/betaald/1")
        self.assertIn(b"diaconie@hervormdwapenveld.nl", r.data)
        self.assertIn(b"Kerkstraat", r.data)


# ══════════════════════════════════════════════════════════════════════════════
# 9. ADMIN
# ══════════════════════════════════════════════════════════════════════════════

class TestAdmin(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def _login(self):
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass"})

    def test_admin_zonder_login_geeft_302(self):
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r.headers["Location"])

    def test_fout_wachtwoord_blijft_op_loginpagina(self):
        r = self.client.post("/admin/login",
                             data={"gebruiker": "admin", "wachtwoord": "fout"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Onjuiste", r.data)

    def test_leeg_wachtwoord_altijd_geweigerd(self):
        r = self.client.post("/admin/login",
                             data={"gebruiker": "admin", "wachtwoord": ""})
        self.assertNotEqual(r.status_code, 302)

    def test_correct_login_geeft_toegang(self):
        self._login()
        self.assertEqual(self.client.get("/admin").status_code, 200)

    def test_logout_verwijdert_sessie(self):
        self._login()
        self.client.get("/admin/logout")
        self.assertEqual(self.client.get("/admin").status_code, 302)

    def test_admin_bevat_details_handmatige_bestelling(self):
        self._login()
        r = self.client.get("/admin")
        self.assertIn(b"<details", r.data)
        self.assertIn("Handmatige bestelling".encode(), r.data)

    def test_admin_bevat_details_instellingen(self):
        self._login()
        r = self.client.get("/admin")
        self.assertIn(b"<details", r.data)
        self.assertIn("Instellingen".encode(), r.data)

    def test_mail_opnieuw_niet_betaald_geeft_404(self):
        doe_bestelling(self.client)
        self._login()
        self.assertEqual(self.client.post("/admin/mail-opnieuw/1").status_code, 404)

    def test_mail_opnieuw_betaald_verstuurt_mail(self):
        doe_bestelling(self.client, aantal=2)
        App.get_db().execute(
            "UPDATE bestellingen SET status='betaald', "
            "lot_van=1, lot_tot=2, mail_verstuurd=0 WHERE id=1"
        )
        self._login()
        with patch("app.stuur_bevestigingsmail", return_value=True) as mm:
            r = self.client.post("/admin/mail-opnieuw/1")
        self.assertTrue(mm.called)
        self.assertEqual(r.status_code, 302)


# ══════════════════════════════════════════════════════════════════════════════
# 10. SECURITY HEADERS
# ══════════════════════════════════════════════════════════════════════════════

class TestSecurityHeaders(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_x_frame_options_deny(self):
        self.assertEqual(self.client.get("/").headers.get("X-Frame-Options"), "DENY")

    def test_x_content_type_options(self):
        self.assertEqual(
            self.client.get("/").headers.get("X-Content-Type-Options"), "nosniff"
        )

    def test_content_security_policy(self):
        self.assertIn("Content-Security-Policy", self.client.get("/").headers)

    def test_referrer_policy(self):
        self.assertIn("Referrer-Policy", self.client.get("/").headers)


# ══════════════════════════════════════════════════════════════════════════════
# 11. BUG-REGRESSIETESTS
#     Eén test per opgeloste bug — voorkomt regressie bij toekomstige wijzigingen
# ══════════════════════════════════════════════════════════════════════════════

class TestBugRegressie(unittest.TestCase):
    """
    Bug-overzicht
    ─────────────
    BUG-1  wijs_lotnummers_toe() gooide GEEN ValueError bij oversell
           → tests hingen ervan af; lotnummers boven MAX_EENDJES werden stil
             uitgedeeld.
    BUG-2  Webhook-handler ving ValueError van wijs_lotnummers_toe() niet op
           → bij oversell crashte de handler; status bleef op 'aangemaakt'.
    BUG-3  init_db() werd alleen aangeroepen in __main__
           → gunicorn initialiseerde de DB nooit; eerste request crashte met
             "no such table: bestellingen".
    BUG-4  Geen ROLLBACK bij sqlite3.Error in bestellen()
           → openstaande EXCLUSIVE lock tot teardown.
    BUG-5  wijs_lotnummers_toe() had geen idempotentie-check
           → gelijktijdige webhook + /betaald/<id> fallback konden beiden
             nieuwe lotnummers uitdelen voor dezelfde bestelling.
    BUG-6  Geen expliciete ROLLBACK in except Exception van betaald()-fallback
           → wijs_lotnummers_toe deed intern al ROLLBACK, maar de fout werd
             niet onderscheiden van andere exceptions.
    """

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    # ── BUG-1: oversell gooit ValueError ──────────────────────────────────────

    def test_bug1_oversell_gooit_value_error(self):
        """wijs_lotnummers_toe() moet ValueError gooien als einde > MAX_EENDJES."""
        db = maak_db()
        db.execute(
            "INSERT INTO bestellingen (naam,telefoon,email,aantal,bedrag) "
            "VALUES (?,?,?,?,?)", ("Jan", "06", "j@t.nl", 5, 10.00)
        )
        # Stel teller zo in dat volgend_lot al op de grens zit
        db.execute("UPDATE teller SET volgend_lot=? WHERE id=1", (MAX_EENDJES,))
        with self.assertRaises(ValueError):
            wijs_lotnummers_toe(db, 1, 5)
        db.close()

    def test_bug1_teller_ongewijzigd_na_oversell(self):
        """Teller mag NIET verhoogd worden als de ValueError gegooid wordt."""
        db = maak_db()
        db.execute(
            "INSERT INTO bestellingen (naam,telefoon,email,aantal,bedrag) "
            "VALUES (?,?,?,?,?)", ("Jan", "06", "j@t.nl", 5, 10.00)
        )
        db.execute("UPDATE teller SET volgend_lot=? WHERE id=1", (MAX_EENDJES,))
        try:
            wijs_lotnummers_toe(db, 1, 5)
        except ValueError:
            pass
        rij = db.execute("SELECT volgend_lot FROM teller WHERE id=1").fetchone()
        self.assertEqual(rij["volgend_lot"], MAX_EENDJES)
        db.close()

    def test_bug1_status_ongewijzigd_na_oversell(self):
        """Status mag NIET 'betaald' worden als er geen lotnummers beschikbaar zijn."""
        db = maak_db()
        db.execute(
            "INSERT INTO bestellingen (naam,telefoon,email,aantal,bedrag) "
            "VALUES (?,?,?,?,?)", ("Jan", "06", "j@t.nl", 5, 10.00)
        )
        db.execute("UPDATE teller SET volgend_lot=? WHERE id=1", (MAX_EENDJES,))
        try:
            wijs_lotnummers_toe(db, 1, 5)
        except ValueError:
            pass
        rij = db.execute("SELECT status FROM bestellingen WHERE id=1").fetchone()
        self.assertEqual(rij["status"], "aangemaakt")
        db.close()

    # ── BUG-2: webhook vangt ValueError op en zet status 'mislukt' ────────────

    def test_bug2_oversell_via_webhook_zet_status_mislukt(self):
        """Webhook moet status 'mislukt' zetten als oversell de betaling blokkeert."""
        doe_bestelling(self.client)
        stel_mollie_id_in(1, "tr_oversell")
        # Stel teller zo in dat er geen lotnummers meer over zijn
        App.get_db().execute(
            "UPDATE teller SET volgend_lot=? WHERE id=1", (MAX_EENDJES + 1,)
        )
        doe_webhook(self.client, "tr_oversell", "paid")
        rij = App.get_db().execute(
            "SELECT status, lot_van FROM bestellingen WHERE mollie_id='tr_oversell'"
        ).fetchone()
        self.assertEqual(rij["status"], "mislukt")
        self.assertIsNone(rij["lot_van"])

    def test_bug2_oversell_webhook_geeft_200_terug(self):
        """Webhook moet 200 teruggeven bij oversell (Mollie mag niet herproberen)."""
        doe_bestelling(self.client)
        stel_mollie_id_in(1, "tr_oversell2")
        App.get_db().execute(
            "UPDATE teller SET volgend_lot=? WHERE id=1", (MAX_EENDJES + 1,)
        )
        r = doe_webhook(self.client, "tr_oversell2", "paid")
        self.assertEqual(r.status_code, 200)

    # ── BUG-3: init_db() wordt aangeroepen bij module-import (gunicorn) ────────

    def test_bug3_tabellen_bestaan_na_module_import(self):
        """De drie tabellen moeten bestaan direct na import — ook zonder __main__."""
        db = App.get_db()
        tabellen = {
            rij[0] for rij in
            db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        self.assertIn("bestellingen", tabellen)
        self.assertIn("teller", tabellen)
        self.assertIn("webhook_log", tabellen)

    def test_bug3_teller_rij_aanwezig_na_init(self):
        """Teller-tabel moet een startrij hebben (id=1) na initialisatie."""
        rij = App.get_db().execute("SELECT volgend_lot FROM teller WHERE id=1").fetchone()
        self.assertIsNotNone(rij)
        self.assertGreaterEqual(rij["volgend_lot"], 1)

    # ── BUG-5: wijs_lotnummers_toe is idempotent ──────────────────────────────

    def test_bug5_dubbele_webhook_geeft_zelfde_loten_terug(self):
        """Een tweede webhook-aanroep voor dezelfde betaling mag GEEN nieuwe loten uitdelen."""
        doe_bestelling(self.client, aantal=3)
        stel_mollie_id_in(1, "tr_dubbel")
        doe_webhook(self.client, "tr_dubbel", "paid")
        eerste_lot_van = App.get_db().execute(
            "SELECT lot_van FROM bestellingen WHERE mollie_id='tr_dubbel'"
        ).fetchone()["lot_van"]

        # Tweede webhook-aanroep
        doe_webhook(self.client, "tr_dubbel", "paid")
        tweede_lot_van = App.get_db().execute(
            "SELECT lot_van FROM bestellingen WHERE mollie_id='tr_dubbel'"
        ).fetchone()["lot_van"]

        self.assertEqual(eerste_lot_van, tweede_lot_van,
            "Lotnummers zijn veranderd na een tweede webhook — dubbele toewijzing!")

    def test_bug5_teller_niet_verhoogd_bij_idempotente_aanroep(self):
        """De lotnummerteller mag niet oplopen als loten al eerder zijn toegewezen."""
        doe_bestelling(self.client, aantal=2)
        stel_mollie_id_in(1, "tr_idem")
        doe_webhook(self.client, "tr_idem", "paid")
        teller_na_eerste = App.get_db().execute(
            "SELECT volgend_lot FROM teller WHERE id=1"
        ).fetchone()["volgend_lot"]

        doe_webhook(self.client, "tr_idem", "paid")
        teller_na_tweede = App.get_db().execute(
            "SELECT volgend_lot FROM teller WHERE id=1"
        ).fetchone()["volgend_lot"]

        self.assertEqual(teller_na_eerste, teller_na_tweede,
            "Teller opgehoogd bij idempotente aanroep — loten verspild!")

    def test_bug5_fallback_plus_webhook_geen_dubbele_loten(self):
        """Webhook én /betaald/ fallback mogen samen NIET twee keer loten uitdelen."""
        doe_bestelling(self.client, aantal=2)
        stel_mollie_id_in(1, "tr_race")

        # Simuleer dat webhook EERST een betaald status verwerkt
        doe_webhook(self.client, "tr_race", "paid")
        loten_na_webhook = App.get_db().execute(
            "SELECT lot_van, lot_tot FROM bestellingen WHERE mollie_id='tr_race'"
        ).fetchone()

        # Dan roept /betaald/<id> de fallback aan (status is al 'betaald')
        mock_b = simuleer_mollie_betaling("paid")
        with patch("app.maak_mollie_client") as mc, \
             patch("app.stuur_bevestigingsmail", return_value=True):
            mc.return_value.payments.get.return_value = mock_b
            self.client.get("/betaald/1")

        loten_na_fallback = App.get_db().execute(
            "SELECT lot_van, lot_tot FROM bestellingen WHERE mollie_id='tr_race'"
        ).fetchone()

        self.assertEqual(loten_na_webhook["lot_van"],  loten_na_fallback["lot_van"],
            "lot_van is veranderd na fallback — dubbele toewijzing via race condition!")
        self.assertEqual(loten_na_webhook["lot_tot"],  loten_na_fallback["lot_tot"],
            "lot_tot is veranderd na fallback — dubbele toewijzing via race condition!")

    # ── BUG-6: betaald() fallback onderscheidt ValueError van andere exceptions ─

    def test_bug6_fallback_oversell_laat_status_op_aangemaakt(self):
        """Bij oversell in de fallback moet de status 'aangemaakt' blijven (webhook pakt het op)."""
        doe_bestelling(self.client, aantal=1)
        stel_mollie_id_in(1, "tr_fallback_oversell")
        App.get_db().execute(
            "UPDATE teller SET volgend_lot=? WHERE id=1", (MAX_EENDJES + 1,)
        )
        mock_b = simuleer_mollie_betaling("paid")
        with patch("app.maak_mollie_client") as mc, \
             patch("app.stuur_bevestigingsmail", return_value=True):
            mc.return_value.payments.get.return_value = mock_b
            r = self.client.get("/betaald/1")

        self.assertEqual(r.status_code, 200)
        rij = App.get_db().execute(
            "SELECT status FROM bestellingen WHERE mollie_id='tr_fallback_oversell'"
        ).fetchone()
        # Status blijft aangemaakt — webhook zet het daarna op mislukt
        self.assertIn(rij["status"], ("aangemaakt", "mislukt"))


# ══════════════════════════════════════════════════════════════════════════════
# 12. WIJZIGEN & VERWIJDEREN
# ══════════════════════════════════════════════════════════════════════════════

class TestWijzigenVerwijderen(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        doe_bestelling(self.client, naam="Jan Jansen", aantal=2)
        self._login()

    def tearDown(self):
        self.ctx.pop()

    def _login(self):
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass"})

    def _wijzig(self, bestelling_id=1, **kwargs):
        data = {
            "naam": "Jan Jansen", "telefoon": "0612345678",
            "email": "jan@test.nl", "status": "aangemaakt",
            "mail_verstuurd": "0",
        }
        data.update(kwargs)
        return self.client.post(f"/admin/bestelling/{bestelling_id}/wijzigen", data=data)

    # ── wijzigen GET ──────────────────────────────────────────────────────────

    def test_wijzigen_get_geeft_200(self):
        self.assertEqual(self.client.get("/admin/bestelling/1/wijzigen").status_code, 200)

    def test_wijzigen_get_onbekend_id_geeft_404(self):
        self.assertEqual(self.client.get("/admin/bestelling/99999/wijzigen").status_code, 404)

    def test_wijzigen_get_zonder_login_geeft_302(self):
        self.client.get("/admin/logout")
        self.assertEqual(self.client.get("/admin/bestelling/1/wijzigen").status_code, 302)

    # ── wijzigen POST ─────────────────────────────────────────────────────────

    def test_wijzigen_post_redirect_naar_admin(self):
        r = self._wijzig(naam="Piet Pietersen")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin", r.headers["Location"])

    def test_wijzigen_naam_wordt_opgeslagen(self):
        self._wijzig(naam="Piet Pietersen")
        rij = App.get_db().execute("SELECT naam FROM bestellingen WHERE id=1").fetchone()
        self.assertEqual(rij["naam"], "Piet Pietersen")

    def test_wijzigen_status_wordt_opgeslagen(self):
        self._wijzig(status="betaald")
        rij = App.get_db().execute("SELECT status FROM bestellingen WHERE id=1").fetchone()
        self.assertEqual(rij["status"], "betaald")

    def test_wijzigen_lege_naam_geeft_fout(self):
        r = self._wijzig(naam="")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"verplicht", r.data)

    def test_wijzigen_ongeldige_status_geeft_fout(self):
        r = self._wijzig(status="onbekend")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"status", r.data.lower())

    def test_wijzigen_ongeldig_email_geeft_fout(self):
        r = self._wijzig(email="geen-at-teken")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"e-mail", r.data.lower())

    def test_wijzigen_onbekend_id_geeft_404(self):
        r = self._wijzig(bestelling_id=99999)
        self.assertEqual(r.status_code, 404)

    def test_wijzigen_mail_verstuurd_vlag_opgeslagen(self):
        self._wijzig(mail_verstuurd="1")
        rij = App.get_db().execute("SELECT mail_verstuurd FROM bestellingen WHERE id=1").fetchone()
        self.assertEqual(rij["mail_verstuurd"], 1)

    # ── database reset ────────────────────────────────────────────────────────

    def test_reset_juiste_bevestiging_wist_alles(self):
        r = self.client.post("/admin/reset", data={"bevestiging": "RESET"})
        self.assertEqual(r.status_code, 302)
        db = App.get_db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM bestellingen").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT volgend_lot FROM teller").fetchone()[0], 1)

    def test_reset_verkeerde_bevestiging_doet_niets(self):
        r = self.client.post("/admin/reset", data={"bevestiging": "reset"})
        self.assertEqual(r.status_code, 302)
        db = App.get_db()
        self.assertGreater(db.execute("SELECT COUNT(*) FROM bestellingen").fetchone()[0], 0)

    def test_reset_zonder_login_geeft_302(self):
        self.client.get("/admin/logout")
        r = self.client.post("/admin/reset", data={"bevestiging": "RESET"})
        self.assertEqual(r.status_code, 302)


# ══════════════════════════════════════════════════════════════════════════════
# 13. TRANSACTIEKOSTEN
# ══════════════════════════════════════════════════════════════════════════════

class TestTransactiekosten(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    # ── /api/prijs ─────────────────────────────────────────────────────────────

    def test_prijs_zonder_tk_geen_toeslag(self):
        r = self.client.get("/api/prijs?aantal=2")
        self.assertEqual(r.get_json()["bedrag"], 5.00)

    def test_prijs_met_tk_voegt_32_cent_toe(self):
        r = self.client.get("/api/prijs?aantal=2&transactiekosten=1")
        self.assertAlmostEqual(r.get_json()["bedrag"], 5.32, places=2)

    def test_prijs_met_tk_tekst_bevat_komma(self):
        r = self.client.get("/api/prijs?aantal=1&transactiekosten=1")
        self.assertIn(",", r.get_json()["bedrag_tekst"])

    def test_prijs_tk_nul_geen_toeslag(self):
        """transactiekosten=0 moet zelfde resultaat geven als geen param."""
        r = self.client.get("/api/prijs?aantal=5&transactiekosten=0")
        self.assertEqual(r.get_json()["bedrag"], 10.00)

    # ── /bestellen opslaan ────────────────────────────────────────────────────

    def test_bestelling_zonder_tk_slaat_nul_op(self):
        doe_bestelling(self.client, transactiekosten=False)
        rij = App.get_db().execute(
            "SELECT transactiekosten FROM bestellingen WHERE id=1"
        ).fetchone()
        self.assertEqual(rij["transactiekosten"], 0)

    def test_bestelling_met_tk_slaat_een_op(self):
        doe_bestelling(self.client, transactiekosten=True)
        rij = App.get_db().execute(
            "SELECT transactiekosten FROM bestellingen WHERE id=1"
        ).fetchone()
        self.assertEqual(rij["transactiekosten"], 1)

    def test_bestelling_met_tk_hogere_bedrag_opgeslagen(self):
        doe_bestelling(self.client, aantal=2, transactiekosten=True)
        rij = App.get_db().execute(
            "SELECT bedrag FROM bestellingen WHERE id=1"
        ).fetchone()
        self.assertAlmostEqual(rij["bedrag"], 5.32, places=2)

    def test_bestelling_met_tk_slaat_bedrag_op_in_kolom(self):
        doe_bestelling(self.client, transactiekosten=True)
        rij = App.get_db().execute(
            "SELECT transactiekosten_bedrag FROM bestellingen WHERE id=1"
        ).fetchone()
        self.assertAlmostEqual(rij["transactiekosten_bedrag"], 0.32, places=2)

    def test_bestelling_zonder_tk_slaat_nul_op_in_kolom(self):
        doe_bestelling(self.client, transactiekosten=False)
        rij = App.get_db().execute(
            "SELECT transactiekosten_bedrag FROM bestellingen WHERE id=1"
        ).fetchone()
        self.assertAlmostEqual(rij["transactiekosten_bedrag"], 0.0, places=2)

    def test_bestelling_zonder_tk_normaal_bedrag_opgeslagen(self):
        doe_bestelling(self.client, aantal=2, transactiekosten=False)
        rij = App.get_db().execute(
            "SELECT bedrag FROM bestellingen WHERE id=1"
        ).fetchone()
        self.assertAlmostEqual(rij["bedrag"], 5.00, places=2)

    # ── e-mail tekst ──────────────────────────────────────────────────────────

    def test_email_met_tk_vermeldt_transactiekosten(self):
        verzonden = {}
        def nep_send(params):
            verzonden["html"] = params.get("html", "")
            return {"id": "test"}
        with patch("resend.Emails.send", side_effect=nep_send):
            stuur_bevestigingsmail("Jan", "jan@t.nl", 2, 1, 2, 5.32, transactiekosten=True)
        self.assertIn("transactiekosten", verzonden.get("html", "").lower())

    def test_email_zonder_tk_vermeldt_geen_transactiekosten(self):
        verzonden = {}
        def nep_send(params):
            verzonden["html"] = params.get("html", "")
            return {"id": "test"}
        with patch("resend.Emails.send", side_effect=nep_send):
            stuur_bevestigingsmail("Jan", "jan@t.nl", 2, 1, 2, 5.00, transactiekosten=False)
        self.assertNotIn("transactiekosten", verzonden.get("html", "").lower())


# ══════════════════════════════════════════════════════════════════════════════
# 14. MAX_PER_BESTELLING (configureerbaar)
# ══════════════════════════════════════════════════════════════════════════════

class TestMaxPerBestelling(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass"})

    def tearDown(self):
        self.ctx.pop()

    # ── valideer_invoer ───────────────────────────────────────────────────────

    def test_valideer_invoer_standaard_max_100(self):
        fouten = valideer_invoer("Jan Jansen", "0612345678", "jan@test.nl", 100)
        self.assertEqual(fouten, [])

    def test_valideer_invoer_101_met_standaard_geeft_fout(self):
        fouten = valideer_invoer("Jan Jansen", "0612345678", "jan@test.nl", 101)
        self.assertTrue(any("100" in f for f in fouten))

    def test_valideer_invoer_aangepast_max_50(self):
        fouten = valideer_invoer("Jan Jansen", "0612345678", "jan@test.nl", 50, max_per_bestelling=50)
        self.assertEqual(fouten, [])

    def test_valideer_invoer_51_met_max_50_geeft_fout(self):
        fouten = valideer_invoer("Jan Jansen", "0612345678", "jan@test.nl", 51, max_per_bestelling=50)
        self.assertTrue(any("50" in f for f in fouten))

    def test_valideer_invoer_foutmelding_bevat_max(self):
        fouten = valideer_invoer("Jan Jansen", "0612345678", "jan@test.nl", 25, max_per_bestelling=20)
        self.assertTrue(any("20" in f for f in fouten))

    # ── /api/prijs reageert op max_per_bestelling ─────────────────────────────

    def test_api_prijs_101_geeft_400_bij_default(self):
        self.assertEqual(self.client.get("/api/prijs?aantal=101").status_code, 400)

    def test_api_prijs_50_geeft_200_na_verlagen_max(self):
        App.get_db().execute("UPDATE teller SET max_per_bestelling=50 WHERE id=1")
        self.assertEqual(self.client.get("/api/prijs?aantal=50").status_code, 200)

    def test_api_prijs_51_geeft_400_na_verlagen_max(self):
        App.get_db().execute("UPDATE teller SET max_per_bestelling=50 WHERE id=1")
        self.assertEqual(self.client.get("/api/prijs?aantal=51").status_code, 400)

    # ── /bestellen reageert op max_per_bestelling ─────────────────────────────

    def test_bestellen_101_geeft_422_bij_default(self):
        self.assertEqual(doe_bestelling(self.client, aantal=101).status_code, 422)

    def test_bestellen_10_geeft_422_na_verlagen_max_naar_5(self):
        App.get_db().execute("UPDATE teller SET max_per_bestelling=5 WHERE id=1")
        self.assertEqual(doe_bestelling(self.client, aantal=10).status_code, 422)

    def test_bestellen_5_geeft_302_na_verlagen_max_naar_5(self):
        App.get_db().execute("UPDATE teller SET max_per_bestelling=5 WHERE id=1")
        self.assertEqual(doe_bestelling(self.client, aantal=5).status_code, 302)

    # ── admin instellingen-route ──────────────────────────────────────────────

    def test_admin_instellingen_wijzigt_max_per_bestelling(self):
        self.client.post("/admin/instellingen", data={"max_per_bestelling": "30"})
        rij = App.get_db().execute("SELECT max_per_bestelling FROM teller WHERE id=1").fetchone()
        self.assertEqual(rij["max_per_bestelling"], 30)

    def test_admin_instellingen_wijzigt_max_eendjes(self):
        self.client.post("/admin/instellingen", data={"max_eendjes": "500"})
        rij = App.get_db().execute("SELECT max_eendjes FROM teller WHERE id=1").fetchone()
        self.assertEqual(rij["max_eendjes"], 500)

    def test_admin_instellingen_zonder_login_geeft_302(self):
        self.client.get("/admin/logout")
        r = self.client.post("/admin/instellingen", data={"max_per_bestelling": "10"})
        self.assertEqual(r.status_code, 302)

    def test_admin_instellingen_max_eendjes_lager_dan_verkocht_geeft_fout(self):
        # Voeg een betaalde bestelling toe van 10 eendjes
        doe_bestelling(self.client, aantal=10)
        App.get_db().execute(
            "UPDATE bestellingen SET status='betaald', lot_van=1, lot_tot=10 WHERE id=1"
        )
        r = self.client.post("/admin/instellingen", data={"max_eendjes": "5"})
        self.assertEqual(r.status_code, 302)  # redirect terug
        rij = App.get_db().execute("SELECT max_eendjes FROM teller WHERE id=1").fetchone()
        self.assertGreater(rij["max_eendjes"], 5)  # niet verlaagd


# ══════════════════════════════════════════════════════════════════════════════
# 15. OPRUIMEN & API BESCHIKBAAR
# ══════════════════════════════════════════════════════════════════════════════

class TestOpruimenEnApiBeschikbaar(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass"})

    def tearDown(self):
        self.ctx.pop()

    # ── /api/beschikbaar ──────────────────────────────────────────────────────

    def test_api_beschikbaar_geeft_200(self):
        self.assertEqual(self.client.get("/api/beschikbaar").status_code, 200)

    def test_api_beschikbaar_bevat_velden(self):
        d = self.client.get("/api/beschikbaar").get_json()
        self.assertIn("verkocht", d)
        self.assertIn("beschikbaar", d)
        self.assertIn("max_eendjes", d)
        self.assertIn("max_per_bestelling", d)

    def test_api_beschikbaar_na_betaalde_bestelling(self):
        doe_bestelling(self.client, aantal=3)
        App.get_db().execute(
            "UPDATE bestellingen SET status='betaald', lot_van=1, lot_tot=3 WHERE id=1"
        )
        d = self.client.get("/api/beschikbaar").get_json()
        self.assertEqual(d["verkocht"], 3)
        self.assertEqual(d["beschikbaar"], d["max_eendjes"] - 3)

    # ── /admin/opruimen ───────────────────────────────────────────────────────

    def test_opruimen_verwijdert_verlopen_zonder_loten(self):
        doe_bestelling(self.client, aantal=2)
        App.get_db().execute("UPDATE bestellingen SET status='verlopen' WHERE id=1")
        self.client.post("/admin/opruimen")
        aantal = App.get_db().execute("SELECT COUNT(*) FROM bestellingen").fetchone()[0]
        self.assertEqual(aantal, 0)

    def test_opruimen_laat_betaalde_bestelling_staan(self):
        doe_bestelling(self.client, aantal=2)
        App.get_db().execute(
            "UPDATE bestellingen SET status='betaald', lot_van=1, lot_tot=2 WHERE id=1"
        )
        self.client.post("/admin/opruimen")
        aantal = App.get_db().execute("SELECT COUNT(*) FROM bestellingen").fetchone()[0]
        self.assertEqual(aantal, 1)

    def test_opruimen_laat_verlopen_met_loten_staan(self):
        """Verlopen bestelling die toch lotnummers heeft (edge case) blijft staan."""
        doe_bestelling(self.client, aantal=2)
        App.get_db().execute(
            "UPDATE bestellingen SET status='verlopen', lot_van=1, lot_tot=2 WHERE id=1"
        )
        self.client.post("/admin/opruimen")
        aantal = App.get_db().execute("SELECT COUNT(*) FROM bestellingen").fetchone()[0]
        self.assertEqual(aantal, 1)

    def test_opruimen_zonder_login_geeft_302(self):
        self.client.get("/admin/logout")
        r = self.client.post("/admin/opruimen")
        self.assertEqual(r.status_code, 302)


# ══════════════════════════════════════════════════════════════════════════════
# 16. BEVEILIGINGSVERBETERINGEN
# ══════════════════════════════════════════════════════════════════════════════

class TestBeveiligingsVerbeteringen(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def _login(self):
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass"})

    # ── CSP nonce ─────────────────────────────────────────────────────────────

    def test_csp_bevat_nonce(self):
        """CSP script-src moet een nonce bevatten, geen unsafe-inline."""
        csp = self.client.get("/").headers.get("Content-Security-Policy", "")
        self.assertIn("nonce-", csp)
        self.assertNotIn("'unsafe-inline'", csp.split("script-src")[1].split(";")[0])

    def test_csp_nonce_verschilt_per_request(self):
        """Elke request krijgt een unieke nonce."""
        csp1 = self.client.get("/").headers.get("Content-Security-Policy", "")
        csp2 = self.client.get("/").headers.get("Content-Security-Policy", "")
        nonce1 = [part for part in csp1.split() if part.startswith("'nonce-")][0]
        nonce2 = [part for part in csp2.split() if part.startswith("'nonce-")][0]
        self.assertNotEqual(nonce1, nonce2)

    # ── Permissions-Policy ────────────────────────────────────────────────────

    def test_permissions_policy_aanwezig(self):
        pp = self.client.get("/").headers.get("Permissions-Policy", "")
        self.assertIn("camera=()", pp)
        self.assertIn("microphone=()", pp)
        self.assertIn("geolocation=()", pp)

    # ── Sessie permanent na login ─────────────────────────────────────────────

    def test_sessie_is_permanent_na_login(self):
        """Na succesvol inloggen moet session.permanent True zijn."""
        with self.client.session_transaction() as sess:
            self.assertFalse(sess.get("admin_ingelogd", False))
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass"})
        with self.client.session_transaction() as sess:
            self.assertTrue(sess.get("admin_ingelogd", False))
            self.assertTrue(sess.permanent)

    # ── saniteer_log ──────────────────────────────────────────────────────────

    def test_saniteer_log_verwijdert_newline(self):
        from app import saniteer_log
        self.assertEqual(saniteer_log("jan\nnep@log.nl"), "jan nep@log.nl")

    def test_saniteer_log_verwijdert_carriage_return(self):
        from app import saniteer_log
        self.assertEqual(saniteer_log("jan\rnep@log.nl"), "jan nep@log.nl")

    def test_saniteer_log_laat_normale_tekst_ongewijzigd(self):
        from app import saniteer_log
        self.assertEqual(saniteer_log("jan@test.nl"), "jan@test.nl")

    # ── Paginering admin ──────────────────────────────────────────────────────

    def test_admin_pagina_1_geeft_200(self):
        self._login()
        self.assertEqual(self.client.get("/admin?pagina=1").status_code, 200)

    def test_admin_ongeldige_pagina_wordt_geklampt(self):
        """Pagina 999 bij lege DB → pagina 1 (totaal_paginas=1)."""
        self._login()
        self.assertEqual(self.client.get("/admin?pagina=999").status_code, 200)

    def test_admin_paginering_laadt_juiste_rijen(self):
        """Met 51 bestellingen moet pagina 2 zichtbaar zijn."""
        self._login()
        db = App.get_db()
        for i in range(51):
            db.execute(
                "INSERT INTO bestellingen (naam, telefoon, email, aantal, bedrag) "
                "VALUES (?,?,?,?,?)",
                (f"Koper {i}", "0600000000", f"k{i}@test.nl", 1, 2.50)
            )
        r = self.client.get("/admin?pagina=2")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Pagina", r.data)

    # ── Statusfilter ──────────────────────────────────────────────────────────

    def _vul_bestellingen(self):
        db = App.get_db()
        statussen = ["betaald", "betaald", "aangemaakt", "mislukt", "verlopen", "geannuleerd"]
        for i, status in enumerate(statussen):
            db.execute(
                "INSERT INTO bestellingen (naam, telefoon, email, aantal, bedrag, status) "
                "VALUES (?,?,?,?,?,?)",
                (f"Koper {i}", "0600000000", f"k{i}@test.nl", 1, 2.50, status)
            )

    def test_statusfilter_betaald_toont_alleen_betaald(self):
        self._login()
        self._vul_bestellingen()
        r = self.client.get("/admin?status=betaald")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'data-status="betaald"', r.data)
        self.assertNotIn(b'data-status="aangemaakt"', r.data)
        self.assertNotIn(b'data-status="mislukt"', r.data)

    def test_statusfilter_aangemaakt_toont_alleen_openstaand(self):
        self._login()
        self._vul_bestellingen()
        r = self.client.get("/admin?status=aangemaakt")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'data-status="aangemaakt"', r.data)
        self.assertNotIn(b'data-status="betaald"', r.data)
        self.assertNotIn(b'data-status="mislukt"', r.data)

    def test_statusfilter_ongeldig_valt_terug_op_alles(self):
        """Een ongeldige statuswaarde wordt genegeerd — alle bestellingen worden getoond."""
        self._login()
        self._vul_bestellingen()
        r = self.client.get("/admin?status=onzin")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'data-status="betaald"', r.data)
        self.assertIn(b'data-status="mislukt"', r.data)

    def test_statusfilter_leeg_toont_alles(self):
        self._login()
        self._vul_bestellingen()
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'data-status="betaald"', r.data)
        self.assertIn(b'data-status="mislukt"', r.data)


# ══════════════════════════════════════════════════════════════════════════════
# 17. Mail opnieuw versturen
# ══════════════════════════════════════════════════════════════════════════════

class TestMailOpnieuw(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        # Maak een betaalde bestelling aan
        doe_bestelling(self.client, transactiekosten=True)
        with patch("app.maak_mollie_client") as mc:
            mc.return_value.payments.get.return_value = simuleer_mollie_betaling("paid")
            self.client.post("/webhook", data={"id": "tr_test001"})
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass"})

    def tearDown(self):
        self.ctx.pop()

    def test_resend_betaalde_bestelling_geeft_redirect(self):
        with patch("resend.Emails.send", return_value={"id": "ok"}):
            r = self.client.post("/admin/mail-opnieuw/1")
        self.assertEqual(r.status_code, 302)

    def test_resend_al_verstuurd_werkt_ook(self):
        """Resend moet ook werken als mail al eerder verstuurd was."""
        App.get_db().execute("UPDATE bestellingen SET mail_verstuurd=1 WHERE id=1")
        with patch("resend.Emails.send", return_value={"id": "ok"}):
            r = self.client.post("/admin/mail-opnieuw/1")
        self.assertEqual(r.status_code, 302)

    def test_resend_gebruikt_opgeslagen_tk_bedrag(self):
        """De mail gebruikt het destijds opgeslagen transactiekosten-bedrag."""
        verzonden = {}
        def nep_send(params):
            verzonden["html"] = params.get("html", "")
            return {"id": "ok"}
        with patch("resend.Emails.send", side_effect=nep_send):
            self.client.post("/admin/mail-opnieuw/1")
        self.assertIn("transactiekosten", verzonden.get("html", "").lower())
        self.assertIn("0,32", verzonden.get("html", ""))

    def test_resend_niet_betaald_geeft_404(self):
        App.get_db().execute("UPDATE bestellingen SET status='aangemaakt' WHERE id=1")
        r = self.client.post("/admin/mail-opnieuw/1")
        self.assertEqual(r.status_code, 404)

    def test_resend_onbekend_id_geeft_404(self):
        r = self.client.post("/admin/mail-opnieuw/99999")
        self.assertEqual(r.status_code, 404)

    def test_resend_zonder_login_geeft_302(self):
        self.client.get("/admin/logout")
        r = self.client.post("/admin/mail-opnieuw/1")
        self.assertEqual(r.status_code, 302)


# ══════════════════════════════════════════════════════════════════════════════
# 18. Admin prijsinstellingen
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminPrijsInstellingen(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass"})

    def tearDown(self):
        self.ctx.pop()

    def _stel_in(self, **kwargs):
        data = {
            "max_eendjes": "3000", "max_per_bestelling": "100",
            "prijs_per_stuk": "2.50", "prijs_vijf_stuks": "10.00",
            "transactiekosten": "0.32",
        }
        data.update(kwargs)
        return self.client.post("/admin/instellingen", data=data,
                                follow_redirects=True)

    def test_prijs_per_stuk_wordt_opgeslagen(self):
        self._stel_in(prijs_per_stuk="3.00")
        self.assertAlmostEqual(App.get_prijs_per_stuk(), 3.00, places=2)

    def test_prijs_vijf_stuks_wordt_opgeslagen(self):
        self._stel_in(prijs_vijf_stuks="12.00")
        self.assertAlmostEqual(App.get_prijs_vijf_stuks(), 12.00, places=2)

    def test_transactiekosten_wordt_opgeslagen(self):
        self._stel_in(transactiekosten="0.45")
        self.assertAlmostEqual(App.get_transactiekosten(), 0.45, places=2)

    def test_prijs_per_stuk_nul_geeft_fout(self):
        r = self._stel_in(prijs_per_stuk="0")
        self.assertIn(b"groter dan 0", r.data)

    def test_prijs_vijf_stuks_negatief_geeft_fout(self):
        r = self._stel_in(prijs_vijf_stuks="-1")
        self.assertIn(b"groter dan 0", r.data)

    def test_transactiekosten_nul_is_geldig(self):
        """Transactiekosten mogen 0 zijn (geen iDEAL-toeslag)."""
        self._stel_in(transactiekosten="0")
        self.assertAlmostEqual(App.get_transactiekosten(), 0.0, places=2)

    def test_nieuwe_prijs_gebruikt_in_berekening(self):
        self._stel_in(prijs_per_stuk="3.00", prijs_vijf_stuks="12.00")
        r = self.client.get("/api/prijs?aantal=1")
        self.assertAlmostEqual(r.get_json()["bedrag"], 3.00, places=2)

    def test_nieuwe_prijs_vijf_gebruikt_in_berekening(self):
        self._stel_in(prijs_vijf_stuks="12.00")
        r = self.client.get("/api/prijs?aantal=5")
        self.assertAlmostEqual(r.get_json()["bedrag"], 12.00, places=2)

    def test_nieuwe_transactiekosten_gebruikt_in_api(self):
        self._stel_in(transactiekosten="0.50")
        r = self.client.get("/api/prijs?aantal=1&transactiekosten=1")
        self.assertAlmostEqual(r.get_json()["bedrag"], 2.50 + 0.50, places=2)


# ══════════════════════════════════════════════════════════════════════════════
# 18. CSV-injectie beveiliging
# ══════════════════════════════════════════════════════════════════════════════

class TestCsvInjectie(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass"})

    def tearDown(self):
        self.ctx.pop()

    def _download_csv(self):
        r = self.client.get("/admin/export-csv")
        self.assertEqual(r.status_code, 200)
        # Sla BOM over en decodeer
        return r.data.decode("utf-8-sig")

    def _bestel_met_naam(self, naam):
        doe_bestelling(self.client, naam=naam)

    def test_formule_in_naam_wordt_geescaped(self):
        self._bestel_met_naam("=SUM(1+1)")
        csv_tekst = self._download_csv()
        # Kaal "=SUM" (zonder voorafgaande apostrof) mag niet voorkomen
        self.assertNotIn('"=SUM', csv_tekst)
        self.assertIn("'=SUM", csv_tekst)

    def test_plus_prefix_wordt_geescaped(self):
        self._bestel_met_naam("+HYPERLINK()")
        csv_tekst = self._download_csv()
        self.assertIn("'+HYPERLINK", csv_tekst)

    def test_at_prefix_wordt_geescaped(self):
        self._bestel_met_naam("@SUM(A1)")
        csv_tekst = self._download_csv()
        self.assertIn("'@SUM", csv_tekst)

    def test_min_prefix_wordt_geescaped(self):
        self._bestel_met_naam("-1+2")
        csv_tekst = self._download_csv()
        self.assertIn("'-1+2", csv_tekst)

    def test_gewone_naam_ongewijzigd(self):
        self._bestel_met_naam("Jan Jansen")
        csv_tekst = self._download_csv()
        self.assertIn("Jan Jansen", csv_tekst)

    def test_bestandsnaam_bevat_tijdstempel(self):
        r = self.client.get("/admin/export-csv")
        self.assertEqual(r.status_code, 200)
        cd = r.headers.get("Content-Disposition", "")
        # Verwacht bijv. bestellingen_20260312_143022.csv
        self.assertRegex(cd, r"bestellingen_\d{8}_\d{6}\.csv")

    def test_csv_ideal_bestelling_heeft_betaalwijze_ideal(self):
        doe_bestelling(self.client, naam="iDEAL Koper")
        csv_tekst = self._download_csv()
        self.assertIn('"ideal"', csv_tekst)

    def test_csv_handmatige_bestelling_heeft_betaalwijze_contant(self):
        self.client.post("/admin/handmatig", data={
            "naam": "Contant Koper", "email": "", "telefoon": "",
            "aantal": "1", "betaalwijze": "contant",
        })
        csv_tekst = self._download_csv()
        self.assertIn('"contant"', csv_tekst)

    def test_csv_kolommen_tellen(self):
        """Header moet exact 13 kolommen bevatten."""
        csv_tekst = self._download_csv()
        header = csv_tekst.splitlines()[0]
        self.assertEqual(header.count(";"), 12)  # 13 kolommen = 12 scheidingstekens

    def test_csv_header_bevat_alle_kolomnamen(self):
        csv_tekst = self._download_csv()
        header = csv_tekst.splitlines()[0]
        for kolom in ["ID", "Naam", "E-mail", "Telefoon", "Aantal", "Bedrag",
                      "iDEAL-kosten", "Lot van", "Lot tot", "Status",
                      "Betaalwijze", "Mail verstuurd", "Aangemaakt op"]:
            self.assertIn(kolom, header)


class TestMailHeader(unittest.TestCase):

    def _vang_mail_params(self, naam="Jan"):
        verzonden = {}
        def nep_send(params):
            verzonden.update(params)
            return {"id": "test"}
        with patch("resend.Emails.send", side_effect=nep_send):
            stuur_bevestigingsmail(naam, "jan@test.nl", 1, 1, 1, 2.50)
        return verzonden

    def test_onderwerp_zonder_eend_emoji(self):
        params = self._vang_mail_params()
        self.assertNotIn("🦆", params.get("subject", ""))

    def test_onderwerp_bevat_lotnummers(self):
        params = self._vang_mail_params()
        self.assertIn("Jouw lotnummers", params.get("subject", ""))

    def test_mail_html_bevat_afbeelding(self):
        params = self._vang_mail_params()
        self.assertIn("eend.png", params.get("html", ""))

    def test_mail_html_bevat_blauwe_header(self):
        params = self._vang_mail_params()
        self.assertIn("#0077B6", params.get("html", ""))

    def test_mail_html_bevat_datum(self):
        params = self._vang_mail_params()
        self.assertIn("30 mei 2026", params.get("html", ""))


class TestNotificatieEmail(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass"})

    def tearDown(self):
        self.ctx.pop()

    def test_notificatie_email_instellen_en_opslaan(self):
        r = self.client.post("/admin/instellingen",
                             data={"notificatie_email": "beheer@test.nl"})
        self.assertEqual(r.status_code, 302)
        from app import get_notificatie_email
        self.assertEqual(get_notificatie_email(), "beheer@test.nl")

    def test_notificatie_email_leegmaken(self):
        self.client.post("/admin/instellingen",
                         data={"notificatie_email": "beheer@test.nl"})
        self.client.post("/admin/instellingen",
                         data={"notificatie_email": ""})
        from app import get_notificatie_email
        self.assertEqual(get_notificatie_email(), "")

    def test_ongeldig_notificatie_email_geeft_fout(self):
        r = self.client.post("/admin/instellingen",
                             data={"notificatie_email": "geen-email"},
                             follow_redirects=True)
        self.assertIn(b"ongeldig", r.data.lower())

    def test_notificatie_mail_wordt_verstuurd(self):
        # Stel notificatie-adres in
        self.client.post("/admin/instellingen",
                         data={"notificatie_email": "kopie@test.nl"})
        verzonden = []
        def nep_send(params):
            verzonden.append(params)
            return {"id": "test"}
        with patch("resend.Emails.send", side_effect=nep_send):
            from app import stuur_bevestigingsmail
            stuur_bevestigingsmail("Jan", "jan@test.nl", 1, 1, 1, 2.50)
        adressen = [p["to"][0] for p in verzonden]
        self.assertIn("jan@test.nl", adressen)
        self.assertIn("kopie@test.nl", adressen)

    def test_notificatie_onderwerp_bevat_kopie_label(self):
        self.client.post("/admin/instellingen",
                         data={"notificatie_email": "kopie@test.nl"})
        verzonden = []
        def nep_send(params):
            verzonden.append(params)
            return {"id": "test"}
        with patch("resend.Emails.send", side_effect=nep_send):
            from app import stuur_bevestigingsmail
            stuur_bevestigingsmail("Jan", "jan@test.nl", 1, 1, 1, 2.50)
        kopie = next(p for p in verzonden if p["to"][0] == "kopie@test.nl")
        self.assertIn("[Kopie]", kopie["subject"])

    def test_geen_notificatie_als_adres_leeg(self):
        self.client.post("/admin/instellingen",
                         data={"notificatie_email": ""})
        verzonden = []
        def nep_send(params):
            verzonden.append(params)
            return {"id": "test"}
        with patch("resend.Emails.send", side_effect=nep_send):
            from app import stuur_bevestigingsmail
            stuur_bevestigingsmail("Jan", "jan@test.nl", 1, 1, 1, 2.50)
        self.assertEqual(len(verzonden), 1)  # alleen naar klant


# ══════════════════════════════════════════════════════════════════════════════
# HANDMATIGE BESTELLINGEN
#     POST /admin/handmatig — contant/overboeking verkoop buiten iDEAL om
# ══════════════════════════════════════════════════════════════════════════════

class TestHandmatigeBestellingen(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass"})

    def tearDown(self):
        self.ctx.pop()

    def test_handmatige_bestelling_contant(self):
        r = self.client.post("/admin/handmatig", data={
            "naam": "Piet Pietersen", "email": "piet@test.nl",
            "telefoon": "0612345678", "aantal": "2", "betaalwijze": "contant",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        db = App.get_db()
        b = db.execute("SELECT * FROM bestellingen WHERE naam='Piet Pietersen'").fetchone()
        self.assertIsNotNone(b)
        self.assertEqual(b["status"], "betaald")
        self.assertEqual(b["betaalwijze"], "contant")
        self.assertIsNotNone(b["lot_van"])
        self.assertIsNotNone(b["lot_tot"])

    def test_handmatige_bestelling_overboeking(self):
        r = self.client.post("/admin/handmatig", data={
            "naam": "Klaas Klaassen", "email": "",
            "telefoon": "", "aantal": "3", "betaalwijze": "overboeking",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        db = App.get_db()
        b = db.execute("SELECT * FROM bestellingen WHERE naam='Klaas Klaassen'").fetchone()
        self.assertEqual(b["betaalwijze"], "overboeking")
        self.assertEqual(b["status"], "betaald")

    def test_handmatige_bestelling_zonder_email_geen_mail(self):
        with patch("app.stuur_bevestigingsmail") as mock_mail:
            self.client.post("/admin/handmatig", data={
                "naam": "Geen Email", "email": "",
                "telefoon": "", "aantal": "1", "betaalwijze": "contant",
            })
            mock_mail.assert_not_called()
        # mail_verstuurd=1 zodat de admin-waarschuwing niet verschijnt
        db = App.get_db()
        b = db.execute("SELECT mail_verstuurd FROM bestellingen WHERE naam='Geen Email'").fetchone()
        self.assertEqual(b["mail_verstuurd"], 1)

    def test_handmatige_bestelling_met_email_stuurt_mail(self):
        with patch("app.stuur_bevestigingsmail", return_value=True) as mock_mail:
            self.client.post("/admin/handmatig", data={
                "naam": "Met Email", "email": "met@test.nl",
                "telefoon": "", "aantal": "1", "betaalwijze": "contant",
            })
            mock_mail.assert_called_once()

    def test_handmatige_bestelling_lotnummers_oplopend(self):
        self.client.post("/admin/handmatig", data={
            "naam": "Eerste", "email": "", "telefoon": "",
            "aantal": "2", "betaalwijze": "contant",
        })
        self.client.post("/admin/handmatig", data={
            "naam": "Tweede", "email": "", "telefoon": "",
            "aantal": "3", "betaalwijze": "contant",
        })
        db = App.get_db()
        eerste = db.execute("SELECT lot_van, lot_tot FROM bestellingen WHERE naam='Eerste'").fetchone()
        tweede = db.execute("SELECT lot_van, lot_tot FROM bestellingen WHERE naam='Tweede'").fetchone()
        self.assertEqual(eerste["lot_van"], 1)
        self.assertEqual(eerste["lot_tot"], 2)
        self.assertEqual(tweede["lot_van"], 3)
        self.assertEqual(tweede["lot_tot"], 5)

    def test_handmatige_bestelling_naam_verplicht(self):
        r = self.client.post("/admin/handmatig", data={
            "naam": "", "email": "", "telefoon": "",
            "aantal": "1", "betaalwijze": "contant",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        db = App.get_db()
        count = db.execute("SELECT COUNT(*) FROM bestellingen").fetchone()[0]
        self.assertEqual(count, 0)

    def test_handmatige_bestelling_ongeldig_betaalwijze_valt_terug_op_contant(self):
        self.client.post("/admin/handmatig", data={
            "naam": "Test User", "email": "", "telefoon": "",
            "aantal": "1", "betaalwijze": "ideal",  # niet geldig voor handmatig
        })
        db = App.get_db()
        b = db.execute("SELECT betaalwijze FROM bestellingen WHERE naam='Test User'").fetchone()
        self.assertEqual(b["betaalwijze"], "contant")

    def test_ideal_bestelling_heeft_betaalwijze_ideal(self):
        doe_bestelling(self.client, naam="iDEAL Koper", aantal=1)
        db = App.get_db()
        b = db.execute("SELECT betaalwijze FROM bestellingen WHERE naam='iDEAL Koper'").fetchone()
        self.assertEqual(b["betaalwijze"], "ideal")

    def test_csv_bevat_betaalwijze_kolom(self):
        r = self.client.get("/admin/export-csv")
        self.assertIn(b"Betaalwijze", r.data)

    def test_handmatige_bestelling_vereist_admin_login(self):
        # Uitloggen
        uitgelogde_client, ctx2 = maak_flask_client()
        r = uitgelogde_client.post("/admin/handmatig", data={
            "naam": "Hacker", "email": "", "telefoon": "",
            "aantal": "1", "betaalwijze": "contant",
        })
        self.assertIn(r.status_code, [302, 401, 403])
        ctx2.pop()


# ══════════════════════════════════════════════════════════════════════════════
# WETTELIJKE PAGINA'S
#     /privacy en /voorwaarden — Mollie-vereisten en AVG-compliance
# ══════════════════════════════════════════════════════════════════════════════

class TestFoutPagina(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_fout_footer_bevat_organisatienaam(self):
        r = self.client.get("/bestaat-niet-xyz")
        self.assertIn("Diaconie Hervormde gemeente te Wapenveld".encode(), r.data)

    def test_fout_footer_bevat_kvk(self):
        r = self.client.get("/bestaat-niet-xyz")
        self.assertIn(b"76404862", r.data)

    def test_fout_footer_bevat_contactgegevens(self):
        r = self.client.get("/bestaat-niet-xyz")
        self.assertIn(b"diaconie@hervormdwapenveld.nl", r.data)
        self.assertIn(b"Kerkstraat", r.data)


class TestWettelijkePaginas(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_privacy_pagina_bereikbaar(self):
        r = self.client.get("/privacy")
        self.assertEqual(r.status_code, 200)

    def test_voorwaarden_pagina_bereikbaar(self):
        r = self.client.get("/voorwaarden")
        self.assertEqual(r.status_code, 200)

    def test_privacy_bevat_kvk(self):
        r = self.client.get("/privacy")
        self.assertIn(b"76404862", r.data)

    def test_voorwaarden_bevat_kvk(self):
        r = self.client.get("/voorwaarden")
        self.assertIn(b"76404862", r.data)

    def test_privacy_bevat_contactgegevens(self):
        r = self.client.get("/privacy")
        self.assertIn(b"diaconie@hervormdwapenveld.nl", r.data)
        self.assertIn(b"Kerkstraat", r.data)

    def test_voorwaarden_bevat_contactgegevens(self):
        r = self.client.get("/voorwaarden")
        self.assertIn(b"diaconie@hervormdwapenveld.nl", r.data)
        self.assertIn(b"Kerkstraat", r.data)

    def test_privacy_bevat_organisatienaam(self):
        r = self.client.get("/privacy")
        self.assertIn("Diaconie Hervormde gemeente te Wapenveld".encode(), r.data)

    def test_voorwaarden_bevat_organisatienaam(self):
        r = self.client.get("/voorwaarden")
        self.assertIn("Diaconie Hervormde gemeente te Wapenveld".encode(), r.data)


# ══════════════════════════════════════════════════════════════════════════════
# BEHEERDERSACCOUNTS
#     DB-gebaseerde multi-account authenticatie
# ══════════════════════════════════════════════════════════════════════════════

class TestBeheerderAccounts(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self._login()

    def tearDown(self):
        self.ctx.pop()

    def _login(self, gebruiker="admin", wachtwoord="testpass"):
        self.client.post("/admin/login",
                         data={"gebruiker": gebruiker, "wachtwoord": wachtwoord})

    def _voeg_toe(self, gebruikersnaam="nieuw_beheerder", wachtwoord="sterkwachtwoord1", bevestiging=None):
        if bevestiging is None:
            bevestiging = wachtwoord
        return self.client.post("/admin/beheerder-toevoegen", data={
            "gebruikersnaam": gebruikersnaam,
            "wachtwoord": wachtwoord,
            "wachtwoord_bevestiging": bevestiging,
        }, follow_redirects=True)

    def _verwijder(self, beheerder_id):
        return self.client.post(f"/admin/beheerder-verwijderen/{beheerder_id}",
                                follow_redirects=True)

    def test_beheerder_toevoegen_werkt(self):
        """Nieuw account verschijnt in de database."""
        self._voeg_toe(gebruikersnaam="testbeheerder")
        rij = App.get_db().execute(
            "SELECT gebruikersnaam FROM beheerders WHERE gebruikersnaam='testbeheerder'"
        ).fetchone()
        self.assertIsNotNone(rij)

    def test_beheerder_toevoegen_te_kort_wachtwoord(self):
        """Wachtwoord korter dan 12 tekens geeft foutmelding."""
        r = self._voeg_toe(wachtwoord="kort", bevestiging="kort")
        self.assertIn(b"12", r.data)

    def test_beheerder_toevoegen_wachtwoord_mismatch(self):
        """Niet-overeenkomende wachtwoorden geven foutmelding."""
        r = self._voeg_toe(wachtwoord="sterkwachtwoord1", bevestiging="anderwachtwoord2")
        self.assertIn(b"overeen", r.data)

    def test_beheerder_toevoegen_dubbele_naam(self):
        """Bestaande gebruikersnaam geeft foutmelding."""
        self._voeg_toe(gebruikersnaam="dubbel_beheerder")
        r = self._voeg_toe(gebruikersnaam="dubbel_beheerder")
        self.assertIn(b"bestaat al", r.data)

    def test_beheerder_verwijderen_werkt(self):
        """Tweede account kan worden verwijderd."""
        self._voeg_toe(gebruikersnaam="te_verwijderen")
        rij = App.get_db().execute(
            "SELECT id FROM beheerders WHERE gebruikersnaam='te_verwijderen'"
        ).fetchone()
        self.assertIsNotNone(rij)
        self._verwijder(rij["id"])
        weg = App.get_db().execute(
            "SELECT id FROM beheerders WHERE gebruikersnaam='te_verwijderen'"
        ).fetchone()
        self.assertIsNone(weg)

    def test_beheerder_verwijderen_laatste_geblokkeerd(self):
        """Als er maar 1 account is, mag het niet worden verwijderd."""
        rij = App.get_db().execute(
            "SELECT id FROM beheerders WHERE gebruikersnaam='admin'"
        ).fetchone()
        r = self._verwijder(rij["id"])
        self.assertIn(b"laatste", r.data)
        # Account nog steeds aanwezig
        nog_aanwezig = App.get_db().execute(
            "SELECT id FROM beheerders WHERE gebruikersnaam='admin'"
        ).fetchone()
        self.assertIsNotNone(nog_aanwezig)

    def test_beheerder_verwijderen_eigen_geblokkeerd(self):
        """Eigen account kan niet worden verwijderd."""
        # Voeg een tweede account toe zodat verwijdering in theorie mogelijk is
        self._voeg_toe(gebruikersnaam="ander_account")
        rij = App.get_db().execute(
            "SELECT id FROM beheerders WHERE gebruikersnaam='admin'"
        ).fetchone()
        r = self._verwijder(rij["id"])
        self.assertIn(b"eigen", r.data)
        nog_aanwezig = App.get_db().execute(
            "SELECT id FROM beheerders WHERE gebruikersnaam='admin'"
        ).fetchone()
        self.assertIsNotNone(nog_aanwezig)

    def test_tweede_account_kan_inloggen(self):
        """Nieuw aangemaakt account kan succesvol inloggen."""
        self._voeg_toe(gebruikersnaam="tweede_admin", wachtwoord="sterkwachtwoord2",
                       bevestiging="sterkwachtwoord2")
        # Uitloggen en inloggen met nieuw account
        self.client.get("/admin/logout")
        r = self.client.post("/admin/login",
                             data={"gebruiker": "tweede_admin",
                                   "wachtwoord": "sterkwachtwoord2"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin", r.headers["Location"])
        # Admin pagina bereikbaar
        self.assertEqual(self.client.get("/admin").status_code, 200)

    def _wijzig_wachtwoord(self, huidig, nieuw, bevestiging=None):
        if bevestiging is None:
            bevestiging = nieuw
        return self.client.post("/admin/wachtwoord-wijzigen", data={
            "huidig_wachtwoord": huidig,
            "nieuw_wachtwoord": nieuw,
            "nieuw_wachtwoord_bevestiging": bevestiging,
        }, follow_redirects=True)

    def test_wachtwoord_wijzigen_werkt(self):
        """Wachtwoord succesvol wijzigen met correct huidig wachtwoord."""
        r = self._wijzig_wachtwoord("testpass", "nieuwwachtwoord99")
        self.assertIn(b"succesvol", r.data)
        # Inloggen met nieuw wachtwoord werkt
        self.client.get("/admin/logout")
        r2 = self.client.post("/admin/login",
                              data={"gebruiker": "admin", "wachtwoord": "nieuwwachtwoord99"})
        self.assertEqual(r2.status_code, 302)

    def test_wachtwoord_wijzigen_fout_huidig(self):
        """Verkeerd huidig wachtwoord geeft foutmelding."""
        r = self._wijzig_wachtwoord("verkeerd", "nieuwwachtwoord99")
        self.assertIn(b"onjuist", r.data)

    def test_wachtwoord_wijzigen_te_kort(self):
        """Nieuw wachtwoord korter dan 12 tekens geeft foutmelding."""
        r = self._wijzig_wachtwoord("testpass", "kortww", "kortww")
        self.assertIn(b"12", r.data)

    def test_wachtwoord_wijzigen_mismatch(self):
        """Niet-overeenkomende nieuwe wachtwoorden geven foutmelding."""
        r = self._wijzig_wachtwoord("testpass", "nieuwwachtwoord99", "anderwachtwoord00")
        self.assertIn(b"overeen", r.data)


# ══════════════════════════════════════════════════════════════════════════════
# Uitvoeren als script
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
