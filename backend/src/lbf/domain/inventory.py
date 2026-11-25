"""Inventory rules I-2 (snapshot staleness and unit conversion) and I-3 (no cross-backend sums).

Rule I-1 (HALDEN strict stock evidence) is applied unchanged by the v1 normalizers inside the
HALDEN adapter; this module holds the backend-neutral parts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lbf.domain.enums import AvailabilityStatus


@dataclass(frozen=True)
class Conversion:
    available_base: int | None
    issue: str | None


def convert_to_base(available_native: int, factor_num: int | None, factor_den: int | None) -> Conversion:
    """``available_base = available_native × factor_num / factor_den`` only when exactly divisible."""
    if factor_num is None or factor_den is None or factor_num <= 0 or factor_den <= 0:
        return Conversion(None, "MISSING_CONVERSION_FACTOR")
    numerator = available_native * factor_num
    if numerator % factor_den != 0:
        return Conversion(None, "NON_INTEGRAL_CONVERSION")
    return Conversion(numerator // factor_den, None)


def snapshot_age_seconds(snapshot_at: datetime, now: datetime) -> int:
    return max(0, int((now - snapshot_at).total_seconds()))


def is_stale(snapshot_at: datetime, now: datetime, max_age_seconds: int) -> bool:
    return snapshot_age_seconds(snapshot_at, now) > max_age_seconds


def status_for_quantity(available_base: int | None) -> AvailabilityStatus:
    if available_base is None:
        return AvailabilityStatus.UNDETERMINED
    return AvailabilityStatus.IN_STOCK if available_base > 0 else AvailabilityStatus.OUT_OF_STOCK


def quantity_to_native(quantity_base: int, factor_num: int, factor_den: int) -> int | None:
    """Base-unit quantity expressed in native units, or ``None`` when not an integral native quantity."""
    numerator = quantity_base * factor_den
    if numerator % factor_num != 0:
        return None
    return numerator // factor_num
