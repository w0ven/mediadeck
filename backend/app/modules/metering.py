"""Retired traffic ledger compatibility. Historical DB tables are preserved."""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

UNIT = "kernel_outbound_ip_bytes"
SOURCE = "measured"
UNKNOWN_USER = ""
BILLABLE_COVERAGE = frozenset({"observed", "retained"})
# First observation after a month-crossing gap longer than this is not
# attributed to the new month (and not invented for the old one).
MONTH_GAP_SECONDS = 3 * 3600


def month_key(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), UTC)
    return dt.strftime("%Y-%m")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        number = int(value)
        return number if -(2 ** 63) <= number < 2 ** 63 else default
    except (TypeError, ValueError, OverflowError):
        return default


def _billable(sample: dict[str, Any], envelope: dict[str, Any]) -> str | None:
    """Return a reject reason, or None if the sample may move the watermark."""
    if envelope.get("unit") not in (None, "", UNIT) and str(envelope.get("unit")) != UNIT:
        return "invalid_unit"
    if envelope.get("nft_ok") is False:
        return "nft_down"
    coverage = str(sample.get("coverage") or "")
    if coverage not in BILLABLE_COVERAGE:
        return coverage or "unmeasured"
    if _as_int(sample.get("counter_bytes"), -1) < 0:
        return "unmeasured"
    return None


class MeasuredMeteringService:
    """Legacy import compatibility. No ledger reads/writes or quota policy."""
    def __init__(self, db, tag_to_user=None, expected_nodes=None):
        pass

    def ingest(self, envelope):
        return {'ok': True, 'retired': True, 'accepted': False,
                'ack': {'boot_id': envelope.get('boot_id'), 'seq': envelope.get('seq')}}

    def snapshot(self, user_id, **kwargs):
        return {'retired': True, 'measured_used_bytes': None}

    def totals(self, **kwargs):
        return {'retired': True, 'by_user': {}, 'by_node': []}

    def reset_credit(self, *args, **kwargs):
        raise ValueError('Traffic accounting retired; historical credit is immutable')

    def used_bytes(self, *args, **kwargs):
        return None
