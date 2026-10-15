"""Fuel price history: an immutable per-product timeline (Phase 3).

Design plan §11.2: a price change takes effect only after the manager
records a meter snapshot reading (``reading_kind='segment'``) on every
active nozzle of that product, taken before the change's effective time.
Each sale interval is then priced by the ``price_history`` row effective at
its ``occurred_at_utc`` — today's price is never applied retrospectively,
and old rows (with the sales priced from them) are never touched.
"""

from __future__ import annotations

from app.calculations import money as calc
from app.calculations import time as timecalc
from app.models.price import PriceInfo
from app.repositories.nozzle_repository import NozzleRepository
from app.repositories.price_repository import PriceRepository
from app.services import errors
from app.services._base import ServiceBase, require_writable_day


def resolve_fuel_product(conn, product: str | int):
    """Fuel-product row by id (int) or exact code (str, case-sensitive).

    Shared by the Phase-3 services so product resolution behaves
    identically everywhere. Raises :class:`NotFoundError` when unknown,
    :class:`ValidationError` for a malformed reference.
    """
    if isinstance(product, bool):
        raise errors.ValidationError(
            f"Invalid fuel product reference: {product!r}.",
            key="err.invalid_product_ref",
            params={"ref": product},
        )
    if isinstance(product, int):
        row = PriceRepository.get_product(conn, product)
    elif isinstance(product, str):
        code = product.strip()
        if not code:
            raise errors.ValidationError(
                "Fuel product code must not be empty.",
                key="err.product_code_empty",
            )
        row = PriceRepository.get_product_by_code(conn, code)
    else:
        raise errors.ValidationError(
            f"Invalid fuel product reference: {product!r}.",
            key="err.invalid_product_ref",
            params={"ref": product},
        )
    if row is None:
        raise errors.NotFoundError(
            f"Unknown fuel product: {product!r}.",
            key="err.unknown_product",
            params={"ref": product},
        )
    return row


def _to_price_info(row) -> PriceInfo:
    return PriceInfo(
        id=int(row["id"]),
        fuel_product_id=int(row["fuel_product_id"]),
        fuel_product_code=row["fuel_product_code"],
        effective_from_utc=row["effective_from_utc"],
        unit_price_10k=int(row["unit_price_10k"]),
        business_date=row["business_date"],
        created_by=row["created_by"],
    )


class PriceService(ServiceBase):
    """Reads and appends the immutable fuel-price timeline."""

    def __init__(self, db_manager, actor: str | None = None) -> None:
        super().__init__(db_manager, actor)
        self._prices = PriceRepository()
        self._nozzles = NozzleRepository()

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def list_fuel_products(self, *, active_only: bool = True) -> list[dict]:
        """Fuel products as ``{id, code, name_en}`` dicts (never hard-coded
        names in callers)."""
        with self.read_connection() as conn:
            rows = self._prices.list_products(conn)
        result = []
        for row in rows:
            if active_only and not row["is_active"]:
                continue
            result.append(
                {
                    "id": int(row["id"]),
                    "code": row["code"],
                    "name_en": row["name_en"],
                }
            )
        return result

    def _resolve_product(self, conn, product: str | int):
        return resolve_fuel_product(conn, product)

    def get_price(
        self, product: str | int, at_utc: str
    ) -> PriceInfo | None:
        """The price effective at *at_utc*, or None when no price exists yet.

        *at_utc* must be a ``YYYY-MM-DDTHH:MM:SS.SSSZ`` timestamp.
        """
        stamp = self._require_timestamp(at_utc)
        with self.read_connection() as conn:
            prod = self._resolve_product(conn, product)
            return self._get_effective_conn(conn, int(prod["id"]), stamp)

    def price_effective_at_day_end(
        self, product: str | int, business_date: str
    ) -> PriceInfo | None:
        """The price effective at the last millisecond of *business_date*
        (23:59:59.999 Pakistan time) — the "today's price" answer."""
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        stamp = timecalc.karachi_end_of_day_utc(business_date)
        with self.read_connection() as conn:
            prod = self._resolve_product(conn, product)
            return self._get_effective_conn(conn, int(prod["id"]), stamp)

    def list_prices(self, product: str | int) -> list[PriceInfo]:
        """A product's price timeline, newest effective time first."""
        with self.read_connection() as conn:
            prod = self._resolve_product(conn, product)
            rows = self._prices.list_for_product(conn, int(prod["id"]))
        return [_to_price_info(row) for row in rows]

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    def set_price(
        self,
        product: str | int,
        price_pkr,
        effective_from_utc: str,
        business_date: str,
        *,
        actor: str | None = None,
    ) -> int:
        """Append a new price row for a product.

        Guards (before any write):
        * price > 0, timestamp well-formed, day exists and is writable;
        * no price already recorded at that exact time (DuplicateError);
        * no backdating: the new time must not precede the latest row
          (ValidationError);
        * §11.2 snapshot rule: when a prior price exists, every ACTIVE
          nozzle of the product needs a ``segment`` reading for
          *business_date* taken at or before the effective time, else the
          change is blocked naming the nozzles.

        Old rows are never touched. Returns the new row id.
        """
        try:
            price_10k = calc.to_price_10k(price_pkr)
        except (TypeError, ValueError) as exc:
            raise errors.ValidationError(
                f"Invalid price: {exc}",
                key="err.invalid_price_detail",
                params={"detail": str(exc)},
            ) from exc
        if price_10k <= 0:
            raise errors.ValidationError(
                "The price must be greater than zero.",
                key="err.price_must_be_positive",
            )
        stamp = self._require_timestamp(effective_from_utc)
        if not timecalc.is_valid_business_date(business_date):
            raise errors.ValidationError(
                f"Invalid business date: {business_date!r}.",
                key="err.invalid_business_date",
                params={"day_date": business_date},
            )
        who = self.resolve_actor(actor)

        with self.transaction() as conn:
            prod = self._resolve_product(conn, product)
            product_id = int(prod["id"])
            require_writable_day(conn, business_date)
            if self._prices.exists_at(conn, product_id, stamp):
                raise errors.DuplicateError(
                    "A price is already recorded for this product at that"
                    " exact time.",
                    key="err.price_already_recorded",
                )
            latest = self._prices.latest(conn, product_id)
            if latest is not None and stamp < latest["effective_from_utc"]:
                raise errors.ValidationError(
                    "Prices cannot be backdated: a price is already effective"
                    f" from {latest['effective_from_utc']}. Post the new price"
                    " with a later effective time.",
                    key="err.price_backdated",
                    params={"stamp": latest["effective_from_utc"]},
                )
            # §11.2 snapshot rule: whenever a prior price row exists, every
            # ACTIVE nozzle of the product needs a ``segment`` snapshot
            # reading for business_date taken at or before the change's
            # effective time — including a day-open price. Unchanged prices
            # simply carry forward (no re-setting needed).
            if latest is not None:
                missing = [
                    nozzle["code"]
                    for nozzle in self._nozzles.list_nozzles(
                        conn, active_only=True
                    )
                    if int(nozzle["fuel_product_id"]) == product_id
                    and not self._nozzles.has_segment_before(
                        conn, int(nozzle["id"]), business_date, stamp
                    )
                ]
                if missing:
                    raise errors.ValidationError(
                        "Record a meter snapshot reading (segment) on "
                        + ", ".join(missing)
                        + " before the price change takes effect.",
                        key="err.price_needs_snapshot",
                        params={"nozzles": ", ".join(missing)},
                    )
            price_id = self._prices.insert(
                conn, product_id, stamp, price_10k, business_date, who
            )
        return price_id

    # ------------------------------------------------------------------
    # connection-taking helpers (for other services' transactions)
    # ------------------------------------------------------------------
    def _get_effective_conn(
        self, conn, fuel_product_id: int, at_utc: str
    ) -> PriceInfo | None:
        """Price effective at *at_utc* on a caller-owned connection."""
        row = self._prices.get_effective(conn, fuel_product_id, at_utc)
        return _to_price_info(row) if row else None

    def _get_effective_strict_conn(
        self, conn, fuel_product_id: int, at_utc: str, business_date: str
    ) -> PriceInfo | None:
        """Price for a sale interval on a caller-owned connection.

        The latest row with ``effective_from_utc`` strictly before the
        interval's end *and* not after the business day's end: a price that
        takes effect after the business day ended cannot price that day's
        sales (readings are sometimes recorded late, so wall-clock alone is
        not enough). The interval ending at a price change's effective
        moment still belongs to the old price (§11.2 — never retrospective).
        """
        day_end = timecalc.karachi_end_of_day_utc(business_date)
        bound = min(at_utc, day_end)  # canonical form: lexicographic = chronological
        row = self._prices.get_effective_strict(conn, fuel_product_id, bound)
        return _to_price_info(row) if row else None

    def _list_products_conn(self, conn, *, active_only: bool = True):
        """Fuel-product rows on a caller-owned connection."""
        rows = self._prices.list_products(conn)
        if active_only:
            rows = [row for row in rows if row["is_active"]]
        return rows

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------
    @staticmethod
    def _require_timestamp(value: str) -> str:
        try:
            return timecalc.require_utc_iso(value)
        except ValueError as exc:
            raise errors.ValidationError(
                str(exc),
                key="err.invalid_utc_timestamp",
                params={"value": value},
            ) from exc
