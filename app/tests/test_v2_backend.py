"""v2 backend tests: auth (migration 016), other-income total, per-fuel stock.

Covers the new v2 surface::

    AuthService.needs_setup / setup_owner / authenticate /
        change_password / security_questions / reset_password
    CashbookService.other_income_total
    InventoryService.fuel_stock_summary

plus the migration-016 trigger backstops (users timestamps, no-delete-last)
through raw SQL on ``db2``.

Design rules pinned here: scrypt-hashed passwords (never plaintext),
failed-attempt lockout (5 → 15 min), case-insensitive usernames, security
answers case-insensitive, "other income" = credit_recovery + other_receipt
(net of reversals, never revenue), per-fuel stock = aggregation of the
per-tank positions (physical/variance None until every tank is dipped).
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from app.services import errors
from app.services.auth_service import AuthError, AuthService

from .conftest import (
    phase3_services,  # noqa: F401  (fixture)
    phase5_services,  # noqa: F401  (fixture)
    pkt_clock_utc_iso,
    pkt_midnight_utc_iso,
    seed_phase3_day,
)


def _setup_two_days(svc):
    """PREV (closed) with 5000 L opening stock for BOTH fuels, then open DAY.

    ``close_prior_day`` only seeds PETROL opening stock, so this helper
    does the first-day setup manually to cover diesel too.
    """
    svc.days.open_day(PREV, opening_cash_paisa=2_000_000)
    effective = pkt_midnight_utc_iso(PREV)
    svc.prices.set_price("PETROL", Decimal("250"), effective, PREV)
    svc.prices.set_price("DIESEL", Decimal("260"), effective, PREV)
    svc.inventory.set_opening_stock("PETROL", Decimal("5000"), PREV)
    svc.inventory.set_opening_stock("DIESEL", Decimal("5000"), PREV)
    for code, litres in PREV_READINGS.items():
        svc.nozzles.record_reading(PREV, code, litres)
    svc.sales.post_day_sales(PREV)
    svc.days.close_day(PREV, physical_cash_paisa=2_000_000)
    return seed_phase3_day(svc, DAY)  # supplier id; prices+stock carry forward

DAY = "2026-10-15"
PREV = "2026-10-14"
PREV_READINGS = {
    "1 PG+": "84938",
    "2 PG+": "81234",
    "3 HSD": "90123",
    "4 HSD": "88456",
}

SETUP = dict(
    username="owner",
    password="StrongPass123",
    security_q1="First school name?",
    security_a1="Greenwood",
    security_q2="Mother's birth city?",
    security_a2="Lahore",
)


@pytest.fixture
def auth(db, db_file):
    """AuthService on a migrated temp database (``db`` runs migrations)."""
    from app.database.connection import DatabaseManager

    return AuthService(DatabaseManager(db_file))


def _setup(auth: AuthService, **overrides):
    kwargs = dict(SETUP)
    kwargs.update(overrides)
    return auth.setup_owner(**kwargs)


# ===========================================================================
# migration 016: users table + triggers
# ===========================================================================


class TestUsersTable:
    def test_schema_version_is_16(self, db):
        from app.database.migrator import get_schema_version

        assert get_schema_version(db) == 16

    def test_users_triggers_exist(self, db):
        names = {
            r["name"]
            for r in db.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'trigger' AND tbl_name = 'users'"
            )
        }
        assert "trg_users_bu_guard_created" in names
        assert "trg_users_bu_touch_updated" in names
        assert "trg_users_bd_no_delete_last" in names

    def test_created_at_immutable(self, db, auth):
        _setup(auth)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE users SET created_at_utc = '2000-01-01T00:00:00.000Z'"
            )

    def test_cannot_delete_last_user(self, db, auth):
        _setup(auth)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM users")

    def test_username_case_insensitive_unique(self, db, auth):
        _setup(auth)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO users (uuid, username, password_hash, password_salt,"
                " role, security_q1, security_a1_hash, security_a1_salt,"
                " security_q2, security_a2_hash, security_a2_salt)"
                " VALUES ('x2', 'OWNER', 'h', 's', 'owner',"
                " 'q1', 'h1', 's1', 'q2', 'h2', 's2')"
            )


# ===========================================================================
# AuthService
# ===========================================================================


class TestAuthSetup:
    def test_needs_setup_true_on_fresh_db(self, auth):
        assert auth.needs_setup() is True

    def test_setup_then_needs_setup_false(self, auth):
        user = _setup(auth)
        assert user.username == "owner"
        assert user.role == "owner"
        assert auth.needs_setup() is False

    def test_second_setup_blocked(self, auth):
        _setup(auth)
        with pytest.raises(errors.BusinessRuleError):
            _setup(auth, username="someone_else")

    def test_blank_username_rejected(self, auth):
        with pytest.raises(errors.ValidationError):
            _setup(auth, username="   ")

    def test_short_password_rejected(self, auth):
        with pytest.raises(errors.ValidationError):
            _setup(auth, password="short7")

    def test_duplicate_security_questions_rejected(self, auth):
        with pytest.raises(errors.ValidationError):
            _setup(auth, security_q2=SETUP["security_q1"])

    def test_blank_security_answer_rejected(self, auth):
        with pytest.raises(errors.ValidationError):
            _setup(auth, security_a2="  ")

    def test_password_never_stored_plaintext(self, db, auth):
        _setup(auth)
        row = db.execute("SELECT * FROM users").fetchone()
        assert SETUP["password"] not in row["password_hash"]
        assert SETUP["password"] not in row["password_salt"]
        assert len(row["password_hash"]) == 64  # 32-byte scrypt hex
        assert len(row["password_salt"]) == 32  # 16-byte salt hex

    def test_answers_never_stored_plaintext(self, db, auth):
        _setup(auth)
        row = db.execute("SELECT * FROM users").fetchone()
        assert "greenwood" not in row["security_a1_hash"].lower()
        assert "lahore" not in row["security_a2_hash"].lower()


class TestAuthenticate:
    def test_success(self, auth):
        _setup(auth)
        user = auth.authenticate("owner", SETUP["password"])
        assert user.username == "owner"
        assert user.last_login_at_utc is not None

    def test_wrong_password(self, auth):
        _setup(auth)
        with pytest.raises(AuthError):
            auth.authenticate("owner", "WrongPass999")

    def test_unknown_user_same_error(self, auth):
        # No user enumeration: unknown username -> same message/key.
        _setup(auth)
        with pytest.raises(AuthError) as e1:
            auth.authenticate("nobody", "whatever123")
        with pytest.raises(AuthError) as e2:
            auth.authenticate("owner", "whatever123")
        assert e1.value.message_key == e2.value.message_key
        assert str(e1.value) == str(e2.value)

    def test_username_case_insensitive(self, auth):
        _setup(auth)
        assert auth.authenticate("OWNER", SETUP["password"]).username == "owner"

    def test_lockout_after_five_failures(self, auth):
        _setup(auth)
        for _ in range(5):
            with pytest.raises(AuthError):
                auth.authenticate("owner", "WrongPass999")
        # Even the correct password is rejected while locked.
        with pytest.raises(AuthError) as exc:
            auth.authenticate("owner", SETUP["password"])
        assert exc.value.message_key == "err.auth_locked"

    def test_failed_counter_persists(self, db, auth):
        _setup(auth)
        for _ in range(3):
            with pytest.raises(AuthError):
                auth.authenticate("owner", "WrongPass999")
        # Fresh read on the test connection: the counter was committed.
        db2_row = db.execute("SELECT failed_attempts FROM users").fetchone()
        assert int(db2_row["failed_attempts"]) == 3

    def test_success_resets_counter(self, db, auth):
        _setup(auth)
        with pytest.raises(AuthError):
            auth.authenticate("owner", "WrongPass999")
        auth.authenticate("owner", SETUP["password"])
        row = db.execute(
            "SELECT failed_attempts, locked_until_utc FROM users"
        ).fetchone()
        assert int(row["failed_attempts"]) == 0
        assert row["locked_until_utc"] is None


class TestChangePassword:
    def test_change_password(self, auth):
        _setup(auth)
        auth.change_password("owner", SETUP["password"], "EvenStronger789")
        auth.authenticate("owner", "EvenStronger789")
        with pytest.raises(AuthError):
            auth.authenticate("owner", SETUP["password"])

    def test_wrong_current_password(self, auth):
        _setup(auth)
        with pytest.raises(AuthError):
            auth.change_password("owner", "WrongPass999", "EvenStronger789")

    def test_weak_new_password_rejected(self, auth):
        _setup(auth)
        with pytest.raises(errors.ValidationError):
            auth.change_password("owner", SETUP["password"], "short")


class TestPasswordReset:
    def test_security_questions_returned(self, auth):
        _setup(auth)
        q1, q2 = auth.security_questions("owner")
        assert q1 == SETUP["security_q1"]
        assert q2 == SETUP["security_q2"]

    def test_reset_with_correct_answers(self, auth):
        _setup(auth)
        auth.reset_password(
            "owner", SETUP["security_a1"], SETUP["security_a2"], "BrandNewPass1"
        )
        auth.authenticate("owner", "BrandNewPass1")

    def test_answers_case_insensitive(self, auth):
        _setup(auth)
        auth.reset_password("owner", "  GREENWOOD ", "lahore", "BrandNewPass1")
        auth.authenticate("owner", "BrandNewPass1")

    def test_wrong_answer_rejected(self, auth):
        _setup(auth)
        with pytest.raises(AuthError):
            auth.reset_password(
                "owner", "wrong", SETUP["security_a2"], "BrandNewPass1"
            )
        # Password unchanged.
        auth.authenticate("owner", SETUP["password"])

    def test_reset_clears_lockout(self, auth):
        _setup(auth)
        for _ in range(5):
            with pytest.raises(AuthError):
                auth.authenticate("owner", "WrongPass999")
        auth.reset_password(
            "owner", SETUP["security_a1"], SETUP["security_a2"], "BrandNewPass1"
        )
        auth.authenticate("owner", "BrandNewPass1")


# ===========================================================================
# CashbookService.other_income_total
# ===========================================================================


class TestOtherIncomeTotal:
    def _open(self, svc, day=DAY):
        svc.days.open_day(day, opening_cash_paisa=1_000_000)
        return svc

    def test_sums_recovery_and_other_receipt(self, phase5_services):
        svc = self._open(phase5_services)
        stamp = pkt_clock_utc_iso(DAY, 10)
        svc.cash.record_receipt(
            business_date=DAY,
            category="credit_recovery",
            amount_paisa=50_000,
            remarks="old debt",
            occurred_at_utc=stamp,
        )
        svc.cash.record_receipt(
            business_date=DAY,
            category="other_receipt",
            amount_paisa=25_000,
            occurred_at_utc=stamp,
        )
        svc.cash.record_receipt(
            business_date=DAY,
            category="cash_sales",
            amount_paisa=100_000,
            occurred_at_utc=stamp,
        )
        total = svc.cash.other_income_total(DAY)
        assert total["credit_recovery_paisa"] == 50_000
        assert total["other_receipt_paisa"] == 25_000
        assert total["total_paisa"] == 75_000

    def test_empty_day_is_zero(self, phase5_services):
        svc = self._open(phase5_services)
        total = svc.cash.other_income_total(DAY)
        assert total["total_paisa"] == 0
        assert total["recoveries"] == []

    def test_reversal_nets_to_zero(self, phase5_services):
        svc = self._open(phase5_services)
        stamp = pkt_clock_utc_iso(DAY, 10)
        receipt = svc.cash.record_receipt(
            business_date=DAY,
            category="credit_recovery",
            amount_paisa=50_000,
            occurred_at_utc=stamp,
        )
        assert svc.cash.other_income_total(DAY)["total_paisa"] == 50_000
        svc.cash.reverse_cash_receipt(receipt.uuid, reason="entered by mistake")
        assert svc.cash.other_income_total(DAY)["total_paisa"] == 0

    def test_recoveries_list_names_customers(self, phase5_services):
        svc = self._open(phase5_services)
        stamp = pkt_clock_utc_iso(DAY, 10)
        cust_id = svc.credit.create_customer("Ahmed Khan").id
        svc.cash.record_receipt(
            business_date=DAY,
            category="credit_recovery",
            amount_paisa=30_000,
            customer_id=cust_id,
            occurred_at_utc=stamp,
        )
        total = svc.cash.other_income_total(DAY)
        assert len(total["recoveries"]) == 1
        assert total["recoveries"][0]["customer_name"] == "Ahmed Khan"
        assert total["recoveries"][0]["amount_paisa"] == 30_000

    def test_other_days_excluded(self, phase5_services):
        svc = phase5_services
        svc.days.open_day(PREV, opening_cash_paisa=1_000_000)
        svc.cash.record_receipt(
            business_date=PREV,
            category="other_receipt",
            amount_paisa=99_000,
            occurred_at_utc=pkt_clock_utc_iso(PREV, 10),
        )
        # DAY was never opened — no receipts exist for it.
        assert svc.cash.other_income_total(DAY)["total_paisa"] == 0
        assert svc.cash.other_income_total(PREV)["total_paisa"] == 99_000


# ===========================================================================
# InventoryService.fuel_stock_summary
# ===========================================================================


class TestFuelStockSummary:
    def test_per_fuel_aggregation(self, phase3_services):
        svc = phase3_services
        supplier = _setup_two_days(svc)
        # New stock arrives today: 2000 L petrol, 1000 L diesel.
        svc.deliveries.record_delivery(
            supplier=supplier,
            fuel_product="PETROL",
            tank="TANK-PETROL",
            business_date=DAY,
            quantity_litres=Decimal("2000"),
            unit_cost_pkr=Decimal("250.0000"),
            occurred_at_utc=pkt_clock_utc_iso(DAY, 9),
        )
        svc.deliveries.record_delivery(
            supplier=supplier,
            fuel_product="DIESEL",
            tank="TANK-DIESEL",
            business_date=DAY,
            quantity_litres=Decimal("1000"),
            unit_cost_pkr=Decimal("260.0000"),
            occurred_at_utc=pkt_clock_utc_iso(DAY, 9),
        )
        summary = {
            row["fuel_product_code"]: row
            for row in svc.inventory.fuel_stock_summary(DAY)
        }
        petrol = summary["PETROL"]
        diesel = summary["DIESEL"]
        # Opening 5000 L each (carried forward), no sales yet.
        assert petrol["opening_ml"] == 5_000_000
        assert petrol["received_ml"] == 2_000_000
        assert petrol["sold_ml"] == 0
        assert petrol["theoretical_ml"] == 7_000_000
        assert diesel["opening_ml"] == 5_000_000
        assert diesel["received_ml"] == 1_000_000
        assert diesel["theoretical_ml"] == 6_000_000

    def test_physical_none_until_all_tanks_dipped(self, phase3_services):
        svc = phase3_services
        _setup_two_days(svc)
        before = {
            row["fuel_product_code"]: row
            for row in svc.inventory.fuel_stock_summary(DAY)
        }
        assert before["PETROL"]["physical_ml"] is None
        assert before["PETROL"]["variance_ml"] is None
        # Dip the single petrol tank.
        svc.inventory.record_physical_count(
            "TANK-PETROL",
            DAY,
            Decimal("6900"),
        )
        after = {
            row["fuel_product_code"]: row
            for row in svc.inventory.fuel_stock_summary(DAY)
        }
        assert after["PETROL"]["physical_ml"] == 6_900_000
        assert (
            after["PETROL"]["variance_ml"]
            == 6_900_000 - after["PETROL"]["theoretical_ml"]
        )
        # Diesel still undipped.
        assert after["DIESEL"]["physical_ml"] is None

    def test_sold_liters_included(self, phase3_services):
        svc = phase3_services
        _setup_two_days(svc)
        # Sell 100 L through nozzle 1 (petrol): 84938 -> 85038.
        svc.nozzles.record_reading(DAY, "1 PG+", "85038")
        svc.sales.post_day_sales(DAY)
        summary = {
            row["fuel_product_code"]: row
            for row in svc.inventory.fuel_stock_summary(DAY)
        }
        assert summary["PETROL"]["sold_ml"] == 100_000
        assert summary["PETROL"]["theoretical_ml"] == 4_900_000
        assert summary["DIESEL"]["sold_ml"] == 0
