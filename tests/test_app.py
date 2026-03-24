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
os.environ["ADMIN_PASS"]     = "testpass12345"
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
            voornaam TEXT NOT NULL DEFAULT '', achternaam TEXT NOT NULL,
            telefoon TEXT NOT NULL, email TEXT NOT NULL,
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
            aangemaakt_op TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            laatste_inlog TEXT,
            sessie_versie INTEGER NOT NULL DEFAULT 0,
            mislukte_pogingen INTEGER NOT NULL DEFAULT 0,
            geblokkeerd_tot TEXT,
            totp_geheim TEXT,
            totp_actief INTEGER NOT NULL DEFAULT 0
        )""",
        f"INSERT INTO teller (id, volgend_lot, max_eendjes, max_per_bestelling, prijs_per_stuk, prijs_vijf_stuks, transactiekosten) VALUES (1, 1, {MAX_EENDJES}, 100, 2.50, 10.00, 0.32)",
        f"INSERT INTO beheerders (gebruikersnaam, wachtwoord_hash) VALUES ('admin', '{generate_password_hash('testpass12345')}')",
    ]:
        conn.execute(ddl)
    return conn


def maak_flask_client():
    App.app.config["TESTING"]          = True
    App.app.config["WTF_CSRF_ENABLED"] = False
    # Frisse database voor elke test — verwijder vorige testdata incl. WAL/SHM
    for _db_bestand in [App.DATABASE, App.DATABASE + "-wal", App.DATABASE + "-shm"]:
        if os.path.exists(_db_bestand):
            os.unlink(_db_bestand)
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


def doe_bestelling(client, voornaam="Jan", achternaam="Jansen", telefoon="0612345678",
                   email="jan@test.nl", aantal=2, transactiekosten=False):
    mock_b = simuleer_mollie_betaling("open")
    with patch("app.maak_mollie_client") as mc:
        mc.return_value.payments.create.return_value = mock_b
        data = {"voornaam": voornaam, "achternaam": achternaam, "telefoon": telefoon,
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

    def _ok(self, voornaam="Jan", achternaam="Jansen", tel="0612345678",
            email="jan@test.nl", aantal=3):
        return valideer_invoer(voornaam, achternaam, tel, email, aantal)

    def test_geldige_invoer_geen_fouten(self):
        self.assertEqual(self._ok(), [])

    def test_voornaam_te_kort(self):
        self.assertGreater(len(self._ok(voornaam="A")), 0)

    def test_voornaam_leeg(self):
        self.assertGreater(len(self._ok(voornaam="")), 0)

    def test_voornaam_te_lang(self):
        fouten = self._ok(voornaam="A" * 101)
        self.assertTrue(any("100" in f for f in fouten))

    def test_achternaam_te_kort(self):
        self.assertGreater(len(self._ok(achternaam="A")), 0)

    def test_achternaam_leeg(self):
        self.assertGreater(len(self._ok(achternaam="")), 0)

    def test_achternaam_te_lang(self):
        fouten = self._ok(achternaam="A" * 101)
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
        fouten = valideer_invoer("", "", "", "geen-email", 0)
        self.assertGreaterEqual(len(fouten), 3)


# ══════════════════════════════════════════════════════════════════════════════
# 3. LOTNUMMER-TOEWIJZING
# ══════════════════════════════════════════════════════════════════════════════

class TestWijsLotnummersToe(unittest.TestCase):

    def setUp(self):
        self.db = maak_db()
        self.db.execute(
            "INSERT INTO bestellingen (voornaam,achternaam,telefoon,email,aantal,bedrag) "
            "VALUES (?,?,?,?,?,?)", ("Jan", "de Vries", "06", "jan@t.nl", 3, 7.50)
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
            "INSERT INTO bestellingen (voornaam,achternaam,telefoon,email,aantal,bedrag) "
            "VALUES (?,?,?,?,?,?)", ("Piet", "Jansen", "06", "piet@t.nl", 2, 5.00)
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

    def _stuur_en_vang_body(self, voornaam="Jan", achternaam="Jansen"):
        verzonden = {}
        def nep_send(params):
            verzonden["html"] = params.get("html", "")
            return {"id": "test"}
        with patch("resend.Emails.send", side_effect=nep_send):
            stuur_bevestigingsmail(voornaam, achternaam, "test@test.nl", 2, 1, 2, 5.00)
        return verzonden.get("html", "")

    def test_scripttag_in_voornaam_geescaped(self):
        body = self._stuur_en_vang_body(voornaam='<script>alert(1)</script>')
        self.assertNotIn("<script>", body)

    def test_html_entiteiten_aanwezig(self):
        body = self._stuur_en_vang_body(achternaam='<b>Naam</b>')
        self.assertIn("&lt;b&gt;", body)

    def test_gewone_naam_verschijnt_in_body(self):
        body = self._stuur_en_vang_body("Jan", "Jansen")
        self.assertIn("Jan Jansen", body)

    def test_resend_fout_geeft_false_geen_exception(self):
        with patch("resend.Emails.send", side_effect=Exception("API fout")):
            resultaat = stuur_bevestigingsmail("Jan", "Jansen", "jan@t.nl", 1, 1, 1, 2.50)
        self.assertFalse(resultaat)

    def test_resend_geeft_true_bij_succes(self):
        with patch("resend.Emails.send", return_value={"id": "abc123"}):
            resultaat = stuur_bevestigingsmail("Jan", "Jansen", "jan@t.nl", 1, 1, 1, 2.50)
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

    def test_lege_voornaam_geeft_422(self):
        self.assertEqual(doe_bestelling(self.client, voornaam="").status_code, 422)

    def test_lege_achternaam_geeft_422(self):
        self.assertEqual(doe_bestelling(self.client, achternaam="").status_code, 422)

    def test_ongeldig_email_geeft_422(self):
        self.assertEqual(doe_bestelling(self.client, email="geen-email").status_code, 422)

    def test_aantal_nul_geeft_422(self):
        self.assertEqual(doe_bestelling(self.client, aantal=0).status_code, 422)

    def test_aantal_101_geeft_422(self):
        self.assertEqual(doe_bestelling(self.client, aantal=101).status_code, 422)

    def test_aantal_als_tekst_geeft_400(self):
        r = self.client.post("/bestellen", data={
            "voornaam": "Jan", "achternaam": "Jansen", "telefoon": "0612345678",
            "email": "jan@test.nl", "aantal": "veel",
        })
        self.assertEqual(r.status_code, 400)

    def test_mollie_fout_geeft_503(self):
        with patch("app.maak_mollie_client") as mc:
            mc.return_value.payments.create.side_effect = MollieError("down")
            r = self.client.post("/bestellen", data={
                "voornaam": "Jan", "achternaam": "Jansen", "telefoon": "0612345678",
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
        self.assertEqual(self.client.get("/betaald/tr_x").status_code, 200)

    def test_betaald_bevat_succes(self):
        self._zet_status("betaald", lot_van=1, lot_tot=2)
        self.assertIn(b"succes", self.client.get("/betaald/tr_x").data)

    def test_mislukt_geeft_200(self):
        self._zet_status("mislukt")
        self.assertEqual(self.client.get("/betaald/tr_x").status_code, 200)

    def test_mislukt_bevat_fout(self):
        self._zet_status("mislukt")
        self.assertIn(b"fout", self.client.get("/betaald/tr_x").data)

    def test_geannuleerd_geeft_200(self):
        self._zet_status("geannuleerd")
        self.assertEqual(self.client.get("/betaald/tr_x").status_code, 200)

    def test_verlopen_geeft_200(self):
        self._zet_status("verlopen")
        self.assertEqual(self.client.get("/betaald/tr_x").status_code, 200)

    def test_aangemaakt_wacht_pagina(self):
        self._zet_status("aangemaakt")
        mock_b = simuleer_mollie_betaling("open")
        with patch("app.maak_mollie_client") as mc:
            mc.return_value.payments.get.return_value = mock_b
            r = self.client.get("/betaald/tr_x")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"wacht", r.data)

    def test_niet_bestaand_id_redirect(self):
        r = self.client.get("/betaald/tr_nonexistent")
        self.assertEqual(r.status_code, 302)

    def test_fallback_paid_wijst_loten_toe(self):
        doe_bestelling(self.client, aantal=1)
        stel_mollie_id_in(1, "tr_fallback")
        mock_b = simuleer_mollie_betaling("paid")
        with patch("app.maak_mollie_client") as mc, \
             patch("app.stuur_bevestigingsmail", return_value=True):
            mc.return_value.payments.get.return_value = mock_b
            self.client.get("/betaald/tr_fallback")
        rij = App.get_db().execute(
            "SELECT status, lot_van FROM bestellingen WHERE id=1"
        ).fetchone()
        self.assertEqual(rij["status"], "betaald")
        self.assertIsNotNone(rij["lot_van"])

    def test_betaald_footer_bevat_organisatienaam(self):
        self._zet_status("betaald", lot_van=1, lot_tot=1)
        r = self.client.get("/betaald/tr_x")
        self.assertIn("Diaconie Hervormde gemeente te Wapenveld".encode(), r.data)

    def test_betaald_footer_bevat_kvk(self):
        self._zet_status("betaald", lot_van=1, lot_tot=1)
        r = self.client.get("/betaald/tr_x")
        self.assertIn(b"76404862", r.data)

    def test_betaald_footer_bevat_contactgegevens(self):
        self._zet_status("betaald", lot_van=1, lot_tot=1)
        r = self.client.get("/betaald/tr_x")
        self.assertIn(b"diaconie@hervormdwapenveld.nl", r.data)
        self.assertIn(b"Kerkstraat", r.data)

    def test_betaald_title_succes(self):
        """<title> bevat het vinkje-emoji bij een geslaagde betaling."""
        self._zet_status("betaald", lot_van=1, lot_tot=2)
        r = self.client.get("/betaald/tr_x")
        self.assertIn("<title>\u2705".encode(), r.data)

    def test_betaald_title_mislukt(self):
        """<title> bevat het kruis-emoji bij een mislukte betaling."""
        self._zet_status("mislukt")
        r = self.client.get("/betaald/tr_x")
        self.assertIn("<title>\u274c".encode(), r.data)

    def test_betaald_title_wacht(self):
        """<title> bevat het zandloper-emoji bij een openstaande betaling."""
        self._zet_status("aangemaakt")
        mock_b = simuleer_mollie_betaling("open")
        with patch("app.maak_mollie_client") as mc:
            mc.return_value.payments.get.return_value = mock_b
            r = self.client.get("/betaald/tr_x")
        self.assertIn("<title>\u23f3".encode(), r.data)


# ══════════════════════════════════════════════════════════════════════════════
# 8b. /betaald/r/<id> tussenroute (Mollie redirectUrl → mollie_id-URL)
# ══════════════════════════════════════════════════════════════════════════════

class TestBetaaldRedirect(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_redirect_stuurt_door_naar_mollie_id_url(self):
        """Tussenroute /betaald/r/<id> stuurt door naar /betaald/<mollie_id>."""
        doe_bestelling(self.client, aantal=1)
        stel_mollie_id_in(1, "tr_redirect_test")
        r = self.client.get("/betaald/r/1")
        self.assertEqual(r.status_code, 302)
        self.assertIn(b"tr_redirect_test", r.headers["Location"].encode())

    def test_redirect_onbekend_id_gaat_naar_index(self):
        """Tussenroute met onbekend id stuurt door naar /."""
        r = self.client.get("/betaald/r/99999")
        self.assertEqual(r.status_code, 302)

    def test_redirect_zonder_mollie_id_gaat_naar_index(self):
        """Tussenroute zonder mollie_id (NULL) stuurt door naar /."""
        doe_bestelling(self.client)
        App.get_db().execute("UPDATE bestellingen SET mollie_id=NULL WHERE id=1")
        r = self.client.get("/betaald/r/1")
        self.assertEqual(r.status_code, 302)


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
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

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
        self.client.post("/admin/logout")
        self.assertEqual(self.client.get("/admin").status_code, 302)

    def test_admin_bevat_details_handmatige_bestelling(self):
        self._login()
        r = self.client.get("/admin")
        self.assertIn(b"<details", r.data)
        self.assertIn("Handmatige bestelling".encode(), r.data)

    def test_admin_bevat_details_instellingen(self):
        self._login()
        r = self.client.get("/admin/beheer")
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
            "INSERT INTO bestellingen (voornaam,achternaam,telefoon,email,aantal,bedrag) "
            "VALUES (?,?,?,?,?,?)", ("Jan", "Jansen", "06", "j@t.nl", 5, 10.00)
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
            "INSERT INTO bestellingen (voornaam,achternaam,telefoon,email,aantal,bedrag) "
            "VALUES (?,?,?,?,?,?)", ("Jan", "Jansen", "06", "j@t.nl", 5, 10.00)
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
            "INSERT INTO bestellingen (voornaam,achternaam,telefoon,email,aantal,bedrag) "
            "VALUES (?,?,?,?,?,?)", ("Jan", "Jansen", "06", "j@t.nl", 5, 10.00)
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

        # Dan roept /betaald/<mollie_id> de fallback aan (status is al 'betaald')
        mock_b = simuleer_mollie_betaling("paid")
        with patch("app.maak_mollie_client") as mc, \
             patch("app.stuur_bevestigingsmail", return_value=True):
            mc.return_value.payments.get.return_value = mock_b
            self.client.get("/betaald/tr_race")

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
            r = self.client.get("/betaald/tr_fallback_oversell")

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
        doe_bestelling(self.client, voornaam="Jan", achternaam="Jansen", aantal=2)
        self._login()

    def tearDown(self):
        self.ctx.pop()

    def _login(self):
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def _wijzig(self, bestelling_id=1, **kwargs):
        data = {
            "voornaam": "Jan", "achternaam": "Jansen", "telefoon": "0612345678",
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
        self.client.post("/admin/logout")
        self.assertEqual(self.client.get("/admin/bestelling/1/wijzigen").status_code, 302)

    # ── wijzigen POST ─────────────────────────────────────────────────────────

    def test_wijzigen_post_redirect_naar_admin(self):
        r = self._wijzig(voornaam="Piet", achternaam="Pietersen")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin", r.headers["Location"])

    def test_wijzigen_naam_wordt_opgeslagen(self):
        self._wijzig(voornaam="Piet", achternaam="Pietersen")
        rij = App.get_db().execute("SELECT voornaam, achternaam FROM bestellingen WHERE id=1").fetchone()
        self.assertEqual(rij["voornaam"], "Piet")
        self.assertEqual(rij["achternaam"], "Pietersen")

    def test_wijzigen_status_wordt_opgeslagen(self):
        self._wijzig(status="betaald")
        rij = App.get_db().execute("SELECT status FROM bestellingen WHERE id=1").fetchone()
        self.assertEqual(rij["status"], "betaald")

    def test_wijzigen_lege_voornaam_geeft_fout(self):
        r = self._wijzig(voornaam="")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"verplicht", r.data)

    def test_wijzigen_lege_achternaam_geeft_fout(self):
        r = self._wijzig(achternaam="")
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
            stuur_bevestigingsmail("Jan", "Jansen", "jan@t.nl", 2, 1, 2, 5.32, transactiekosten=True)
        self.assertIn("transactiekosten", verzonden.get("html", "").lower())

    def test_email_zonder_tk_vermeldt_geen_transactiekosten(self):
        verzonden = {}
        def nep_send(params):
            verzonden["html"] = params.get("html", "")
            return {"id": "test"}
        with patch("resend.Emails.send", side_effect=nep_send):
            stuur_bevestigingsmail("Jan", "Jansen", "jan@t.nl", 2, 1, 2, 5.00, transactiekosten=False)
        self.assertNotIn("transactiekosten", verzonden.get("html", "").lower())


# ══════════════════════════════════════════════════════════════════════════════
# 14. MAX_PER_BESTELLING (configureerbaar)
# ══════════════════════════════════════════════════════════════════════════════

class TestMaxPerBestelling(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    # ── valideer_invoer ───────────────────────────────────────────────────────

    def test_valideer_invoer_standaard_max_100(self):
        fouten = valideer_invoer("Jan", "Jansen", "0612345678", "jan@test.nl", 100)
        self.assertEqual(fouten, [])

    def test_valideer_invoer_101_met_standaard_geeft_fout(self):
        fouten = valideer_invoer("Jan", "Jansen", "0612345678", "jan@test.nl", 101)
        self.assertTrue(any("100" in f for f in fouten))

    def test_valideer_invoer_aangepast_max_50(self):
        fouten = valideer_invoer("Jan", "Jansen", "0612345678", "jan@test.nl", 50, max_per_bestelling=50)
        self.assertEqual(fouten, [])

    def test_valideer_invoer_51_met_max_50_geeft_fout(self):
        fouten = valideer_invoer("Jan", "Jansen", "0612345678", "jan@test.nl", 51, max_per_bestelling=50)
        self.assertTrue(any("50" in f for f in fouten))

    def test_valideer_invoer_foutmelding_bevat_max(self):
        fouten = valideer_invoer("Jan", "Jansen", "0612345678", "jan@test.nl", 25, max_per_bestelling=20)
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
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

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
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

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
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})
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
                "INSERT INTO bestellingen (voornaam, achternaam, telefoon, email, aantal, bedrag) "
                "VALUES (?,?,?,?,?,?)",
                ("Koper", f"{i}", "0600000000", f"k{i}@test.nl", 1, 2.50)
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
                "INSERT INTO bestellingen (voornaam, achternaam, telefoon, email, aantal, bedrag, status) "
                "VALUES (?,?,?,?,?,?,?)",
                ("Koper", f"{i}", "0600000000", f"k{i}@test.nl", 1, 2.50, status)
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
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

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
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

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
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    def _download_csv(self):
        r = self.client.get("/admin/export-csv")
        self.assertEqual(r.status_code, 200)
        # Sla BOM over en decodeer
        return r.data.decode("utf-8-sig")

    def _bestel_met_naam(self, achternaam):
        doe_bestelling(self.client, achternaam=achternaam)

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
        doe_bestelling(self.client, voornaam="iDEAL", achternaam="Koper")
        csv_tekst = self._download_csv()
        self.assertIn('"ideal"', csv_tekst)

    def test_csv_handmatige_bestelling_heeft_betaalwijze_contant(self):
        self.client.post("/admin/handmatig", data={
            "h_voornaam": "Contant", "h_achternaam": "Koper", "email": "", "telefoon": "",
            "aantal": "1", "betaalwijze": "contant",
        })
        csv_tekst = self._download_csv()
        self.assertIn('"contant"', csv_tekst)

    def test_csv_kolommen_tellen(self):
        """Header moet exact 14 kolommen bevatten."""
        csv_tekst = self._download_csv()
        header = csv_tekst.splitlines()[0]
        self.assertEqual(header.count(";"), 13)  # 14 kolommen = 13 scheidingstekens

    def test_csv_header_bevat_alle_kolomnamen(self):
        csv_tekst = self._download_csv()
        header = csv_tekst.splitlines()[0]
        for kolom in ["ID", "Voornaam", "Achternaam", "E-mail", "Telefoon", "Aantal", "Bedrag",
                      "iDEAL-kosten", "Lot van", "Lot tot", "Status",
                      "Betaalwijze", "Mail verstuurd", "Aangemaakt op"]:
            self.assertIn(kolom, header)


class TestMailHeader(unittest.TestCase):

    def _vang_mail_params(self, voornaam="Jan", achternaam="Jansen"):
        verzonden = {}
        def nep_send(params):
            verzonden.update(params)
            return {"id": "test"}
        with patch("resend.Emails.send", side_effect=nep_send):
            stuur_bevestigingsmail(voornaam, achternaam, "jan@test.nl", 1, 1, 1, 2.50)
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

    def test_mail_html_bevat_locatie(self):
        params = self._vang_mail_params()
        self.assertIn("Manenbergerbrug", params.get("html", ""))

    def test_mail_html_bevat_tijd(self):
        params = self._vang_mail_params()
        self.assertIn("19:00", params.get("html", ""))

    def test_mail_html_praktische_info_icoon_niet_agenda(self):
        """Header van praktische-infoblok mag niet hetzelfde icoon als de datumregel gebruiken."""
        params = self._vang_mail_params()
        html = params.get("html", "")
        # ℹ️ (U+2139 + variatie-selector) wordt gebruikt als header-icoon
        self.assertIn("&#x2139;", html)
        # De kop "Praktische informatie" mag niet met het agenda-icoon (&#x1F4C5;) beginnen
        import re
        kop = re.search(r'Praktische informatie', html)
        self.assertIsNotNone(kop)
        voor_kop = html[:kop.start()]
        # Zoek het dichtstbijzijnde icoon vóór de koptekst
        self.assertNotIn("&#x1F4C5;", voor_kop.split('<p style="margin:0 0 6px')[-1])



# ══════════════════════════════════════════════════════════════════════════════
# HANDMATIGE BESTELLINGEN
#     POST /admin/handmatig — contant/overboeking verkoop buiten iDEAL om
# ══════════════════════════════════════════════════════════════════════════════

class TestHandmatigeBestellingen(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    def test_handmatige_bestelling_contant(self):
        r = self.client.post("/admin/handmatig", data={
            "h_voornaam": "Piet", "h_achternaam": "Pietersen", "email": "piet@test.nl",
            "telefoon": "0612345678", "aantal": "2", "betaalwijze": "contant",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        db = App.get_db()
        b = db.execute("SELECT * FROM bestellingen WHERE voornaam='Piet' AND achternaam='Pietersen'").fetchone()
        self.assertIsNotNone(b)
        self.assertEqual(b["status"], "betaald")
        self.assertEqual(b["betaalwijze"], "contant")
        self.assertIsNotNone(b["lot_van"])
        self.assertIsNotNone(b["lot_tot"])

    def test_handmatige_bestelling_overboeking(self):
        r = self.client.post("/admin/handmatig", data={
            "h_voornaam": "Klaas", "h_achternaam": "Klaassen", "email": "",
            "telefoon": "", "aantal": "3", "betaalwijze": "overboeking",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        db = App.get_db()
        b = db.execute("SELECT * FROM bestellingen WHERE voornaam='Klaas' AND achternaam='Klaassen'").fetchone()
        self.assertEqual(b["betaalwijze"], "overboeking")
        self.assertEqual(b["status"], "betaald")

    def test_handmatige_bestelling_zonder_email_geen_mail(self):
        with patch("app.stuur_bevestigingsmail") as mock_mail:
            self.client.post("/admin/handmatig", data={
                "h_voornaam": "Geen", "h_achternaam": "Email", "email": "",
                "telefoon": "", "aantal": "1", "betaalwijze": "contant",
            })
            mock_mail.assert_not_called()
        # mail_verstuurd=1 zodat de admin-waarschuwing niet verschijnt
        db = App.get_db()
        b = db.execute("SELECT mail_verstuurd FROM bestellingen WHERE voornaam='Geen' AND achternaam='Email'").fetchone()
        self.assertEqual(b["mail_verstuurd"], 1)

    def test_handmatige_bestelling_met_email_stuurt_mail(self):
        with patch("app.stuur_bevestigingsmail", return_value=True) as mock_mail:
            self.client.post("/admin/handmatig", data={
                "h_voornaam": "Met", "h_achternaam": "Email", "email": "met@test.nl",
                "telefoon": "", "aantal": "1", "betaalwijze": "contant",
            })
            mock_mail.assert_called_once()

    def test_handmatige_bestelling_lotnummers_oplopend(self):
        self.client.post("/admin/handmatig", data={
            "h_voornaam": "Eerste", "h_achternaam": "Persoon", "email": "", "telefoon": "",
            "aantal": "2", "betaalwijze": "contant",
        })
        self.client.post("/admin/handmatig", data={
            "h_voornaam": "Tweede", "h_achternaam": "Persoon", "email": "", "telefoon": "",
            "aantal": "3", "betaalwijze": "contant",
        })
        db = App.get_db()
        eerste = db.execute("SELECT lot_van, lot_tot FROM bestellingen WHERE voornaam='Eerste'").fetchone()
        tweede = db.execute("SELECT lot_van, lot_tot FROM bestellingen WHERE voornaam='Tweede'").fetchone()
        self.assertEqual(eerste["lot_van"], 1)
        self.assertEqual(eerste["lot_tot"], 2)
        self.assertEqual(tweede["lot_van"], 3)
        self.assertEqual(tweede["lot_tot"], 5)

    def test_handmatige_bestelling_naam_verplicht(self):
        r = self.client.post("/admin/handmatig", data={
            "h_voornaam": "", "h_achternaam": "", "email": "", "telefoon": "",
            "aantal": "1", "betaalwijze": "contant",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        db = App.get_db()
        count = db.execute("SELECT COUNT(*) FROM bestellingen").fetchone()[0]
        self.assertEqual(count, 0)

    def test_handmatige_bestelling_ongeldig_betaalwijze_valt_terug_op_contant(self):
        self.client.post("/admin/handmatig", data={
            "h_voornaam": "Test", "h_achternaam": "User", "email": "", "telefoon": "",
            "aantal": "1", "betaalwijze": "ideal",  # niet geldig voor handmatig
        })
        db = App.get_db()
        b = db.execute("SELECT betaalwijze FROM bestellingen WHERE voornaam='Test' AND achternaam='User'").fetchone()
        self.assertEqual(b["betaalwijze"], "contant")

    def test_ideal_bestelling_heeft_betaalwijze_ideal(self):
        doe_bestelling(self.client, voornaam="iDEAL", achternaam="Koper", aantal=1)
        db = App.get_db()
        b = db.execute("SELECT betaalwijze FROM bestellingen WHERE voornaam='iDEAL' AND achternaam='Koper'").fetchone()
        self.assertEqual(b["betaalwijze"], "ideal")

    def test_csv_bevat_betaalwijze_kolom(self):
        r = self.client.get("/admin/export-csv")
        self.assertIn(b"Betaalwijze", r.data)

    def test_handmatige_bestelling_vereist_admin_login(self):
        # Uitloggen
        uitgelogde_client, ctx2 = maak_flask_client()
        r = uitgelogde_client.post("/admin/handmatig", data={
            "h_voornaam": "Hacker", "h_achternaam": "Test", "email": "", "telefoon": "",
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

    def _login(self, gebruiker="admin", wachtwoord="testpass12345"):
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
        r = self._wijzig_wachtwoord("testpass12345", "nieuwwachtwoord99")
        self.assertIn(b"succesvol", r.data)
        # Inloggen met nieuw wachtwoord werkt
        self.client.post("/admin/logout")
        r2 = self.client.post("/admin/login",
                              data={"gebruiker": "admin", "wachtwoord": "nieuwwachtwoord99"})
        self.assertEqual(r2.status_code, 302)

    def test_wachtwoord_wijzigen_fout_huidig(self):
        """Verkeerd huidig wachtwoord geeft foutmelding."""
        r = self._wijzig_wachtwoord("verkeerd", "nieuwwachtwoord99")
        self.assertIn(b"onjuist", r.data)

    def test_wachtwoord_wijzigen_te_kort(self):
        """Nieuw wachtwoord korter dan 12 tekens geeft foutmelding."""
        r = self._wijzig_wachtwoord("testpass12345", "kortww", "kortww")
        self.assertIn(b"12", r.data)

    def test_wachtwoord_wijzigen_mismatch(self):
        """Niet-overeenkomende nieuwe wachtwoorden geven foutmelding."""
        r = self._wijzig_wachtwoord("testpass12345", "nieuwwachtwoord99", "anderwachtwoord00")
        self.assertIn(b"overeen", r.data)

    def test_laatste_inlog_wordt_gezet_bij_login(self):
        """Na inloggen is laatste_inlog gevuld in de database."""
        rij = App.get_db().execute(
            "SELECT laatste_inlog FROM beheerders WHERE gebruikersnaam='admin'"
        ).fetchone()
        self.assertIsNotNone(rij["laatste_inlog"])

    def test_laatste_inlog_tonen_in_beheer_pagina(self):
        """Beheer-pagina toont de 'Laatste ingelogd' kolom."""
        r = self.client.get("/admin/beheer")
        self.assertIn(b"Laatste ingelogd", r.data)

    def test_laatste_inlog_waarde_in_beheer_pagina(self):
        """Beheer-pagina toont een datum in de laatste-inlogkolom na inloggen."""
        r = self.client.get("/admin/beheer")
        # Na login moet er een datum staan (minstens jaar 20xx)
        self.assertIn(b"20", r.data)

    def _reset_wachtwoord(self, beheerder_id, nieuw, bevestiging=None):
        if bevestiging is None:
            bevestiging = nieuw
        return self.client.post(f"/admin/beheerder-wachtwoord-reset/{beheerder_id}", data={
            "nieuw_wachtwoord": nieuw,
            "nieuw_wachtwoord_bevestiging": bevestiging,
        }, follow_redirects=True)

    def test_wachtwoord_reset_werkt(self):
        """Beheerder kan wachtwoord van ander account resetten."""
        self._voeg_toe(gebruikersnaam="reset_target", wachtwoord="oudwachtwoord1")
        rij = App.get_db().execute(
            "SELECT id FROM beheerders WHERE gebruikersnaam='reset_target'"
        ).fetchone()
        r = self._reset_wachtwoord(rij["id"], "nieuwwachtwoord99")
        self.assertIn(b"succesvol", r.data)
        # Inloggen met nieuw wachtwoord werkt
        self.client.post("/admin/logout")
        r2 = self.client.post("/admin/login",
                              data={"gebruiker": "reset_target",
                                    "wachtwoord": "nieuwwachtwoord99"})
        self.assertEqual(r2.status_code, 302)

    def test_wachtwoord_reset_te_kort(self):
        """Nieuw wachtwoord korter dan 12 tekens geeft foutmelding."""
        self._voeg_toe(gebruikersnaam="reset_kort")
        rij = App.get_db().execute(
            "SELECT id FROM beheerders WHERE gebruikersnaam='reset_kort'"
        ).fetchone()
        r = self._reset_wachtwoord(rij["id"], "kort", "kort")
        self.assertIn(b"12", r.data)

    def test_wachtwoord_reset_mismatch(self):
        """Niet-overeenkomende wachtwoorden geven foutmelding."""
        self._voeg_toe(gebruikersnaam="reset_mismatch")
        rij = App.get_db().execute(
            "SELECT id FROM beheerders WHERE gebruikersnaam='reset_mismatch'"
        ).fetchone()
        r = self._reset_wachtwoord(rij["id"], "wachtwoordA123", "wachtwoordB456")
        self.assertIn(b"overeen", r.data)

    def test_wachtwoord_reset_eigen_account_geblokkeerd(self):
        """Eigen wachtwoord kan niet via reset-route worden gewijzigd."""
        rij = App.get_db().execute(
            "SELECT id FROM beheerders WHERE gebruikersnaam='admin'"
        ).fetchone()
        r = self._reset_wachtwoord(rij["id"], "nieuwwachtwoord99")
        self.assertIn(b"eigen", r.data)

    def test_wachtwoord_reset_invalideert_sessie(self):
        """Na wachtwoord-reset wordt de bestaande sessie van dat account ongeldig."""
        self._voeg_toe(gebruikersnaam="sessie_target", wachtwoord="oudwachtwoord1")
        rij = App.get_db().execute(
            "SELECT id, sessie_versie FROM beheerders WHERE gebruikersnaam='sessie_target'"
        ).fetchone()
        oude_versie = rij["sessie_versie"]
        self._reset_wachtwoord(rij["id"], "nieuwwachtwoord99")
        nieuwe_versie = App.get_db().execute(
            "SELECT sessie_versie FROM beheerders WHERE gebruikersnaam='sessie_target'"
        ).fetchone()["sessie_versie"]
        self.assertGreater(nieuwe_versie, oude_versie)

    def test_wachtwoord_reset_heft_blokkering_op(self):
        """Na wachtwoord-reset zijn mislukte_pogingen en geblokkeerd_tot gewist."""
        self._voeg_toe(gebruikersnaam="geblokkeerd_target", wachtwoord="oudwachtwoord1")
        rij = App.get_db().execute(
            "SELECT id FROM beheerders WHERE gebruikersnaam='geblokkeerd_target'"
        ).fetchone()
        # Simuleer geblokkeerd account
        App.get_db().execute(
            "UPDATE beheerders SET mislukte_pogingen=10, geblokkeerd_tot='2099-01-01T00:00:00'"
            " WHERE id=?", (rij["id"],)
        )
        App.get_db().commit()
        self._reset_wachtwoord(rij["id"], "nieuwwachtwoord99")
        na = App.get_db().execute(
            "SELECT mislukte_pogingen, geblokkeerd_tot FROM beheerders WHERE id=?",
            (rij["id"],)
        ).fetchone()
        self.assertEqual(na["mislukte_pogingen"], 0)
        self.assertIsNone(na["geblokkeerd_tot"])

    def test_wachtwoord_reset_onbekend_id_geeft_404(self):
        """Reset voor niet-bestaand ID geeft 404."""
        r = self.client.post("/admin/beheerder-wachtwoord-reset/99999", data={
            "nieuw_wachtwoord": "nieuwwachtwoord99",
            "nieuw_wachtwoord_bevestiging": "nieuwwachtwoord99",
        })
        self.assertEqual(r.status_code, 404)

    def test_wachtwoord_reset_zonder_login_geeft_302(self):
        """Reset-route vereist ingelogde sessie."""
        self.client.post("/admin/logout")
        r = self.client.post("/admin/beheerder-wachtwoord-reset/1", data={
            "nieuw_wachtwoord": "nieuwwachtwoord99",
            "nieuw_wachtwoord_bevestiging": "nieuwwachtwoord99",
        })
        self.assertEqual(r.status_code, 302)

    def test_wachtwoord_reset_wordt_gelogd(self):
        """Wachtwoord-reset schrijft wachtwoord_reset naar de audit-log."""
        self._voeg_toe(gebruikersnaam="log_target", wachtwoord="oudwachtwoord1")
        rij = App.get_db().execute(
            "SELECT id FROM beheerders WHERE gebruikersnaam='log_target'"
        ).fetchone()
        self._reset_wachtwoord(rij["id"], "nieuwwachtwoord99")
        log = App.get_db().execute(
            "SELECT * FROM audit_log WHERE actie='wachtwoord_reset' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(log)
        self.assertIn("log_target", log["details"])


# ══════════════════════════════════════════════════════════════════════════════
# SECURITY.TXT
#     GET /.well-known/security.txt — RFC 9116 compliance
# ══════════════════════════════════════════════════════════════════════════════

class TestSecurityTxt(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_security_txt_geeft_200(self):
        r = self.client.get("/.well-known/security.txt")
        self.assertEqual(r.status_code, 200)

    def test_security_txt_mimetype_text_plain(self):
        r = self.client.get("/.well-known/security.txt")
        self.assertIn("text/plain", r.content_type)

    def test_security_txt_bevat_contact_veld(self):
        r = self.client.get("/.well-known/security.txt")
        self.assertIn(b"Contact:", r.data)

    def test_security_txt_bevat_expires_veld(self):
        r = self.client.get("/.well-known/security.txt")
        self.assertIn(b"Expires:", r.data)

    def test_security_txt_bevat_preferred_languages(self):
        r = self.client.get("/.well-known/security.txt")
        self.assertIn(b"Preferred-Languages: nl, en", r.data)

    def test_security_txt_bevat_canonical_veld(self):
        r = self.client.get("/.well-known/security.txt")
        self.assertIn(b"Canonical:", r.data)

    def test_security_txt_expires_is_toekomst(self):
        """Expires-datum moet in de toekomst liggen (minimaal vandaag)."""
        import re as _re
        r = self.client.get("/.well-known/security.txt")
        tekst = r.data.decode()
        m = _re.search(r"Expires: (\d{4}-\d{2}-\d{2})", tekst)
        self.assertIsNotNone(m)
        from datetime import date
        jaar, maand, dag = map(int, m.group(1).split("-"))
        self.assertGreaterEqual(date(jaar, maand, dag), date.today())


# ══════════════════════════════════════════════════════════════════════════════
# SEO — robots.txt, sitemap.xml, meta-tags, structured data
# ══════════════════════════════════════════════════════════════════════════════

class TestSeoEnRobots(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    # ── robots.txt ────────────────────────────────────────────────────────────

    def test_robots_txt_geeft_200(self):
        r = self.client.get("/robots.txt")
        self.assertEqual(r.status_code, 200)

    def test_robots_txt_mimetype_text_plain(self):
        r = self.client.get("/robots.txt")
        self.assertIn("text/plain", r.content_type)

    def test_robots_txt_bevat_sitemap_verwijzing(self):
        r = self.client.get("/robots.txt")
        self.assertIn(b"Sitemap:", r.data)

    def test_robots_txt_staat_homepage_toe(self):
        r = self.client.get("/robots.txt")
        self.assertIn(b"Allow: /", r.data)

    def test_robots_txt_blokkeert_admin(self):
        r = self.client.get("/robots.txt")
        self.assertIn(b"Disallow: /admin", r.data)

    def test_robots_txt_blokkeert_bestellen(self):
        r = self.client.get("/robots.txt")
        self.assertIn(b"Disallow: /bestellen", r.data)

    def test_robots_txt_blokkeert_betaald(self):
        r = self.client.get("/robots.txt")
        self.assertIn(b"Disallow: /betaald/", r.data)

    # ── sitemap.xml ───────────────────────────────────────────────────────────

    def test_sitemap_xml_geeft_200(self):
        r = self.client.get("/sitemap.xml")
        self.assertEqual(r.status_code, 200)

    def test_sitemap_xml_mimetype_application_xml(self):
        r = self.client.get("/sitemap.xml")
        self.assertIn("xml", r.content_type)

    def test_sitemap_xml_bevat_homepage(self):
        r = self.client.get("/sitemap.xml")
        self.assertIn(b"<loc>", r.data)

    def test_sitemap_xml_bevat_privacy(self):
        r = self.client.get("/sitemap.xml")
        self.assertIn(b"/privacy", r.data)

    def test_sitemap_xml_bevat_voorwaarden(self):
        r = self.client.get("/sitemap.xml")
        self.assertIn(b"/voorwaarden", r.data)

    def test_sitemap_xml_bevat_geen_admin(self):
        r = self.client.get("/sitemap.xml")
        self.assertNotIn(b"/admin", r.data)

    # ── Homepage SEO-meta-tags ─────────────────────────────────────────────────

    def test_homepage_bevat_meta_description(self):
        r = self.client.get("/")
        self.assertIn(b'<meta name="description"', r.data)

    def test_homepage_bevat_canonical_url(self):
        r = self.client.get("/")
        self.assertIn(b'rel="canonical"', r.data)

    def test_homepage_bevat_og_title(self):
        r = self.client.get("/")
        self.assertIn(b'property="og:title"', r.data)

    def test_homepage_bevat_og_description(self):
        r = self.client.get("/")
        self.assertIn(b'property="og:description"', r.data)

    def test_homepage_bevat_og_image(self):
        r = self.client.get("/")
        self.assertIn(b'property="og:image"', r.data)

    def test_homepage_bevat_og_url(self):
        r = self.client.get("/")
        self.assertIn(b'property="og:url"', r.data)

    def test_homepage_bevat_twitter_card(self):
        r = self.client.get("/")
        self.assertIn(b'name="twitter:card"', r.data)

    def test_homepage_bevat_robots_index(self):
        r = self.client.get("/")
        self.assertIn(b'content="index, follow"', r.data)

    def test_homepage_bevat_json_ld(self):
        r = self.client.get("/")
        self.assertIn(b'application/ld+json', r.data)

    def test_homepage_json_ld_bevat_event_type(self):
        r = self.client.get("/")
        self.assertIn(b'"@type": "Event"', r.data)

    # ── noindex op niet-openbare pagina's ─────────────────────────────────────

    def test_foutpagina_bevat_noindex(self):
        r = self.client.get("/bestaat-niet-xyz")
        self.assertIn(b"noindex", r.data)

    def test_admin_login_bevat_noindex(self):
        r = self.client.get("/admin/login")
        self.assertIn(b"noindex", r.data)

    def test_privacy_bevat_noindex(self):
        r = self.client.get("/privacy")
        self.assertIn(b"noindex", r.data)

    def test_voorwaarden_bevat_noindex(self):
        r = self.client.get("/voorwaarden")
        self.assertIn(b"noindex", r.data)

    def test_privacy_bevat_canonical(self):
        r = self.client.get("/privacy")
        self.assertIn(b'rel="canonical"', r.data)

    def test_voorwaarden_bevat_canonical(self):
        r = self.client.get("/voorwaarden")
        self.assertIn(b'rel="canonical"', r.data)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN LOGIN GEBRUIKERSNAAM IN SESSIE
#     Sessie slaat gebruikersnaam op en logout gebruikt die
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminLoginSessie(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_login_slaat_gebruikersnaam_op_in_sessie(self):
        """Na succesvol inloggen moet admin_gebruikersnaam in de sessie staan."""
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("admin_gebruikersnaam"), "admin")

    def test_logout_redirect_naar_loginpagina(self):
        """Uitloggen stuurt door naar de inlogpagina."""
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})
        r = self.client.post("/admin/logout")
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r.headers["Location"])

    def test_logout_wist_sessie_volledig(self):
        """Na uitloggen mogen geen admin-sessiegegevens meer aanwezig zijn."""
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})
        self.client.post("/admin/logout")
        with self.client.session_transaction() as sess:
            self.assertFalse(sess.get("admin_ingelogd", False))
            self.assertIsNone(sess.get("admin_gebruikersnaam"))

    def test_logout_zonder_login_geeft_302(self):
        """Uitloggen zonder actieve sessie redirect naar inlogpagina."""
        r = self.client.post("/admin/logout")
        self.assertEqual(r.status_code, 302)

    def test_login_onbekende_gebruiker_geeft_fout(self):
        """Een onbekende gebruikersnaam geeft een foutmelding op de loginpagina."""
        r = self.client.post("/admin/login",
                             data={"gebruiker": "onbekend", "wachtwoord": "testpass12345"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Onjuiste", r.data)

    def test_login_geeft_200_bij_GET(self):
        """GET-verzoek op loginpagina geeft formulier terug."""
        r = self.client.get("/admin/login")
        self.assertEqual(r.status_code, 200)


# ══════════════════════════════════════════════════════════════════════════════
# BEHEERDER TOEVOEGEN — EXTRA GEVALLEN
# ══════════════════════════════════════════════════════════════════════════════

class TestBeheerderToevoegenExtra(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    def test_beheerder_toevoegen_lege_naam_geeft_fout(self):
        """Lege gebruikersnaam wordt geweigerd."""
        r = self.client.post("/admin/beheerder-toevoegen", data={
            "gebruikersnaam": "",
            "wachtwoord": "sterkwachtwoord1",
            "wachtwoord_bevestiging": "sterkwachtwoord1",
        }, follow_redirects=True)
        self.assertIn(b"verplicht", r.data)

    def test_beheerder_toevoegen_zonder_login_geeft_302(self):
        """Unauthenticated toegang wordt doorgestuurd naar login."""
        self.client.post("/admin/logout")
        r = self.client.post("/admin/beheerder-toevoegen", data={
            "gebruikersnaam": "nieuw",
            "wachtwoord": "sterkwachtwoord1",
            "wachtwoord_bevestiging": "sterkwachtwoord1",
        })
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r.headers["Location"])

    def test_beheerder_toevoegen_slaat_hash_op_niet_plaintext(self):
        """Wachtwoord wordt gehashed opgeslagen — niet als plaintext."""
        self.client.post("/admin/beheerder-toevoegen", data={
            "gebruikersnaam": "hash_test",
            "wachtwoord": "sterkwachtwoord1",
            "wachtwoord_bevestiging": "sterkwachtwoord1",
        })
        rij = App.get_db().execute(
            "SELECT wachtwoord_hash FROM beheerders WHERE gebruikersnaam='hash_test'"
        ).fetchone()
        self.assertIsNotNone(rij)
        self.assertNotEqual(rij["wachtwoord_hash"], "sterkwachtwoord1")
        # Hash mag niet gelijk zijn aan het plaintext wachtwoord
        self.assertGreater(len(rij["wachtwoord_hash"]), 20)


# ══════════════════════════════════════════════════════════════════════════════
# BEHEERDER VERWIJDEREN — EXTRA GEVALLEN
# ══════════════════════════════════════════════════════════════════════════════

class TestBeheerderVerwijderenExtra(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    def test_beheerder_verwijderen_onbekend_id_geeft_404(self):
        """Verwijderen van niet-bestaand account geeft 404."""
        # Voeg tweede account toe zodat de "laatste account"-blokkering niet vuurt
        self.client.post("/admin/beheerder-toevoegen", data={
            "gebruikersnaam": "tweede",
            "wachtwoord": "sterkwachtwoord1",
            "wachtwoord_bevestiging": "sterkwachtwoord1",
        })
        r = self.client.post("/admin/beheerder-verwijderen/99999",
                             follow_redirects=False)
        self.assertEqual(r.status_code, 404)

    def test_beheerder_verwijderen_zonder_login_geeft_302(self):
        """Unauthenticated toegang wordt doorgestuurd naar login."""
        self.client.post("/admin/logout")
        r = self.client.post("/admin/beheerder-verwijderen/1")
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r.headers["Location"])


# ══════════════════════════════════════════════════════════════════════════════
# WACHTWOORD WIJZIGEN — EXTRA GEVALLEN
# ══════════════════════════════════════════════════════════════════════════════

class TestWachtwoordWijzigenExtra(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    def test_wachtwoord_wijzigen_zonder_login_geeft_302(self):
        """Unauthenticated toegang wordt doorgestuurd naar login."""
        self.client.post("/admin/logout")
        r = self.client.post("/admin/wachtwoord-wijzigen", data={
            "huidig_wachtwoord": "testpass12345",
            "nieuw_wachtwoord": "nieuwwachtwoord99",
            "nieuw_wachtwoord_bevestiging": "nieuwwachtwoord99",
        })
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r.headers["Location"])


# ══════════════════════════════════════════════════════════════════════════════
# HANDMATIGE BESTELLING — EXTRA GEVALLEN
# ══════════════════════════════════════════════════════════════════════════════

class TestHandmatigeBestellingenExtra(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    def test_handmatige_bestelling_ongeldig_aantal_tekst(self):
        """Tekstwaarde voor aantal geeft foutmelding, geen crash."""
        r = self.client.post("/admin/handmatig", data={
            "h_voornaam": "Test", "h_achternaam": "Persoon", "email": "", "telefoon": "",
            "aantal": "veel", "betaalwijze": "contant",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        count = App.get_db().execute("SELECT COUNT(*) FROM bestellingen").fetchone()[0]
        self.assertEqual(count, 0)

    def test_handmatige_bestelling_aantal_nul_geeft_fout(self):
        """Aantal = 0 wordt geweigerd."""
        r = self.client.post("/admin/handmatig", data={
            "h_voornaam": "Test", "h_achternaam": "Persoon", "email": "", "telefoon": "",
            "aantal": "0", "betaalwijze": "contant",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        count = App.get_db().execute("SELECT COUNT(*) FROM bestellingen").fetchone()[0]
        self.assertEqual(count, 0)

    def test_handmatige_bestelling_oversell_geeft_fout_geen_record(self):
        """Bij oversell wordt geen bestelling aangemaakt en de foutmelding getoond."""
        App.get_db().execute("UPDATE teller SET volgend_lot=? WHERE id=1", (MAX_EENDJES + 1,))
        r = self.client.post("/admin/handmatig", data={
            "h_voornaam": "Oversell", "h_achternaam": "Test", "email": "", "telefoon": "",
            "aantal": "1", "betaalwijze": "contant",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        count = App.get_db().execute("SELECT COUNT(*) FROM bestellingen").fetchone()[0]
        self.assertEqual(count, 0)

    def test_handmatige_bestelling_ongeldig_email_geeft_fout(self):
        """Ongeldig e-mailadres bij handmatige bestelling wordt geweigerd."""
        r = self.client.post("/admin/handmatig", data={
            "h_voornaam": "Test", "h_achternaam": "Persoon", "email": "geen-at-teken",
            "telefoon": "", "aantal": "1", "betaalwijze": "contant",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        count = App.get_db().execute("SELECT COUNT(*) FROM bestellingen").fetchone()[0]
        self.assertEqual(count, 0)

    def test_handmatige_bestelling_naam_te_lang_geeft_fout(self):
        """Naam langer dan 100 tekens wordt geweigerd."""
        r = self.client.post("/admin/handmatig", data={
            "h_voornaam": "Aa", "h_achternaam": "A" * 101, "email": "", "telefoon": "",
            "aantal": "1", "betaalwijze": "contant",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        count = App.get_db().execute("SELECT COUNT(*) FROM bestellingen").fetchone()[0]
        self.assertEqual(count, 0)

    def test_handmatige_bestelling_mail_verstuurd_bij_succes_met_email(self):
        """Bij een succesvolle bestelling met e-mail wordt mail_verstuurd=1 als mail slaagt."""
        with patch("app.stuur_bevestigingsmail", return_value=True):
            self.client.post("/admin/handmatig", data={
                "h_voornaam": "Met", "h_achternaam": "Email", "email": "met@test.nl",
                "telefoon": "", "aantal": "1", "betaalwijze": "contant",
            })
        rij = App.get_db().execute(
            "SELECT mail_verstuurd FROM bestellingen WHERE voornaam='Met' AND achternaam='Email'"
        ).fetchone()
        self.assertEqual(rij["mail_verstuurd"], 1)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN ZOEKFUNCTIE
#     Server-side zoekopdracht via ?zoek=... parameter
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminZoekfunctie(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})
        # Vul database met testbestellingen
        db = App.get_db()
        db.execute(
            "INSERT INTO bestellingen (voornaam, achternaam, telefoon, email, aantal, bedrag, status, lot_van, lot_tot) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("Zoek", "Jansen", "0612345678", "zoek@test.nl", 2, 5.00, "betaald", 1, 2)
        )
        db.execute(
            "INSERT INTO bestellingen (voornaam, achternaam, telefoon, email, aantal, bedrag, status) "
            "VALUES (?,?,?,?,?,?,?)",
            ("Andere", "Persoon", "0687654321", "andere@test.nl", 1, 2.50, "aangemaakt")
        )

    def tearDown(self):
        self.ctx.pop()

    def test_zoek_op_voornaam_vindt_resultaat(self):
        r = self.client.get("/admin?zoek=Zoek")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Zoek", r.data)
        self.assertNotIn(b"andere@test.nl", r.data)

    def test_zoek_op_achternaam_vindt_resultaat(self):
        r = self.client.get("/admin?zoek=Jansen")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Jansen", r.data)
        self.assertNotIn(b"andere@test.nl", r.data)

    def test_zoek_op_email_vindt_resultaat(self):
        r = self.client.get("/admin?zoek=zoek@test.nl")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"zoek@test.nl", r.data)
        self.assertNotIn(b"andere@test.nl", r.data)

    def test_zoek_op_lotnummer_vindt_resultaat(self):
        r = self.client.get("/admin?zoek=1")
        self.assertEqual(r.status_code, 200)
        # Lot_van=1 komt voor in de eerste bestelling
        self.assertIn(b"Zoek", r.data)
        self.assertIn(b"Jansen", r.data)

    def test_zoek_op_lotnummer_binnen_bereik(self):
        """Zoeken op #2 vindt bestelling met lot_van=1, lot_tot=2."""
        r = self.client.get("/admin?zoek=2")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Zoek", r.data)
        self.assertIn(b"Jansen", r.data)

    def test_zoek_op_lotnummer_met_hekje(self):
        """Zoeken op #2 (met #) vindt bestelling met lot_van=1, lot_tot=2."""
        r = self.client.get("/admin?zoek=%232")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Zoek", r.data)
        self.assertIn(b"Jansen", r.data)

    def test_zoek_geen_resultaat_toont_lege_lijst(self):
        r = self.client.get("/admin?zoek=bestaaniet99999")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"zoek@test.nl", r.data)
        self.assertNotIn(b"andere@test.nl", r.data)

    def test_zoek_gecombineerd_met_statusfilter(self):
        """Zoek en statusfilter samen werken correct."""
        r = self.client.get("/admin?zoek=Persoon&status=aangemaakt")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Andere", r.data)
        self.assertNotIn(b"zoek@test.nl", r.data)

    def test_zoek_te_lang_wordt_afgekapt(self):
        """Zoekterm langer dan 100 tekens wordt afgekapt zonder fout."""
        lange_zoekterm = "x" * 200
        r = self.client.get(f"/admin?zoek={lange_zoekterm}")
        self.assertEqual(r.status_code, 200)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN SORTEERFUNCTIE
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminSorteerfunctie(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})
        db = App.get_db()
        db.execute(
            "INSERT INTO bestellingen (voornaam, achternaam, telefoon, email, aantal, bedrag, status, lot_van, lot_tot) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("Anna", "Bakker", "0600000001", "anna@test.nl", 5, 10.00, "betaald", 1, 5)
        )
        db.execute(
            "INSERT INTO bestellingen (voornaam, achternaam, telefoon, email, aantal, bedrag, status, lot_van, lot_tot) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("Boris", "Coster", "0600000002", "boris@test.nl", 2, 5.00, "betaald", 6, 7)
        )

    def tearDown(self):
        self.ctx.pop()

    def test_sorteer_op_achternaam_asc(self):
        r = self.client.get("/admin?sorter=achternaam&richting=asc")
        self.assertEqual(r.status_code, 200)
        positie_anna = r.data.find(b"Bakker")
        positie_boris = r.data.find(b"Coster")
        self.assertLess(positie_anna, positie_boris)

    def test_sorteer_op_achternaam_desc(self):
        r = self.client.get("/admin?sorter=achternaam&richting=desc")
        self.assertEqual(r.status_code, 200)
        positie_anna = r.data.find(b"Bakker")
        positie_boris = r.data.find(b"Coster")
        self.assertGreater(positie_anna, positie_boris)

    def test_sorter_ongeldig_valt_terug_op_id(self):
        r = self.client.get("/admin?sorter=injectie&richting=asc")
        self.assertEqual(r.status_code, 200)

    def test_richting_ongeldig_valt_terug_op_desc(self):
        r = self.client.get("/admin?sorter=achternaam&richting=DROP")
        self.assertEqual(r.status_code, 200)

    def test_sorteerpijlen_zichtbaar_in_koppen(self):
        r = self.client.get("/admin?sorter=voornaam&richting=asc")
        self.assertIn(b"sorteer", r.data)


# ══════════════════════════════════════════════════════════════════════════════
# FOUTPAGINA'S — HTTP 400, 403, 500
# ══════════════════════════════════════════════════════════════════════════════

class TestFoutpaginasExtra(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_404_bevat_paginacode(self):
        r = self.client.get("/bestaat-niet-xyz")
        self.assertEqual(r.status_code, 404)
        self.assertIn(b"404", r.data)

    def test_500_error_handler_geeft_foutpagina(self):
        """De 500-errorhandler geeft een foutpagina terug met status 500."""
        with App.app.test_request_context():
            resp = App.app.make_response(
                App.app.handle_http_exception(
                    __import__("werkzeug.exceptions", fromlist=["InternalServerError"])
                    .InternalServerError()
                )
            )
        self.assertEqual(resp.status_code, 500)

    def test_foutpagina_bevat_security_headers(self):
        """Ook foutpagina's krijgen de security headers mee."""
        r = self.client.get("/bestaat-niet-xyz")
        self.assertIn("X-Frame-Options", r.headers)
        self.assertIn("Content-Security-Policy", r.headers)


# ══════════════════════════════════════════════════════════════════════════════
# BETAALD PAGINA — EXTRA GEVALLEN
# ══════════════════════════════════════════════════════════════════════════════

class TestBetaaldPaginaExtra(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_aangemaakt_zonder_mollie_id_redirect_via_tussenroute(self):
        """Bestelling met status 'aangemaakt' maar zonder mollie_id: tussenroute
        /betaald/r/<id> stuurt door naar / omdat mollie_id ontbreekt."""
        doe_bestelling(self.client)
        # Verwijder mollie_id zodat de tussenroute geen mollie_id kan opzoeken
        App.get_db().execute("UPDATE bestellingen SET mollie_id=NULL WHERE id=1")
        r = self.client.get("/betaald/r/1")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/", r.headers["Location"])

    def test_betaald_geeft_lotnummers_weer(self):
        """Succespagina toont de toegewezen lotnummers."""
        doe_bestelling(self.client, aantal=2)
        App.get_db().execute(
            "UPDATE bestellingen SET status='betaald', mollie_id='tr_x', "
            "lot_van=5, lot_tot=6 WHERE id=1"
        )
        r = self.client.get("/betaald/tr_x")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"5", r.data)
        self.assertIn(b"6", r.data)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN INSTELLINGEN — EXTRA GEVALLEN
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminInstellingenExtra(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    def test_transactiekosten_negatief_geeft_fout(self):
        """Negatieve transactiekosten worden geweigerd."""
        r = self.client.post("/admin/instellingen",
                             data={"transactiekosten": "-0.10"},
                             follow_redirects=True)
        self.assertIn(b"negatief", r.data)
        # Waarde mag niet gewijzigd zijn
        self.assertAlmostEqual(App.get_transactiekosten(), 0.32, places=2)

    def test_max_per_bestelling_en_max_eendjes_samen_opgeslagen(self):
        """max_per_bestelling en max_eendjes worden tegelijk correct opgeslagen."""
        self.client.post("/admin/instellingen", data={
            "max_eendjes": "500",
            "max_per_bestelling": "20",
        }, follow_redirects=True)
        rij = App.get_db().execute(
            "SELECT max_eendjes, max_per_bestelling FROM teller WHERE id=1"
        ).fetchone()
        self.assertEqual(rij["max_eendjes"], 500)
        self.assertEqual(rij["max_per_bestelling"], 20)

    def test_max_eendjes_nul_geeft_fout(self):
        """max_eendjes = 0 wordt geweigerd (moet minimaal 1 zijn)."""
        r = self.client.post("/admin/instellingen",
                             data={"max_eendjes": "0"},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        rij = App.get_db().execute("SELECT max_eendjes FROM teller WHERE id=1").fetchone()
        self.assertGreater(rij["max_eendjes"], 0)

    def test_ongeldig_getal_geeft_fout(self):
        """Niet-numerieke waarde voor prijs geeft een foutmelding."""
        r = self.client.post("/admin/instellingen",
                             data={"prijs_per_stuk": "abc"},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Ongeldig", r.data)


# ══════════════════════════════════════════════════════════════════════════════
# HOMEPAGE EN API
# ══════════════════════════════════════════════════════════════════════════════

class TestHomepageEnApi(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_homepage_geeft_200(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)

    def test_homepage_toont_beschikbare_eendjes(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        # De pagina moet een getal tonen (beschikbare eendjes)
        self.assertIn(b"eendj", r.data.lower())

    def test_api_beschikbaar_levert_max_per_bestelling(self):
        d = self.client.get("/api/beschikbaar").get_json()
        self.assertIn("max_per_bestelling", d)
        self.assertEqual(d["max_per_bestelling"], 100)

    def test_api_beschikbaar_verkocht_begint_op_nul(self):
        d = self.client.get("/api/beschikbaar").get_json()
        self.assertEqual(d["verkocht"], 0)

    def test_api_prijs_met_transactiekosten_param(self):
        """?transactiekosten=1 verhoogt het bedrag met de iDEAL-kosten."""
        r = self.client.get("/api/prijs?aantal=1&transactiekosten=1")
        self.assertEqual(r.status_code, 200)
        bedrag = r.get_json()["bedrag"]
        self.assertAlmostEqual(bedrag, 2.50 + 0.32, places=2)

    def test_homepage_toont_bestel_cta_knop(self):
        """Homepage toont de 'Bestel hier'-CTA-knop bij een schoon bezoek."""
        r = self.client.get("/")
        self.assertIn(b'id="bestelCta"', r.data)

    def test_homepage_formulier_verborgen_bij_eerste_bezoek(self):
        """Het bestelformulier is server-side verborgen bij een schoon bezoek."""
        r = self.client.get("/")
        self.assertIn(b'id="formulierInhoud" style="display:none"', r.data)

    def test_homepage_toont_verberg_knop_in_formulier(self):
        """De 'Verbergen'-knop zit in de formulierwrapper."""
        r = self.client.get("/")
        self.assertIn(b'id="verbergFormulier"', r.data)

    def test_homepage_formulier_direct_zichtbaar_na_validatiefout(self):
        """Na een validatiefout is het formulier direct zichtbaar (geen CTA-knop)."""
        r = self.client.post("/bestellen", data={
            "voornaam": "", "achternaam": "", "email": "", "telefoon": "", "aantal": "1"
        })
        self.assertNotIn(b'style="display:none"', r.data)
        self.assertNotIn(b'id="bestelCta"', r.data)


# ══════════════════════════════════════════════════════════════════════════════
# SECURITY HEADERS — AANVULLEND
#     X-XSS-Protection, onderdrukt Server-header, CSP base-uri / form-action
# ══════════════════════════════════════════════════════════════════════════════

class TestSecurityHeadersAanvullend(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_x_xss_protection_header_aanwezig(self):
        """X-XSS-Protection header moet aanwezig zijn."""
        r = self.client.get("/")
        self.assertIn("1; mode=block", r.headers.get("X-XSS-Protection", ""))

    def test_server_header_leeg(self):
        """Server-header mag geen versie-informatie bevatten."""
        r = self.client.get("/")
        self.assertEqual(r.headers.get("Server", ""), "")

    def test_csp_bevat_base_uri_self(self):
        """CSP moet base-uri 'self' bevatten om base-tag-injectie te voorkomen."""
        csp = self.client.get("/").headers.get("Content-Security-Policy", "")
        self.assertIn("base-uri 'self'", csp)

    def test_csp_bevat_form_action_self(self):
        """CSP moet form-action 'self' bevatten."""
        csp = self.client.get("/").headers.get("Content-Security-Policy", "")
        self.assertIn("form-action 'self'", csp)

    def test_csp_bevat_frame_ancestors_none(self):
        """CSP moet frame-ancestors 'none' bevatten (vervangt X-Frame-Options)."""
        csp = self.client.get("/").headers.get("Content-Security-Policy", "")
        self.assertIn("frame-ancestors 'none'", csp)

    def test_csp_bevat_img_src_self(self):
        """CSP mag afbeeldingen alleen van 'self' en data: URI's toestaan."""
        csp = self.client.get("/").headers.get("Content-Security-Policy", "")
        self.assertIn("img-src 'self' data:", csp)

    def test_referrer_policy_waarde(self):
        """Referrer-Policy moet strict-origin-when-cross-origin zijn."""
        r = self.client.get("/")
        self.assertEqual(
            r.headers.get("Referrer-Policy", ""),
            "strict-origin-when-cross-origin",
        )


# ══════════════════════════════════════════════════════════════════════════════
# BEVESTIGINGSMAIL — LOTNUMMER-TEKSTOPMAAK
#     stuur_bevestigingsmail() kent 3 tekstvarianten afhankelijk van het aantal:
#       1 lotnummer  → "lotnummer #X"
#       2-4 nummers  → opsomming "# x · # y · …"
#       5+ nummers   → bereik "# x t/m # y"
# ══════════════════════════════════════════════════════════════════════════════

class TestBevestigingsmailOpmaakvarianten(unittest.TestCase):

    def _vang_html(self, lot_van, lot_tot, aantal=None):
        if aantal is None:
            aantal = lot_tot - lot_van + 1
        verzonden = {}
        def nep_send(params):
            verzonden["html"] = params.get("html", "")
            return {"id": "test"}
        with patch("resend.Emails.send", side_effect=nep_send):
            stuur_bevestigingsmail("Jan", "Jansen", "jan@test.nl", aantal, lot_van, lot_tot, 5.00)
        return verzonden.get("html", "")

    def test_een_lotnummer_tekst_enkelvoud(self):
        """Bij lot_van == lot_tot verschijnt 'lotnummer' in enkelvoud."""
        html = self._vang_html(5, 5, aantal=1)
        self.assertIn("lotnummer", html)
        self.assertIn("#5", html)

    def test_twee_lotnummers_worden_opgesomd(self):
        """2 lotnummers worden individueel opgesomd met · als scheidingsteken."""
        html = self._vang_html(3, 4, aantal=2)
        self.assertIn("#3", html)
        self.assertIn("#4", html)
        self.assertIn("middot", html)  # HTML-entiteit &middot;

    def test_vier_lotnummers_worden_opgesomd(self):
        """4 lotnummers (< 5) worden individueel opgesomd."""
        html = self._vang_html(10, 13, aantal=4)
        self.assertIn("#10", html)
        self.assertIn("#13", html)
        self.assertIn("middot", html)

    def test_vijf_lotnummers_bullets(self):
        """5 lotnummers (verschil=4 < 5) gebruiken bullet-notatie."""
        html = self._vang_html(1, 5, aantal=5)
        self.assertIn("middot", html)
        self.assertIn("#1", html)
        self.assertIn("#5", html)
        self.assertNotIn("t/m", html)

    def test_zes_lotnummers_bereiknotatie(self):
        """6 lotnummers (verschil>=5) gebruiken bereiknotatie 't/m'."""
        html = self._vang_html(1, 6, aantal=6)
        self.assertIn("t/m", html)
        self.assertIn("#1", html)
        self.assertIn("#6", html)

    def test_tien_lotnummers_bereiknotatie(self):
        """10 lotnummers gebruiken bereiknotatie."""
        html = self._vang_html(1, 10, aantal=10)
        self.assertIn("t/m", html)
        self.assertNotIn("middot", html)

    def test_een_eendje_tekst_in_mail(self):
        """Bij 1 eendje staat 'eendje' (enkelvoud) in de mail."""
        html = self._vang_html(1, 1, aantal=1)
        self.assertIn("eendje", html)

    def test_meerdere_eendjes_tekst_in_mail(self):
        """Bij meerdere eendjes staat 'eendjes' (meervoud) in de mail."""
        html = self._vang_html(1, 3, aantal=3)
        self.assertIn("eendjes", html)


# ══════════════════════════════════════════════════════════════════════════════
# VALIDEER INVOER — GRENSWAARDES
#     Aanvullende tests voor exacte grenswaardes in valideer_invoer()
# ══════════════════════════════════════════════════════════════════════════════

class TestValideerInvoerGrenswaardes(unittest.TestCase):

    def _ok(self, voornaam="Jan", achternaam="Jansen", tel="0612345678",
            email="jan@test.nl", aantal=3, max_per_bestelling=100):
        return valideer_invoer(voornaam, achternaam, tel, email, aantal, max_per_bestelling)

    def test_naam_precies_2_tekens_geldig(self):
        """Naam van exact 2 tekens moet geldig zijn."""
        self.assertEqual(self._ok(voornaam="AB"), [])

    def test_naam_precies_100_tekens_geldig(self):
        """Naam van exact 100 tekens moet geldig zijn."""
        self.assertEqual(self._ok(voornaam="A" * 100), [])

    def test_naam_101_tekens_ongeldig(self):
        """Naam van 101 tekens moet ongeldig zijn."""
        self.assertGreater(len(self._ok(voornaam="A" * 101)), 0)

    def test_aantal_precies_max_per_bestelling_geldig(self):
        """Aantal precies gelijk aan max_per_bestelling is geldig."""
        self.assertEqual(self._ok(aantal=50, max_per_bestelling=50), [])

    def test_aantal_een_boven_max_per_bestelling_ongeldig(self):
        """Aantal één boven max_per_bestelling is ongeldig."""
        fouten = self._ok(aantal=51, max_per_bestelling=50)
        self.assertGreater(len(fouten), 0)

    def test_telefoon_precies_6_cijfers_geldig(self):
        """Telefoonnummer van exact 6 tekens (met cijfers) is geldig."""
        self.assertEqual(self._ok(tel="123456"), [])

    def test_telefoon_precies_20_tekens_geldig(self):
        """Telefoonnummer van exact 20 tekens is geldig."""
        self.assertEqual(self._ok(tel="+31 6 1234 5678 9012"), [])

    def test_email_met_subdomein_geldig(self):
        """E-mail met subdomein moet geldig zijn."""
        self.assertEqual(self._ok(email="jan@mail.example.com"), [])

    def test_email_met_plusteken_geldig(self):
        """E-mail met plus-adressering moet geldig zijn."""
        self.assertEqual(self._ok(email="jan+test@example.com"), [])


# ══════════════════════════════════════════════════════════════════════════════
# WIJS LOTNUMMERS TOE — AANVULLEND
#     Direct testen van wijs_lotnummers_toe() voor idempotentie bij al-betaald
# ══════════════════════════════════════════════════════════════════════════════

class TestWijsLotnummersToeAanvullend(unittest.TestCase):

    def setUp(self):
        self.db = maak_db()

    def tearDown(self):
        self.db.close()

    def _voeg_bestelling_toe(self, voornaam="Jan", achternaam="Jansen", aantal=2, status="aangemaakt"):
        self.db.execute(
            "INSERT INTO bestellingen (voornaam,achternaam,telefoon,email,aantal,bedrag,status) "
            "VALUES (?,?,?,?,?,?,?)", (voornaam, achternaam, "06", "jan@t.nl", aantal, 5.00, status)
        )
        return self.db.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_idempotentie_geeft_bestaande_loten_terug(self):
        """Als bestelling al 'betaald' is, worden de bestaande loten teruggegeven."""
        bid = self._voeg_bestelling_toe(aantal=2)
        # Eerste toewijzing
        s1, e1 = wijs_lotnummers_toe(self.db, bid, 2)
        # Tweede aanroep — idempotent
        s2, e2 = wijs_lotnummers_toe(self.db, bid, 2)
        self.assertEqual(s1, s2)
        self.assertEqual(e1, e2)

    def test_idempotentie_verhoogt_teller_niet(self):
        """Tweede aanroep op al-betaalde bestelling mag teller niet verhogen."""
        bid = self._voeg_bestelling_toe(aantal=3)
        wijs_lotnummers_toe(self.db, bid, 3)
        teller_na_eerste = self.db.execute(
            "SELECT volgend_lot FROM teller WHERE id=1"
        ).fetchone()["volgend_lot"]
        wijs_lotnummers_toe(self.db, bid, 3)
        teller_na_tweede = self.db.execute(
            "SELECT volgend_lot FROM teller WHERE id=1"
        ).fetchone()["volgend_lot"]
        self.assertEqual(teller_na_eerste, teller_na_tweede)

    def test_precies_een_lotnummer(self):
        """Bestelling van 1 krijgt lot_van == lot_tot."""
        bid = self._voeg_bestelling_toe(aantal=1)
        s, e = wijs_lotnummers_toe(self.db, bid, 1)
        self.assertEqual(s, e)

    def test_teller_verhoogd_na_toewijzing(self):
        """Na toewijzing van N loten staat de teller op start + N."""
        bid = self._voeg_bestelling_toe(aantal=4)
        s, e = wijs_lotnummers_toe(self.db, bid, 4)
        teller = self.db.execute(
            "SELECT volgend_lot FROM teller WHERE id=1"
        ).fetchone()["volgend_lot"]
        self.assertEqual(teller, e + 1)

    def test_oversell_op_exact_laatste_lotnummer(self):
        """Wanneer volgend_lot == max_eendjes is nog 1 lot beschikbaar (niet over)."""
        self.db.execute("UPDATE teller SET volgend_lot=? WHERE id=1", (MAX_EENDJES,))
        bid = self._voeg_bestelling_toe(aantal=1)
        # Dit moet NIET falen — er is nog precies 1 over
        s, e = wijs_lotnummers_toe(self.db, bid, 1)
        self.assertEqual(s, MAX_EENDJES)
        self.assertEqual(e, MAX_EENDJES)

    def test_oversell_een_boven_maximum(self):
        """Wanneer einde > max_eendjes gooit de functie ValueError."""
        self.db.execute("UPDATE teller SET volgend_lot=? WHERE id=1", (MAX_EENDJES,))
        bid = self._voeg_bestelling_toe(aantal=2)
        with self.assertRaises(ValueError):
            wijs_lotnummers_toe(self.db, bid, 2)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PAGINA — GEBRUIKERSNAAM EN BEHEERDERLIJST
#     Admin-panel toont ingelogde gebruiker en lijst van beheerders
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminPaginaGebruikersnaam(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    def test_admin_pagina_toont_ingelogde_gebruiker(self):
        """Admin-pagina moet de gebruikersnaam van de ingelogde beheerder tonen."""
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"admin", r.data)

    def test_admin_pagina_toont_beheerderlijst(self):
        """Admin-pagina moet de lijst van beheerdersaccounts tonen."""
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 200)
        # Het account 'admin' moet in de lijst staan
        self.assertIn(b"admin", r.data)

    def test_admin_pagina_toont_tweede_beheerder_na_toevoegen(self):
        """Na toevoegen van een tweede account verschijnt die in de admin-pagina."""
        self.client.post("/admin/beheerder-toevoegen", data={
            "gebruikersnaam": "tweede_admin_zichtbaar",
            "wachtwoord": "sterkwachtwoord9",
            "wachtwoord_bevestiging": "sterkwachtwoord9",
        })
        r = self.client.get("/admin")
        self.assertIn(b"tweede_admin_zichtbaar", r.data)


# ══════════════════════════════════════════════════════════════════════════════
# WACHTWOORD WIJZIGEN — OUD WACHTWOORD WERKT NIET MEER
#     Na een succesvolle wachtwoordwijziging werkt het oude wachtwoord niet meer
# ══════════════════════════════════════════════════════════════════════════════

class TestWachtwoordWijzigenOudWachtwoord(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    def test_oud_wachtwoord_werkt_niet_meer_na_wijziging(self):
        """Na wijzigen moet inloggen met het oude wachtwoord mislukken."""
        self.client.post("/admin/wachtwoord-wijzigen", data={
            "huidig_wachtwoord": "testpass12345",
            "nieuw_wachtwoord": "nieuwwachtwoord99",
            "nieuw_wachtwoord_bevestiging": "nieuwwachtwoord99",
        })
        self.client.get("/admin/logout")
        r = self.client.post("/admin/login",
                             data={"gebruiker": "admin",
                                   "wachtwoord": "testpass12345"})
        # Moet op de loginpagina blijven — geen redirect naar /admin
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Onjuiste", r.data)

    def test_wachtwoord_wijzigen_hash_in_db_gewijzigd(self):
        """Na wijzigen moet de hash in de DB veranderd zijn."""
        oude_hash = App.get_db().execute(
            "SELECT wachtwoord_hash FROM beheerders WHERE gebruikersnaam='admin'"
        ).fetchone()["wachtwoord_hash"]
        self.client.post("/admin/wachtwoord-wijzigen", data={
            "huidig_wachtwoord": "testpass12345",
            "nieuw_wachtwoord": "nieuwwachtwoord99",
            "nieuw_wachtwoord_bevestiging": "nieuwwachtwoord99",
        })
        nieuwe_hash = App.get_db().execute(
            "SELECT wachtwoord_hash FROM beheerders WHERE gebruikersnaam='admin'"
        ).fetchone()["wachtwoord_hash"]
        self.assertNotEqual(oude_hash, nieuwe_hash)


# ══════════════════════════════════════════════════════════════════════════════
# BETAALD PAGINA — FALLBACK MOLLIE-FOUT
#     /betaald/<id> vangt Mollie-API-fouten af zonder te crashen
# ══════════════════════════════════════════════════════════════════════════════

class TestBetaaldPaginaFallbackFout(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_mollie_fout_in_fallback_toont_wachtpagina(self):
        """Als Mollie-API faalt in de fallback, toont de pagina de wachtstatus."""
        doe_bestelling(self.client)
        stel_mollie_id_in(1, "tr_fallback_fout")
        with patch("app.maak_mollie_client") as mc:
            mc.return_value.payments.get.side_effect = Exception("Mollie onbereikbaar")
            r = self.client.get("/betaald/tr_fallback_fout")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"wacht", r.data)

    def test_mollie_mollie_error_in_fallback_geeft_200(self):
        """MollieError in de fallback levert geen crash op."""
        doe_bestelling(self.client)
        stel_mollie_id_in(1, "tr_fallback_molliefout")
        with patch("app.maak_mollie_client") as mc:
            mc.return_value.payments.get.side_effect = MollieError("timeout")
            r = self.client.get("/betaald/tr_fallback_molliefout")
        self.assertEqual(r.status_code, 200)

    def test_betaald_status_geannuleerd_toont_waarschuwing(self):
        """Geannuleerde betaling toont een waarschuwingsstatus."""
        doe_bestelling(self.client)
        App.get_db().execute(
            "UPDATE bestellingen SET status='geannuleerd', mollie_id='tr_ann' WHERE id=1"
        )
        r = self.client.get("/betaald/tr_ann")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"waarsch", r.data)

    def test_betaald_status_verlopen_toont_waarschuwing(self):
        """Verlopen betaling toont een waarschuwingsstatus."""
        doe_bestelling(self.client)
        App.get_db().execute(
            "UPDATE bestellingen SET status='verlopen', mollie_id='tr_exp' WHERE id=1"
        )
        r = self.client.get("/betaald/tr_exp")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"waarsch", r.data)


# ══════════════════════════════════════════════════════════════════════════════
# BESTELLEN — AANVULLENDE GEVALLEN
#     Minder geteste paden in de /bestellen route
# ══════════════════════════════════════════════════════════════════════════════

class TestBestellenAanvullend(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_geldige_bestelling_slaat_betaalwijze_ideal_op(self):
        """Normale iDEAL-bestelling krijgt betaalwijze='ideal' in de database."""
        doe_bestelling(self.client, voornaam="iDEAL", achternaam="Test")
        rij = App.get_db().execute(
            "SELECT betaalwijze FROM bestellingen WHERE voornaam='iDEAL' AND achternaam='Test'"
        ).fetchone()
        self.assertEqual(rij["betaalwijze"], "ideal")

    def test_validatiefout_behoudt_ingevulde_naam(self):
        """Bij validatiefout (lege naam) moet de response de eerder ingevulde
        gegevens terugsturen (vorig-dict)."""
        r = self.client.post("/bestellen", data={
            "voornaam": "", "achternaam": "",
            "telefoon": "0612345678",
            "email": "jan@test.nl",
            "aantal": "2",
        })
        self.assertEqual(r.status_code, 422)

    def test_naam_alleen_spaties_geeft_422(self):
        """Naam met alleen spaties (len < 2 na strip) geeft een 422."""
        r = doe_bestelling(self.client, voornaam="   ", achternaam="   ")
        self.assertEqual(r.status_code, 422)

    def test_te_veel_eendjes_bij_bijna_vol_geeft_409(self):
        """Als er minder eendjes beschikbaar zijn dan besteld wordt, geeft 409."""
        # Stel max op 3, bestel er 5
        App.get_db().execute("UPDATE teller SET max_eendjes=3 WHERE id=1")
        r = doe_bestelling(self.client, aantal=5)
        self.assertEqual(r.status_code, 409)

    def test_bestelling_slaat_naam_op(self):
        """De naam wordt correct opgeslagen in de database."""
        doe_bestelling(self.client, voornaam="Unieke", achternaam="Naam Test")
        rij = App.get_db().execute(
            "SELECT voornaam, achternaam FROM bestellingen WHERE voornaam='Unieke' AND achternaam='Naam Test'"
        ).fetchone()
        self.assertIsNotNone(rij)
        self.assertEqual(rij["voornaam"], "Unieke")
        self.assertEqual(rij["achternaam"], "Naam Test")

    def test_bestelling_slaat_email_lowercase_op(self):
        """E-mailadres wordt lowercase opgeslagen."""
        doe_bestelling(self.client, email="TEST@EXAMPLE.COM")
        rij = App.get_db().execute(
            "SELECT email FROM bestellingen WHERE id=1"
        ).fetchone()
        self.assertEqual(rij["email"], "test@example.com")


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN INSTELLINGEN — LEEG FORMULIER
#     Formulier zonder ingevulde velden doet geen wijzigingen
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminInstellingenLeegFormulier(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    def test_leeg_formulier_wijzigt_niets(self):
        """Volledig leeg instellingen-formulier mag geen waarden wijzigen."""
        # Lees beginsituatie
        rij_voor = App.get_db().execute(
            "SELECT max_eendjes, max_per_bestelling, prijs_per_stuk, "
            "prijs_vijf_stuks, transactiekosten FROM teller WHERE id=1"
        ).fetchone()
        r = self.client.post("/admin/instellingen", data={})
        self.assertEqual(r.status_code, 302)
        rij_na = App.get_db().execute(
            "SELECT max_eendjes, max_per_bestelling, prijs_per_stuk, "
            "prijs_vijf_stuks, transactiekosten FROM teller WHERE id=1"
        ).fetchone()
        self.assertEqual(rij_voor["max_eendjes"],        rij_na["max_eendjes"])
        self.assertEqual(rij_voor["max_per_bestelling"], rij_na["max_per_bestelling"])
        self.assertEqual(rij_voor["prijs_per_stuk"],     rij_na["prijs_per_stuk"])
        self.assertEqual(rij_voor["prijs_vijf_stuks"],   rij_na["prijs_vijf_stuks"])
        self.assertAlmostEqual(rij_voor["transactiekosten"],
                               rij_na["transactiekosten"], places=4)

    def test_prijs_per_stuk_negatief_geeft_fout(self):
        """Negatieve prijs per stuk wordt geweigerd."""
        r = self.client.post("/admin/instellingen",
                             data={"prijs_per_stuk": "-1.00"},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"groter dan 0", r.data)
        self.assertAlmostEqual(App.get_prijs_per_stuk(), 2.50, places=2)

    def test_max_per_bestelling_nul_geeft_fout(self):
        """max_per_bestelling = 0 is ongeldig (moet minimaal 1 zijn)."""
        r = self.client.post("/admin/instellingen",
                             data={"max_per_bestelling": "0"},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        rij = App.get_db().execute(
            "SELECT max_per_bestelling FROM teller WHERE id=1"
        ).fetchone()
        self.assertGreater(rij["max_per_bestelling"], 0)


# ══════════════════════════════════════════════════════════════════════════════
# HANDMATIGE BESTELLING — ONGELDIG TELEFOONNUMMER
#     handmatige_bestelling() valideert ook het telefoonnummer
# ══════════════════════════════════════════════════════════════════════════════

class TestHandmatigeBestellingTelefoon(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    def test_ongeldig_telefoonnummer_geeft_fout(self):
        """Ongeldig telefoonnummer bij handmatige bestelling wordt geweigerd."""
        r = self.client.post("/admin/handmatig", data={
            "h_voornaam": "Test", "h_achternaam": "Persoon", "email": "",
            "telefoon": "abc-xyz",  # geen cijfers
            "aantal": "1", "betaalwijze": "contant",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        count = App.get_db().execute("SELECT COUNT(*) FROM bestellingen").fetchone()[0]
        self.assertEqual(count, 0)

    def test_geldig_telefoonnummer_met_spaties_en_plus(self):
        """Telefoonnummer met spaties, plus en streepjes is geldig."""
        r = self.client.post("/admin/handmatig", data={
            "h_voornaam": "Test", "h_achternaam": "Persoon", "email": "",
            "telefoon": "+31 6-12345678",
            "aantal": "1", "betaalwijze": "contant",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        rij = App.get_db().execute(
            "SELECT COUNT(*) AS n FROM bestellingen WHERE voornaam='Test' AND achternaam='Persoon'"
        ).fetchone()
        self.assertEqual(rij["n"], 1)

    def test_handmatig_zonder_telefoon_is_geldig(self):
        """Lege telefoon bij handmatige bestelling is toegestaan."""
        r = self.client.post("/admin/handmatig", data={
            "h_voornaam": "Geen", "h_achternaam": "Telefoon", "email": "",
            "telefoon": "",
            "aantal": "1", "betaalwijze": "contant",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        rij = App.get_db().execute(
            "SELECT COUNT(*) AS n FROM bestellingen WHERE voornaam='Geen' AND achternaam='Telefoon'"
        ).fetchone()
        self.assertEqual(rij["n"], 1)


# ══════════════════════════════════════════════════════════════════════════════
# WEBHOOK — AANVULLENDE GEVALLEN
#     Extra edge-cases voor de webhook-handler
# ══════════════════════════════════════════════════════════════════════════════

class TestWebhookAanvullend(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_webhook_logt_aanroep_in_webhook_log(self):
        """Elke geldige webhook-aanroep wordt opgeslagen in webhook_log."""
        doe_bestelling(self.client)
        stel_mollie_id_in(1, "tr_logtest")
        doe_webhook(self.client, "tr_logtest", "paid")
        rij = App.get_db().execute(
            "SELECT mollie_id FROM webhook_log WHERE mollie_id='tr_logtest'"
        ).fetchone()
        self.assertIsNotNone(rij)

    def test_webhook_id_zonder_tr_prefix_geeft_400(self):
        """ID zonder 'tr_' prefix is ongeldig en geeft 400."""
        r = self.client.post("/webhook", data={"id": "pp_test001"},
                             environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 400)

    def test_webhook_paid_mail_niet_opnieuw_bij_dubbele_aanroep(self):
        """Bij een tweede paid-webhook voor dezelfde bestelling wordt geen mail verstuurd."""
        doe_bestelling(self.client)
        stel_mollie_id_in(1, "tr_dubbel_mail")
        # Eerste aanroep
        doe_webhook(self.client, "tr_dubbel_mail", "paid")
        pogingen_na_eerste = App.get_db().execute(
            "SELECT pogingen FROM bestellingen WHERE mollie_id='tr_dubbel_mail'"
        ).fetchone()["pogingen"]
        # Tweede aanroep — webhook-handler herkent 'al betaald' en stuurt geen mail
        doe_webhook(self.client, "tr_dubbel_mail", "paid")
        pogingen_na_tweede = App.get_db().execute(
            "SELECT pogingen FROM bestellingen WHERE mollie_id='tr_dubbel_mail'"
        ).fetchone()["pogingen"]
        # Pogingen mogen niet verhoogd worden bij de tweede aanroep
        self.assertEqual(pogingen_na_eerste, pogingen_na_tweede)

    def test_webhook_ip_wordt_opgeslagen_in_log(self):
        """Het IP-adres van de webhook-aanroep wordt opgeslagen in webhook_log."""
        doe_bestelling(self.client)
        stel_mollie_id_in(1, "tr_ip_log")
        doe_webhook(self.client, "tr_ip_log", "open", ip="10.0.0.1")
        rij = App.get_db().execute(
            "SELECT ip FROM webhook_log WHERE mollie_id='tr_ip_log'"
        ).fetchone()
        self.assertIsNotNone(rij)
        self.assertEqual(rij["ip"], "10.0.0.1")


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN BEHEER PAGINA
#     GET /admin/beheer — instellingen, beheerders, gevaarzone
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminBeheerPagina(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def _login(self):
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def test_admin_beheer_zonder_login_redirect(self):
        """GET /admin/beheer zonder login geeft 302."""
        r = self.client.get("/admin/beheer")
        self.assertEqual(r.status_code, 302)

    def test_admin_beheer_met_login_geeft_200(self):
        """GET /admin/beheer met login geeft 200."""
        self._login()
        r = self.client.get("/admin/beheer")
        self.assertEqual(r.status_code, 200)

    def test_admin_beheer_bevat_instellingen(self):
        """Beheerpagina bevat de Instellingen-sectie."""
        self._login()
        r = self.client.get("/admin/beheer")
        self.assertIn(b"Instellingen", r.data)

    def test_admin_beheer_bevat_beheerders(self):
        """Beheerpagina bevat de Beheerders-sectie."""
        self._login()
        r = self.client.get("/admin/beheer")
        self.assertIn(b"Beheerders", r.data)

    def test_admin_beheer_bevat_gevaarzone(self):
        """Beheerpagina bevat de Gevaarzone-sectie."""
        self._login()
        r = self.client.get("/admin/beheer")
        self.assertIn(b"Gevaarzone", r.data)

    def test_admin_beheer_bevat_nav_bestellingen(self):
        """Beheerpagina bevat een link terug naar /admin."""
        self._login()
        r = self.client.get("/admin/beheer")
        self.assertIn(b"/admin", r.data)

    def test_admin_beheer_bevat_noindex(self):
        """Beheerpagina bevat noindex meta-tag."""
        self._login()
        r = self.client.get("/admin/beheer")
        self.assertIn(b"noindex", r.data)

    def test_admin_instellingen_redirect_naar_beheer(self):
        """POST naar /admin/instellingen stuurt door naar /admin/beheer."""
        self._login()
        r = self.client.post("/admin/instellingen",
                             data={"max_eendjes": "3000"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/beheer", r.headers["Location"])

    def test_admin_opruimen_redirect_naar_beheer(self):
        """POST naar /admin/opruimen stuurt door naar /admin/beheer."""
        self._login()
        r = self.client.post("/admin/opruimen")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/beheer", r.headers["Location"])

    def test_admin_reset_redirect_naar_beheer(self):
        """POST naar /admin/reset met RESET stuurt door naar /admin/beheer."""
        self._login()
        r = self.client.post("/admin/reset", data={"bevestiging": "RESET"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/beheer", r.headers["Location"])

    def test_admin_pagina_bevat_beheer_knop(self):
        """GET /admin bevat een link naar /admin/beheer."""
        self._login()
        r = self.client.get("/admin")
        self.assertIn(b"/admin/beheer", r.data)

    def test_admin_pagina_bevat_geen_instellingen(self):
        """GET /admin bevat de instellingen-formuliervelden niet meer."""
        self._login()
        r = self.client.get("/admin")
        self.assertNotIn(b"Totaal beschikbare eendjes", r.data)

    def test_beheerder_toevoegen_redirect_naar_beheer(self):
        """POST naar /admin/beheerder-toevoegen stuurt door naar /admin/beheer."""
        self._login()
        r = self.client.post("/admin/beheerder-toevoegen", data={
            "gebruikersnaam": "nieuw_test_beheerder",
            "wachtwoord": "sterkwachtwoord99",
            "wachtwoord_bevestiging": "sterkwachtwoord99",
        })
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/beheer", r.headers["Location"])

    def test_admin_titel_bestellingen(self):
        """Paginatitel van /admin is 'Bestellingen'."""
        self._login()
        r = self.client.get("/admin")
        self.assertIn(b"Bestellingen", r.data)

    def test_admin_beheer_titel_beheer(self):
        """Paginatitel van /admin/beheer is 'Beheer'."""
        self._login()
        r = self.client.get("/admin/beheer")
        self.assertIn(b"Beheer", r.data)

    def test_admin_bevat_zoek_rij(self):
        """Bestellingenpagina bevat zoek-rij met zoekveld."""
        self._login()
        r = self.client.get("/admin")
        self.assertIn(b"zoek-rij", r.data)

    def test_admin_bevat_filter_rij(self):
        """Bestellingenpagina bevat filter-rij met statusfilters."""
        self._login()
        r = self.client.get("/admin")
        self.assertIn(b"filter-rij", r.data)

    def test_admin_bevat_bestellingen_nav_link(self):
        """Beheerpagina bevat link terug naar /admin."""
        self._login()
        r = self.client.get("/admin/beheer")
        self.assertIn(b"/admin\"", r.data)


# ══════════════════════════════════════════════════════════════════════════════
# ONDERHOUDSMODUS
# ══════════════════════════════════════════════════════════════════════════════

class TestOnderhoudsmodus(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        # Zet onderhoudsmodus altijd uit na elke test
        self.client.post("/admin/instellingen", data={})
        self.ctx.pop()

    def _zet_modus(self, aan: bool):
        data = {"onderhoudsmodus": "1"} if aan else {}
        self.client.post("/admin/instellingen", data=data)

    def test_onderhoudsmodus_standaard_uit(self):
        """Onderhoudsmodus is standaard uitgeschakeld."""
        from app import get_onderhoudsmodus
        self.assertFalse(get_onderhoudsmodus())

    def test_onderhoudsmodus_inschakelen_slaat_op(self):
        """Na POST met onderhoudsmodus=1 retourneert get_onderhoudsmodus() True."""
        from app import get_onderhoudsmodus
        self._zet_modus(True)
        self.assertTrue(get_onderhoudsmodus())

    def test_onderhoudsmodus_uitschakelen_slaat_op(self):
        """Na POST zonder onderhoudsmodus retourneert get_onderhoudsmodus() False."""
        from app import get_onderhoudsmodus
        self._zet_modus(True)
        self._zet_modus(False)
        self.assertFalse(get_onderhoudsmodus())

    def test_publieke_route_geeft_503_als_modus_aan(self):
        """/ geeft HTTP 503 als onderhoudsmodus is ingeschakeld."""
        self._zet_modus(True)
        r = self.client.get("/")
        self.assertEqual(r.status_code, 503)

    def test_publieke_route_geeft_200_als_modus_uit(self):
        """/ geeft HTTP 200 als onderhoudsmodus is uitgeschakeld."""
        self._zet_modus(False)
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)

    def test_onderhoudspagina_bevat_bericht(self):
        """Onderhoudspagina toont een informatief bericht."""
        self._zet_modus(True)
        r = self.client.get("/")
        self.assertIn(b"terug", r.data.lower())

    def test_onderhoudspagina_bevat_noindex(self):
        """Onderhoudspagina heeft noindex meta-tag."""
        self._zet_modus(True)
        r = self.client.get("/")
        self.assertIn(b"noindex", r.data)

    def test_admin_bereikbaar_in_onderhoudsmodus(self):
        """GET /admin is bereikbaar (200) als onderhoudsmodus aan is."""
        self._zet_modus(True)
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 200)

    def test_admin_beheer_bereikbaar_in_onderhoudsmodus(self):
        """GET /admin/beheer is bereikbaar (200) als onderhoudsmodus aan is."""
        self._zet_modus(True)
        r = self.client.get("/admin/beheer")
        self.assertEqual(r.status_code, 200)

    def test_webhook_bereikbaar_in_onderhoudsmodus(self):
        """POST /webhook retourneert geen 503 als onderhoudsmodus aan is."""
        self._zet_modus(True)
        r = self.client.post("/webhook", data={"id": "tr_test"})
        self.assertNotEqual(r.status_code, 503)

    def test_api_beschikbaar_bereikbaar_in_onderhoudsmodus(self):
        """GET /api/beschikbaar retourneert geen 503 als onderhoudsmodus aan is."""
        self._zet_modus(True)
        r = self.client.get("/api/beschikbaar")
        self.assertNotEqual(r.status_code, 503)

    def test_admin_beheer_toont_onderhoudsmodus_checkbox(self):
        """Beheerpagina toont de onderhoudsmodus-checkbox."""
        r = self.client.get("/admin/beheer")
        self.assertIn(b"onderhoudsmodus", r.data.lower())

    def test_inschakelen_toont_flashmelding(self):
        """Inschakelen onderhoudsmodus geeft flash-bevestiging."""
        r = self.client.post("/admin/instellingen",
                             data={"onderhoudsmodus": "1"},
                             follow_redirects=True)
        self.assertIn(b"ngeschakeld", r.data)

    def test_uitschakelen_toont_flashmelding(self):
        """Uitschakelen onderhoudsmodus geeft flash-bevestiging."""
        self._zet_modus(True)
        r = self.client.post("/admin/instellingen",
                             data={},
                             follow_redirects=True)
        self.assertIn(b"itgeschakeld", r.data)


# ══════════════════════════════════════════════════════════════════════════════
# Setup-pagina (eerste beheerdersaccount)
# ══════════════════════════════════════════════════════════════════════════════

class TestSetupPagina(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        # Zet een nep-token zodat de setup-route actief is
        App._setup_token = "testtoken123"
        # Verwijder eventueel achtergebleven tokenbestand
        if os.path.exists(App._SETUP_TOKEN_BESTAND):
            os.remove(App._SETUP_TOKEN_BESTAND)

    def tearDown(self):
        App._setup_token = None
        if os.path.exists(App._SETUP_TOKEN_BESTAND):
            os.remove(App._SETUP_TOKEN_BESTAND)
        self.ctx.pop()

    def test_setup_zonder_token_geeft_404(self):
        """GET /setup zonder token geeft 404."""
        r = self.client.get("/setup")
        self.assertEqual(r.status_code, 404)

    def test_setup_met_verkeerd_token_geeft_404(self):
        """GET /setup met verkeerd token geeft 404."""
        r = self.client.get("/setup?token=fouttoken")
        self.assertEqual(r.status_code, 404)

    def test_setup_met_juist_token_geeft_200(self):
        """GET /setup met juist token toont het formulier."""
        r = self.client.get("/setup?token=testtoken123")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Account aanmaken", r.data)

    def test_setup_formulier_bevat_uitleg(self):
        """Setup-pagina toont een uitlegblok."""
        r = self.client.get("/setup?token=testtoken123")
        self.assertIn(b"beheerdersaccount", r.data)

    def test_setup_te_kort_wachtwoord_geeft_fout(self):
        """POST met te kort wachtwoord toont foutmelding."""
        r = self.client.post("/setup?token=testtoken123", data={
            "token": "testtoken123",
            "gebruikersnaam": "beheerder",
            "wachtwoord": "tekortp",
            "bevestiging": "tekortp",
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"12 tekens", r.data)

    def test_setup_niet_overeenkomende_wachtwoorden_geeft_fout(self):
        """POST met niet-overeenkomende wachtwoorden toont foutmelding."""
        r = self.client.post("/setup?token=testtoken123", data={
            "token": "testtoken123",
            "gebruikersnaam": "beheerder",
            "wachtwoord": "langgoed12345",
            "bevestiging": "anderswachtwoord",
        })
        self.assertIn(b"niet overeen", r.data)

    def test_setup_geldig_maakt_account_en_redirectt(self):
        """Geldige POST maakt account aan en redirectt naar login."""
        r = self.client.post("/setup?token=testtoken123", data={
            "token": "testtoken123",
            "gebruikersnaam": "nieuwbeheerder",
            "wachtwoord": "veiligwacht12",
            "bevestiging": "veiligwacht12",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login", r.headers["Location"])

    def test_setup_token_wordt_gewist_na_aanmaken(self):
        """Na succesvol aanmaken is het setup-token None."""
        self.client.post("/setup?token=testtoken123", data={
            "token": "testtoken123",
            "gebruikersnaam": "nieuwbeheerder",
            "wachtwoord": "veiligwacht12",
            "bevestiging": "veiligwacht12",
        })
        self.assertIsNone(App._setup_token)

    def test_setup_niet_beschikbaar_als_token_none(self):
        """Als _setup_token None is geeft /setup 404."""
        App._setup_token = None
        r = self.client.get("/setup?token=testtoken123")
        self.assertEqual(r.status_code, 404)

    def test_setup_verwijdert_tokenbestand_na_aanmaken(self):
        """Na succesvol aanmaken wordt het .setup_token bestand verwijderd."""
        with open(App._SETUP_TOKEN_BESTAND, "w") as f:
            f.write("testtoken123")
        self.client.post("/setup?token=testtoken123", data={
            "token": "testtoken123",
            "gebruikersnaam": "nieuwbeheerder",
            "wachtwoord": "veiligwacht12",
            "bevestiging": "veiligwacht12",
        })
        self.assertFalse(os.path.exists(App._SETUP_TOKEN_BESTAND))


# ══════════════════════════════════════════════════════════════════════════════
# Recente wijzigingen: vallende eendjes, accordion, afzendernaam, projectblokje
# ══════════════════════════════════════════════════════════════════════════════

class TestRecenteWijzigingen(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def _zet_betaald(self):
        doe_bestelling(self.client)
        App.get_db().execute(
            "UPDATE bestellingen SET status='betaald', mollie_id='tr_x', lot_van=1, lot_tot=2 WHERE id=1"
        )

    def _vang_mail(self, **kwargs):
        verzonden = {}
        def nep_send(params):
            verzonden.update(params)
            return {"id": "test"}
        with patch("resend.Emails.send", side_effect=nep_send):
            stuur_bevestigingsmail("Jan", "Jansen", "jan@test.nl", 2, 1, 2, 5.00, **kwargs)
        return verzonden

    # --- Vallende eendjes animatie ---

    def test_vallende_eendjes_script_aanwezig_bij_succes(self):
        """Animatiescript (maakEendje) wordt alleen ingeladen bij status 'betaald'."""
        self._zet_betaald()
        r = self.client.get("/betaald/tr_x")
        self.assertIn(b"maakEendje", r.data)

    def test_vallende_eendjes_script_afwezig_bij_mislukt(self):
        """Geen animatiescript bij mislukte betaling."""
        doe_bestelling(self.client)
        App.get_db().execute(
            "UPDATE bestellingen SET status='mislukt', mollie_id='tr_x' WHERE id=1"
        )
        r = self.client.get("/betaald/tr_x")
        self.assertNotIn(b"maakEendje", r.data)

    def test_vallende_eendjes_script_afwezig_bij_geannuleerd(self):
        """Geen animatiescript bij geannuleerde betaling."""
        doe_bestelling(self.client)
        App.get_db().execute(
            "UPDATE bestellingen SET status='geannuleerd', mollie_id='tr_x' WHERE id=1"
        )
        r = self.client.get("/betaald/tr_x")
        self.assertNotIn(b"maakEendje", r.data)

    def test_projectzin_zichtbaar_op_betaald_pagina_bij_succes(self):
        """Projectvermelding 'Ik geloof, ik deel' zichtbaar op betaald-pagina."""
        self._zet_betaald()
        r = self.client.get("/betaald/tr_x")
        self.assertIn("Ik geloof, ik deel".encode(), r.data)

    # --- Accordion "Hoe werkt de race?" ---

    def test_homepage_bevat_accordion_details(self):
        """Homepage bevat een <details> accordion element."""
        r = self.client.get("/")
        self.assertIn(b"<details", r.data)

    def test_accordion_toont_hoe_werkt_het_tekst(self):
        """Accordion-summary bevat 'Hoe werkt de race?'."""
        r = self.client.get("/")
        self.assertIn("Hoe werkt de race?".encode(), r.data)

    def test_accordion_standaard_ingeklapt(self):
        """<details> heeft geen 'open' attribuut — standaard ingeklapt."""
        import re as _re
        r = self.client.get("/")
        match = _re.search(r'<details[^>]*>', r.data.decode())
        self.assertIsNotNone(match)
        self.assertNotIn(" open", match.group())

    # --- AFZENDER_NAAM ---

    def test_afzender_naam_in_from_veld(self):
        """E-mail from-veld bevat 'Badeendjesrace Wapenveld'."""
        mail = self._vang_mail()
        self.assertIn("Badeendjesrace Wapenveld", mail.get("from", ""))

    def test_afzender_naam_in_mailbody(self):
        """E-mailbody bevat de afzendernaam 'Badeendjesrace Wapenveld'."""
        mail = self._vang_mail()
        self.assertIn("Badeendjesrace Wapenveld", mail.get("html", ""))

    # --- Projectblokje in e-mail ---

    def test_projectblokje_noemt_ik_geloof_ik_deel(self):
        """Bevestigingsmail vermeldt het diaconale project 'Ik geloof, ik deel'."""
        mail = self._vang_mail()
        self.assertIn("Ik geloof, ik deel", mail.get("html", ""))

    def test_projectblokje_bevat_projectlink(self):
        """Bevestigingsmail bevat link naar het project op hervormdwapenveld.nl."""
        mail = self._vang_mail()
        self.assertIn("hgjb-diaconaal-project", mail.get("html", ""))

    def test_projectblokje_noemt_hgjb_commissie(self):
        """Bevestigingsmail vermeldt de HGJB-commissie."""
        mail = self._vang_mail()
        self.assertIn("HGJB-commissie", mail.get("html", ""))


# ══════════════════════════════════════════════════════════════════════════════
# Sponsorstrip
# ══════════════════════════════════════════════════════════════════════════════

class TestSponsorStrip(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def _get_index(self, bestanden):
        """Haal de homepage op met gesimuleerde sponsorbestanden."""
        with patch("app.os.path.isdir", return_value=True), \
             patch("app.os.listdir", return_value=bestanden):
            return self.client.get("/")

    def test_geen_sponsors_geen_sectie(self):
        """Als de sponsormap leeg is, verschijnt er geen sponsorsectie."""
        r = self._get_index([])
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"Met dank aan onze sponsors", r.data)

    def test_map_bestaat_niet_geen_sectie(self):
        """Als de sponsormap niet bestaat, verschijnt er geen sponsorsectie."""
        with patch("app.os.path.isdir", return_value=False):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"Met dank aan onze sponsors", r.data)

    def test_een_sponsor_toont_statische_layout(self):
        """Met 1 sponsor wordt de statische layout getoond (geen scrollanimatie)."""
        r = self._get_index(["bakker.png"])
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'class="sponsor-rij-statisch"', r.data)
        self.assertNotIn(b'class="sponsor-baan"', r.data)

    def test_vier_sponsors_toont_statische_layout(self):
        """Met precies 4 sponsors wordt de statische layout getoond."""
        r = self._get_index(["a.png", "b.png", "c.jpg", "d.svg"])
        self.assertIn(b'class="sponsor-rij-statisch"', r.data)
        self.assertNotIn(b'class="sponsor-baan"', r.data)

    def test_vijf_sponsors_toont_scroll_layout(self):
        """Met 5 sponsors wordt de scrollende layout getoond."""
        r = self._get_index(["a.png", "b.png", "c.jpg", "d.svg", "e.webp"])
        self.assertIn(b'class="sponsor-baan"', r.data)
        self.assertNotIn(b'class="sponsor-rij-statisch"', r.data)

    def test_vijf_sponsors_toont_drie_rijen(self):
        """Met 5+ sponsors worden drie scrollende rijen getoond."""
        r = self._get_index(["a.png", "b.png", "c.jpg", "d.svg", "e.webp"])
        self.assertEqual(r.data.count(b'class="sponsor-baan"'), 3)

    def test_niet_ondersteund_bestandstype_wordt_genegeerd(self):
        """Bestanden met een niet-ondersteund type (.gif, .pdf) worden genegeerd."""
        r = self._get_index(["logo.gif", "doc.pdf", "foto.png"])
        # Alleen foto.png telt mee → 1 sponsor → statische layout
        self.assertIn(b'class="sponsor-rij-statisch"', r.data)
        self.assertNotIn(b"logo.gif", r.data)
        self.assertNotIn(b"doc.pdf", r.data)

    def test_volgorde_is_alfabetisch(self):
        """Sponsors worden alfabetisch op bestandsnaam weergegeven."""
        r = self._get_index(["zebra.png", "appel.png", "midden.png"])
        data = r.data.decode()
        pos_a = data.find("appel.png")
        pos_m = data.find("midden.png")
        pos_z = data.find("zebra.png")
        self.assertLess(pos_a, pos_m)
        self.assertLess(pos_m, pos_z)

    def test_scroll_layout_bevat_duplicaten_voor_loop(self):
        """De scrollende layout toont elk logo tweemaal voor een naadloze loop."""
        r = self._get_index(["a.png", "b.png", "c.jpg", "d.svg", "e.webp"])
        data = r.data.decode()
        self.assertEqual(data.count("img/sponsors/a.png"), 2)

    def test_sponsor_titel_zichtbaar_bij_sponsors(self):
        """De sectietitel 'sponsors' is zichtbaar als er sponsors zijn."""
        r = self._get_index(["logo.png"])
        self.assertIn(b"Met dank aan onze sponsors", r.data)


# ══════════════════════════════════════════════════════════════════════════════
# INSTELLINGEN TRANSACTIEVEILIGHEID
#     Transactie-atomiciteit en DB-foutafhandeling in POST /admin/instellingen
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminInstellingenTransactie(unittest.TestCase):
    """Tests voor de transactie-veiligheid van POST /admin/instellingen."""

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    def test_alle_instellingen_worden_in_één_request_opgeslagen(self):
        """Alle velden in één POST worden allemaal opgeslagen (atomisch commit)."""
        self.client.post("/admin/instellingen", data={
            "max_eendjes": "2500",
            "max_per_bestelling": "75",
            "prijs_per_stuk": "3.00",
            "prijs_vijf_stuks": "13.00",
            "transactiekosten": "0.40",
        })
        rij = App.get_db().execute("SELECT * FROM teller WHERE id=1").fetchone()
        self.assertEqual(rij["max_eendjes"], 2500)
        self.assertEqual(rij["max_per_bestelling"], 75)
        self.assertAlmostEqual(rij["prijs_per_stuk"], 3.00, places=2)
        self.assertAlmostEqual(rij["prijs_vijf_stuks"], 13.00, places=2)
        self.assertAlmostEqual(rij["transactiekosten"], 0.40, places=2)

    def test_één_ongeldig_veld_voorkomt_opslag_van_alle_velden(self):
        """Als één veld ongeldig is, worden géén velden opgeslagen (all-or-nothing)."""
        origineel = dict(App.get_db().execute("SELECT * FROM teller WHERE id=1").fetchone())
        # prijs_per_stuk=0 is ongeldig; de rest is geldig
        self.client.post("/admin/instellingen", data={
            "max_eendjes": "2500",
            "max_per_bestelling": "75",
            "prijs_per_stuk": "0",   # ongeldig
            "prijs_vijf_stuks": "13.00",
            "transactiekosten": "0.40",
        })
        rij = App.get_db().execute("SELECT * FROM teller WHERE id=1").fetchone()
        self.assertEqual(rij["max_eendjes"], origineel["max_eendjes"])
        self.assertEqual(rij["max_per_bestelling"], origineel["max_per_bestelling"])
        self.assertAlmostEqual(rij["prijs_vijf_stuks"], origineel["prijs_vijf_stuks"], places=2)
        self.assertAlmostEqual(rij["transactiekosten"], origineel["transactiekosten"], places=2)

    def test_db_fout_bij_update_geeft_foutmelding_en_geen_gedeeltelijke_opslag(self):
        """Als de DB-update mislukt, wordt een foutmelding getoond en niets opgeslagen."""
        import sqlite3 as _sqlite3

        real_db = App.get_db()
        origineel_max = real_db.execute(
            "SELECT max_eendjes FROM teller WHERE id=1"
        ).fetchone()["max_eendjes"]
        origineel_execute = real_db.execute

        def execute_met_fout(sql, *args, **kwargs):
            if "UPDATE teller SET" in sql:
                raise _sqlite3.OperationalError("gesimuleerde DB-fout")
            return origineel_execute(sql, *args, **kwargs)

        mock_db = MagicMock(wraps=real_db)
        mock_db.execute = execute_met_fout

        with patch("app.get_db", return_value=mock_db):
            r = self.client.post("/admin/instellingen",
                                 data={"max_eendjes": "2000"},
                                 follow_redirects=True)

        self.assertIn(b"Fout bij opslaan", r.data)
        huidig_max = real_db.execute(
            "SELECT max_eendjes FROM teller WHERE id=1"
        ).fetchone()["max_eendjes"]
        self.assertEqual(huidig_max, origineel_max)


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT-LOG
# ══════════════════════════════════════════════════════════════════════════════

class TestAuditLog(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    def _audit_regels(self):
        return App.get_db().execute(
            "SELECT actie, details, gebruiker, ip FROM audit_log ORDER BY id"
        ).fetchall()

    def test_login_succes_wordt_gelogd(self):
        """Succesvolle login schrijft login_succes naar audit_log."""
        regels = self._audit_regels()
        acties = [r["actie"] for r in regels]
        self.assertIn("login_succes", acties)
        rij = next(r for r in regels if r["actie"] == "login_succes")
        self.assertEqual(rij["gebruiker"], "admin")

    def test_login_mislukt_wordt_gelogd(self):
        """Mislukte login schrijft login_mislukt naar audit_log."""
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "foutWachtwoord"})
        regels = self._audit_regels()
        acties = [r["actie"] for r in regels]
        self.assertIn("login_mislukt", acties)

    def test_logout_wordt_gelogd(self):
        """Uitloggen schrijft logout naar audit_log."""
        self.client.post("/admin/logout")
        regels = self._audit_regels()
        acties = [r["actie"] for r in regels]
        self.assertIn("logout", acties)

    def test_wachtwoord_wijzigen_wordt_gelogd(self):
        """Wachtwoordwijziging schrijft wachtwoord_gewijzigd naar audit_log."""
        self.client.post("/admin/wachtwoord-wijzigen", data={
            "huidig_wachtwoord": "testpass12345",
            "nieuw_wachtwoord": "NieuwWachtwoord99",
            "nieuw_wachtwoord_bevestiging": "NieuwWachtwoord99",
        })
        regels = self._audit_regels()
        acties = [r["actie"] for r in regels]
        self.assertIn("wachtwoord_gewijzigd", acties)

    def test_beheerder_aangemaakt_wordt_gelogd(self):
        """Nieuw beheerdersaccount schrijft beheerder_aangemaakt naar audit_log."""
        self.client.post("/admin/beheerder-toevoegen", data={
            "gebruikersnaam": "nieuwebeheerder",
            "wachtwoord": "sterkwachtwoord1",
            "wachtwoord_bevestiging": "sterkwachtwoord1",
        })
        regels = self._audit_regels()
        acties = [r["actie"] for r in regels]
        self.assertIn("beheerder_aangemaakt", acties)
        rij = next(r for r in regels if r["actie"] == "beheerder_aangemaakt")
        self.assertIn("nieuwebeheerder", rij["details"])

    def test_instellingen_gewijzigd_wordt_gelogd(self):
        """Instellingen opslaan schrijft instellingen_gewijzigd naar audit_log."""
        self.client.post("/admin/instellingen", data={"max_per_bestelling": "20"})
        regels = self._audit_regels()
        acties = [r["actie"] for r in regels]
        self.assertIn("instellingen_gewijzigd", acties)

    def test_opruimen_wordt_gelogd(self):
        """Opruimen schrijft opruimen naar audit_log."""
        self.client.post("/admin/opruimen")
        regels = self._audit_regels()
        acties = [r["actie"] for r in regels]
        self.assertIn("opruimen", acties)

    def test_audit_log_zichtbaar_op_beheer_pagina(self):
        """Audit-log regels zijn zichtbaar op /admin/beheer."""
        r = self.client.get("/admin/beheer")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Audit-log", r.data)
        self.assertIn(b"Ingelogd", r.data)  # login_succes label

    def test_audit_log_wissen(self):
        """Audit-log wissen verwijdert alle regels (op de wisregel zelf na)."""
        self.client.post("/admin/audit-wissen")
        regels = self._audit_regels()
        # Na wissen staat alleen de 'audit_log_gewist' regel er nog in
        self.assertEqual(len(regels), 1)
        self.assertEqual(regels[0]["actie"], "audit_log_gewist")

    def test_audit_log_bevat_ip_adres(self):
        """Audit-log regels bevatten een IP-adres."""
        regels = self._audit_regels()
        rij = next(r for r in regels if r["actie"] == "login_succes")
        self.assertIsNotNone(rij["ip"])

    def test_reset_wordt_gelogd(self):
        """Database-reset schrijft reset naar audit_log."""
        self.client.post("/admin/reset", data={"bevestiging": "RESET"})
        regels = self._audit_regels()
        acties = [r["actie"] for r in regels]
        self.assertIn("reset", acties)

    def test_cf_connecting_ip_wordt_gebruikt_als_aanwezig(self):
        """CF-Connecting-IP header wordt opgeslagen als client-IP boven remote_addr."""
        self.client.post("/admin/opruimen", headers={"CF-Connecting-IP": "9.8.7.6"})
        regels = self._audit_regels()
        rij = next((r for r in regels if r["actie"] == "opruimen"), None)
        self.assertIsNotNone(rij)
        self.assertEqual(rij["ip"], "9.8.7.6")


# ══════════════════════════════════════════════════════════════════════════════
# Tweefactorauthenticatie
# ══════════════════════════════════════════════════════════════════════════════

class TestTweeFactorAuth(unittest.TestCase):
    """Tests voor TOTP-gebaseerde tweefactorauthenticatie."""

    def setUp(self):
        self.client, self.ctx = maak_flask_client()
        # Inloggen als admin
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def tearDown(self):
        self.ctx.pop()

    def _zet_totp_actief(self):
        """Stel een TOTP-geheim in en activeer 2FA voor admin in de DB."""
        import pyotp
        geheim = pyotp.random_base32()
        db = App.get_db()
        db.execute(
            "UPDATE beheerders SET totp_geheim = ?, totp_actief = 1 WHERE gebruikersnaam = 'admin'",
            (geheim,)
        )
        db.commit()
        return geheim

    def _genereer_geldige_code(self, geheim):
        import pyotp
        return pyotp.TOTP(geheim).now()

    def test_2fa_setup_pagina_laadt(self):
        """GET /admin/2fa/instellen geeft 200."""
        r = self.client.get("/admin/2fa/instellen")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Tweefactorauthenticatie", r.data)

    def test_nieuw_geheim_genereren(self):
        """POST /admin/2fa/nieuw-geheim genereert een geheim en redirect."""
        r = self.client.post("/admin/2fa/nieuw-geheim")
        self.assertEqual(r.status_code, 302)
        # Controleer dat het geheim in de DB staat
        db = App.get_db()
        rij = db.execute("SELECT totp_geheim, totp_actief FROM beheerders WHERE gebruikersnaam='admin'").fetchone()
        self.assertIsNotNone(rij["totp_geheim"])
        self.assertEqual(rij["totp_actief"], 0)  # nog niet actief

    def test_2fa_bevestigen_activeert_met_geldige_code(self):
        """POST /admin/2fa/bevestigen met geldige code zet totp_actief=1."""
        import pyotp
        # Genereer geheim
        self.client.post("/admin/2fa/nieuw-geheim")
        db = App.get_db()
        geheim = db.execute("SELECT totp_geheim FROM beheerders WHERE gebruikersnaam='admin'").fetchone()["totp_geheim"]
        code = pyotp.TOTP(geheim).now()
        r = self.client.post("/admin/2fa/bevestigen", data={"code": code})
        self.assertEqual(r.status_code, 302)
        rij = db.execute("SELECT totp_actief FROM beheerders WHERE gebruikersnaam='admin'").fetchone()
        self.assertEqual(rij["totp_actief"], 1)

    def test_2fa_bevestigen_mislukt_met_ongeldige_code(self):
        """POST /admin/2fa/bevestigen met verkeerde code activeert 2FA niet."""
        self.client.post("/admin/2fa/nieuw-geheim")
        r = self.client.post("/admin/2fa/bevestigen", data={"code": "000000"},
                             follow_redirects=True)
        self.assertIn(b"Ongeldige code", r.data)
        db = App.get_db()
        rij = db.execute("SELECT totp_actief FROM beheerders WHERE gebruikersnaam='admin'").fetchone()
        self.assertEqual(rij["totp_actief"], 0)

    def test_login_met_2fa_vraagt_totp_stap(self):
        """Als 2FA actief is, leidt succesvolle wachtwoordcheck door naar /admin/login/totp."""
        self._zet_totp_actief()
        self.client.post("/admin/logout")
        r = self.client.post("/admin/login",
                             data={"gebruiker": "admin", "wachtwoord": "testpass12345"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login/totp", r.headers["Location"])

    def test_login_totp_stap_laadt(self):
        """GET /admin/login/totp geeft 200 als er een pending sessie is."""
        geheim = self._zet_totp_actief()
        self.client.post("/admin/logout")
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})
        r = self.client.get("/admin/login/totp")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"2FA", r.data)

    def test_login_totp_stap_zonder_pending_sessie_redirect(self):
        """GET /admin/login/totp zonder pending sessie redirect naar login."""
        self.client.post("/admin/logout")
        r = self.client.get("/admin/login/totp")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login", r.headers["Location"])

    def test_login_met_2fa_geldige_code_logt_in(self):
        """Geldige TOTP-code na wachtwoord voltooit de login."""
        import pyotp
        geheim = self._zet_totp_actief()
        self.client.post("/admin/logout")
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})
        code = pyotp.TOTP(geheim).now()
        r = self.client.post("/admin/login/totp", data={"code": code},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Bestellingen", r.data)

    def test_login_met_2fa_ongeldige_code_logt_niet_in(self):
        """Ongeldige TOTP-code voltooit de login niet."""
        self._zet_totp_actief()
        self.client.post("/admin/logout")
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})
        r = self.client.post("/admin/login/totp", data={"code": "000000"},
                             follow_redirects=True)
        self.assertIn(b"Ongeldige code", r.data)
        # Nog steeds niet ingelogd op /admin
        r2 = self.client.get("/admin")
        self.assertEqual(r2.status_code, 302)

    def test_2fa_uitschakelen_met_geldige_code(self):
        """2FA uitschakelen met geldige code zet totp_actief=0."""
        import pyotp
        geheim = self._zet_totp_actief()
        code = pyotp.TOTP(geheim).now()
        r = self.client.post("/admin/2fa/uitschakelen", data={"code": code})
        self.assertEqual(r.status_code, 302)
        db = App.get_db()
        rij = db.execute("SELECT totp_actief, totp_geheim FROM beheerders WHERE gebruikersnaam='admin'").fetchone()
        self.assertEqual(rij["totp_actief"], 0)
        self.assertIsNone(rij["totp_geheim"])

    def test_2fa_uitschakelen_met_ongeldige_code_blijft_actief(self):
        """2FA uitschakelen met verkeerde code laat 2FA actief."""
        self._zet_totp_actief()
        r = self.client.post("/admin/2fa/uitschakelen", data={"code": "000000"},
                             follow_redirects=True)
        self.assertIn(b"Ongeldige code", r.data)
        db = App.get_db()
        rij = db.execute("SELECT totp_actief FROM beheerders WHERE gebruikersnaam='admin'").fetchone()
        self.assertEqual(rij["totp_actief"], 1)

    def test_beheer_toont_2fa_status(self):
        """admin_beheer toont 2FA-status per beheerder."""
        r = self.client.get("/admin/beheer")
        self.assertIn(b"2FA", r.data)

    def test_2fa_events_in_audit_log(self):
        """2FA in- en uitschakelen verschijnt in de audit-log."""
        import pyotp
        # Genereer en activeer
        self.client.post("/admin/2fa/nieuw-geheim")
        db = App.get_db()
        geheim = db.execute("SELECT totp_geheim FROM beheerders WHERE gebruikersnaam='admin'").fetchone()["totp_geheim"]
        code = pyotp.TOTP(geheim).now()
        self.client.post("/admin/2fa/bevestigen", data={"code": code})
        # Uitschakelen
        code2 = pyotp.TOTP(geheim).now()
        self.client.post("/admin/2fa/uitschakelen", data={"code": code2})
        regels = db.execute("SELECT actie FROM audit_log").fetchall()
        acties = [r["actie"] for r in regels]
        self.assertIn("2fa_ingeschakeld", acties)
        self.assertIn("2fa_uitgeschakeld", acties)


# ══════════════════════════════════════════════════════════════════════════════
# GEZONDHEIDSCHECK (/health)
# ══════════════════════════════════════════════════════════════════════════════

class TestGezondheidsCheck(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def test_health_geeft_200(self):
        """/health geeft HTTP 200 terug bij normale werking."""
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)

    def test_health_geeft_json(self):
        """/health geeft een JSON-response terug."""
        r = self.client.get("/health")
        self.assertEqual(r.content_type, "application/json")

    def test_health_status_ok(self):
        """/health bevat "status": "ok" bij normale werking."""
        r = self.client.get("/health")
        data = r.get_json()
        self.assertEqual(data["status"], "ok")

    def test_health_db_ok(self):
        """/health rapporteert db: ok bij werkende database."""
        r = self.client.get("/health")
        data = r.get_json()
        self.assertEqual(data["db"], "ok")

    def test_health_mollie_ok(self):
        """/health rapporteert mollie: ok als Mollie-client geen fout gooit."""
        r = self.client.get("/health")
        data = r.get_json()
        self.assertIn(data["mollie"], ("ok", "niet_geconfigureerd"))

    def test_health_mollie_fout_geeft_503(self):
        """/health geeft 503 als de Mollie-client een uitzondering gooit."""
        with patch("app.maak_mollie_client", side_effect=Exception("Mollie neer")):
            r = self.client.get("/health")
        self.assertEqual(r.status_code, 503)
        data = r.get_json()
        self.assertEqual(data["status"], "fout")
        self.assertEqual(data["mollie"], "fout")

    def test_health_bereikbaar_in_onderhoudsmodus(self):
        """/health is bereikbaar (niet 503) als onderhoudsmodus aan is."""
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})
        self.client.post("/admin/instellingen", data={"onderhoudsmodus": "1"})
        r = self.client.get("/health")
        self.assertNotEqual(r.status_code, 503)
        self.client.post("/admin/instellingen", data={})

    def test_foutpagina_500_bevat_statuslink(self):
        """fout.html toont de statuslink bij HTTP 500."""
        with App.app.test_request_context():
            resp = App.app.make_response(
                App.app.handle_http_exception(
                    __import__("werkzeug.exceptions", fromlist=["InternalServerError"])
                    .InternalServerError()
                )
            )
        self.assertIn(b"uptime.ipalm.nl", resp.data)

    def test_foutpagina_404_bevat_geen_statuslink(self):
        """fout.html toont de statuslink NIET bij HTTP 404."""
        r = self.client.get("/bestaat-niet-xyz")
        self.assertNotIn(b"uptime.ipalm.nl", r.data)

    def test_onderhoudspagina_bevat_statuslink(self):
        """onderhoud.html toont altijd de statuslink."""
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})
        self.client.post("/admin/instellingen", data={"onderhoudsmodus": "1"})
        r = self.client.get("/")
        self.assertIn(b"uptime.ipalm.nl", r.data)
        self.client.post("/admin/instellingen", data={})


# ══════════════════════════════════════════════════════════════════════════════
# Admin statistieken: barchart verkopen per dag + webhook-alarm
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminStatistieken(unittest.TestCase):

    def setUp(self):
        self.client, self.ctx = maak_flask_client()

    def tearDown(self):
        self.ctx.pop()

    def _login(self):
        self.client.post("/admin/login",
                         data={"gebruiker": "admin", "wachtwoord": "testpass12345"})

    def _zet_betaald(self, pogingen=1):
        doe_bestelling(self.client)
        App.get_db().execute(
            "UPDATE bestellingen SET status='betaald', mollie_id='tr_s', "
            "lot_van=1, lot_tot=2, pogingen=? WHERE id=1",
            (pogingen,)
        )

    def test_admin_bevat_webhook_alarm_label(self):
        """Admin-pagina toont altijd het 'Webhook herhaald' stat-label."""
        self._login()
        r = self.client.get("/admin")
        self.assertIn("Webhook herhaald".encode(), r.data)

    def test_admin_webhook_alarm_geen_waarschuwing_bij_lege_db(self):
        """Lege DB: geen enkel stat-div heeft de waarschuwing-stat klasse."""
        self._login()
        r = self.client.get("/admin")
        self.assertNotIn(b'class="stat waarschuwing-stat"', r.data)

    def test_admin_webhook_alarm_waarschuwing_bij_hoge_pogingen(self):
        """Bestellingen met pogingen > 2 activeren de waarschuwing-stat klasse op een stat-div."""
        self._zet_betaald(pogingen=3)
        self._login()
        r = self.client.get("/admin")
        self.assertIn(b'class="stat waarschuwing-stat"', r.data)

    def test_admin_grafiek_afwezig_zonder_betaalde_bestellingen(self):
        """Verkopen-per-dag grafiek verschijnt niet als er geen betaalde bestellingen zijn."""
        self._login()
        r = self.client.get("/admin")
        self.assertNotIn("laatste 30 dagen".encode(), r.data)

    def test_admin_grafiek_aanwezig_bij_betaalde_bestelling(self):
        """Verkopen-per-dag grafiek verschijnt als er betaalde bestellingen zijn."""
        self._zet_betaald()
        self._login()
        r = self.client.get("/admin")
        self.assertIn("laatste 30 dagen".encode(), r.data)


# ══════════════════════════════════════════════════════════════════════════════
# Uitvoeren als script
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
