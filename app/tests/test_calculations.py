"""Unit tests for app/calculations/money.py — the single implementation of
all money/quantity math.

Design plan §5.2, §10 acceptance test 20 (financial calculations) and the
Test-1 quantity check: 84,985 L - 84,938 L = 47,000 ml = 47 L.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.calculations.money import (
    amount_paisa_for,
    format_pkr,
    from_ml,
    from_paisa,
    to_ml,
    to_paisa,
)


# --- paisa <-> PKR -----------------------------------------------------------
def test_to_paisa_from_paisa_round_trip():
    assert to_paisa("1234.56") == 123456
    assert from_paisa(123456) == Decimal("1234.56")
    assert from_paisa(to_paisa(Decimal("99.99"))) == Decimal("99.99")


def test_to_paisa_rounds_half_up_once():
    # Round-half-up applied once at the boundary — never banker's rounding.
    assert to_paisa("0.005") == 1
    assert to_paisa("2.675") == 268
    assert to_paisa("2.674") == 267


def test_to_paisa_rejects_float():
    with pytest.raises(TypeError):
        to_paisa(12.34)


def test_from_paisa_rejects_non_int():
    with pytest.raises(TypeError):
        from_paisa(12.34)
    with pytest.raises(TypeError):
        from_paisa(True)


def test_to_paisa_rejects_negative():
    with pytest.raises(ValueError):
        to_paisa(-1)
    with pytest.raises(ValueError):
        to_paisa("-0.01")


# --- milliliters <-> litres --------------------------------------------------
def test_to_ml_from_ml_round_trip():
    assert to_ml("47") == 47000
    assert from_ml(47000) == Decimal("47.000")
    assert from_ml(to_ml(Decimal("3.141"))) == Decimal("3.141")


def test_spec_test1_quantity_math():
    """84,985 L - 84,938 L = 47,000 ml = 47 L exactly."""
    sold_ml = 84_985_000 - 84_938_000
    assert sold_ml == 47_000
    assert from_ml(sold_ml) == Decimal("47.000")


def test_to_ml_rejects_float_and_negative():
    with pytest.raises(TypeError):
        to_ml(47.5)
    with pytest.raises(ValueError):
        to_ml(-5)
    with pytest.raises(TypeError):
        from_ml(47.0)


# --- canonical sale amount ---------------------------------------------------
def test_amount_paisa_for_spec_example():
    """§5.2: 47 L @ PKR 265.9630 -> 47000 * 2659630 / 100000 = 1250026.1
    -> 1,250,026 paisa (PKR 12,500.26)."""
    assert amount_paisa_for(47000, 2659630) == 1_250_026


def test_amount_paisa_for_ticket_example():
    """§6 worked example: 40 L @ PKR 270 = PKR 10,800 gross."""
    assert amount_paisa_for(40_000, 2_700_000) == 1_080_000


def test_amount_paisa_for_rounds_half_up():
    # 1 ml @ 0.0001 PKR/L -> 0.0000001 PKR -> 0.00001 paisa -> rounds to 0.
    assert amount_paisa_for(1, 1) == 0
    # 50000 ml @ 0.0001 PKR/L -> 0.005 PKR -> 0.5 paisa -> half-up to 1.
    assert amount_paisa_for(50_000, 1) == 1


def test_amount_paisa_for_type_and_value_rules():
    with pytest.raises(TypeError):
        amount_paisa_for(47000.0, 2659630)
    with pytest.raises(TypeError):
        amount_paisa_for(47000, "2659630")
    with pytest.raises(TypeError):
        amount_paisa_for(True, 2659630)
    with pytest.raises(ValueError):
        amount_paisa_for(-1, 2659630)
    with pytest.raises(ValueError):
        amount_paisa_for(47000, -1)


# --- display -----------------------------------------------------------------
def test_format_pkr():
    assert format_pkr(123456) == "1,234.56"
    assert format_pkr(0) == "0.00"
    assert format_pkr(1_250_026) == "12,500.26"
