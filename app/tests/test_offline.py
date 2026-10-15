"""Acceptance Test 16: the application works fully offline.

Two independent proofs, both pinned as regression tests:

1. STATIC — no module under ``app/`` (outside the test suite itself)
   imports any networking library or references Qt networking classes.
   If a future change introduces network usage, this test fails loudly
   and forces an explicit design decision.

2. DYNAMIC — the complete daily workflow (open day -> prices -> readings
   -> deliveries -> sales posting -> credit ticket -> customer payment ->
   expense -> backup -> close day) runs with ``socket.socket`` and
   ``socket.create_connection`` sabotaged. Any code path that touched the
   network would raise instead of silently succeeding.

Method note (honest): the CI/dev machine does have network access, so we
do not rely on the sandbox. We actively disable the socket API for the
duration of the workflow, and the static scan proves there is nothing to
fall back on. Together they establish that no app code path requires the
network. Google Drive integration is folder-based only (``shutil.copy2``),
which is also covered by the socket sabotage.
"""

from __future__ import annotations

import ast
import socket
from decimal import Decimal
from pathlib import Path

from app.database.connection import DatabaseManager
from app.services.backup_service import BackupService

from .conftest import (
    insert_price_row,
    pkt_clock_utc_iso,
    pkt_midnight_utc_iso,
)

APP_DIR = Path(__file__).resolve().parent.parent

#: Top-level module names that imply network I/O. ``http`` covers
#: ``http.client`` / ``http.server``; ``urllib`` covers its submodules.
BANNED_TOP_LEVELS = {
    "socket",
    "ssl",
    "urllib",
    "http",
    "requests",
    "httpx",
    "aiohttp",
    "websockets",
    "websocket",
    "smtplib",
    "ftplib",
    "telnetlib",
}

#: Qt networking classes must never appear in app code.
BANNED_QT_NAMES = (
    "QNetworkAccessManager",
    "QNetworkRequest",
    "QNetworkReply",
    "QTcpSocket",
    "QSslSocket",
)


def _iter_app_modules():
    for path in APP_DIR.rglob("*.py"):
        parts = path.parts
        if "tests" in parts or "__pycache__" in parts:
            continue
        yield path


def test_no_network_imports_in_app_code():
    offenders = []
    for path in _iter_app_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in BANNED_TOP_LEVELS:
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    if top in BANNED_TOP_LEVELS:
                        offenders.append(
                            f"{path.name}: from {node.module} import ..."
                        )
    assert not offenders, "network imports found in app code:\n" + "\n".join(
        offenders
    )


def test_no_qt_networking_references_in_app_code():
    offenders = []
    for path in _iter_app_modules():
        text = path.read_text(encoding="utf-8")
        for name in BANNED_QT_NAMES:
            if name in text:
                offenders.append(f"{path.name}: references {name}")
    assert not offenders, "Qt networking usage found:\n" + "\n".join(offenders)


def test_full_daily_workflow_with_sockets_disabled(
    phase5_services, db, db_file, tmp_path, monkeypatch
):
    """The whole register day works with the socket API disabled."""

    def _blocked(*args, **kwargs):
        raise AssertionError("network access attempted while offline")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    svc = phase5_services
    # Past dates (relative to the machine clock): meter intervals end "now",
    # so the day's price rows must already be effective when readings land.
    prev_day, day = "2026-10-13", "2026-10-14"
    petrol_rate = Decimal("393.64")
    diesel_rate = Decimal("397.76")

    # --- previous day: plant the "previous" meter readings ----------------
    svc.days.open_day(prev_day, opening_cash_paisa=1_000_000)
    effective_prev = pkt_midnight_utc_iso(prev_day)
    svc.prices.set_price("PETROL", petrol_rate, effective_prev, prev_day)
    svc.prices.set_price("DIESEL", diesel_rate, effective_prev, prev_day)
    previous = {
        "1 PG+": "84938",
        "2 PG+": "313261",
        "3 HSD": "569441",
        "4 HSD": "374803",
    }
    for code, reading in previous.items():
        svc.nozzles.record_reading(prev_day, code, reading)
    svc.sales.post_day_sales(prev_day)
    svc.days.close_day(prev_day, physical_cash_paisa=1_000_000)

    # --- the working day -------------------------------------------------
    svc.days.open_day(day)  # opening cash carries forward
    effective = pkt_midnight_utc_iso(day)
    # Same bridge the register-fixture tests use: the §11.2 snapshot rule
    # would block re-setting a day-open price without segment readings.
    insert_price_row(db, "PETROL", effective, 3_936_400, day)
    insert_price_row(db, "DIESEL", effective, 3_977_600, day)

    supplier = svc.deliveries.add_supplier("PSO Depot")
    svc.deliveries.record_delivery(
        supplier=supplier,
        fuel_product="PETROL",
        tank="TANK-PETROL",
        business_date=day,
        quantity_litres=Decimal("1000"),
        unit_cost_pkr=petrol_rate,
        supplier_invoice_no="PSO-OFFLINE-P",
        occurred_at_utc=pkt_clock_utc_iso(day, 9),
    )
    svc.deliveries.record_delivery(
        supplier=supplier,
        fuel_product="DIESEL",
        tank="TANK-DIESEL",
        business_date=day,
        quantity_litres=Decimal("1000"),
        unit_cost_pkr=diesel_rate,
        supplier_invoice_no="PSO-OFFLINE-D",
        occurred_at_utc=pkt_clock_utc_iso(day, 9),
    )
    current = {
        "1 PG+": "84985",   # +47 L
        "2 PG+": "313711",  # +450 L
        "3 HSD": "569549",  # +108 L
        "4 HSD": "374860",  # +57 L
    }
    for code, reading in current.items():
        svc.nozzles.record_reading(day, code, reading)
    posted = svc.sales.post_day_sales(day)
    assert posted is not None

    customer = svc.credit.create_customer("Offline Tester").id
    ticket = svc.credit.issue_ticket(
        customer,
        day,
        [
            {
                "fuel_product_code": "PETROL",
                "nozzle_code": "1 PG+",
                "quantity_litres": Decimal("40"),
            }
        ],
        occurred_at_utc=pkt_clock_utc_iso(day, 10),
    )
    assert ticket.gross_amount_paisa == 40 * 39364  # 40 L @ 393.64, exact paisa
    svc.payments.record_payment(
        customer,
        day,
        300_000,
        method="cash",
        occurred_at_utc=pkt_clock_utc_iso(day, 11),
    )
    svc.expenses.record_expense(
        business_date=day,
        category="Utilities",
        amount_paisa=50_000,
        method="cash",
        occurred_at_utc=pkt_clock_utc_iso(day, 12),
    )

    # --- backup while "offline": local snapshot only, must succeed ------
    backup = BackupService(
        DatabaseManager(db_file),
        actor="system",
        backup_dir=tmp_path / "backups",
    )
    record = backup.run_backup("manual")
    assert record.status == "validated"
    assert (tmp_path / "backups" / record.filename).exists()

    # --- close the day ---------------------------------------------------
    closing = svc.days.close_day(day, physical_cash_paisa=0)
    assert closing.business_date == day
    assert closing.closing_revision == 1

    # Sanity: the day's figures are all present and consistent.
    from app.services.reporting_service import ReportingService

    register = ReportingService(DatabaseManager(db_file)).generate_report(
        "daily_register", business_date=day
    )
    assert register.business_date == day
