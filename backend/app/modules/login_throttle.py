"""Short, self-expiring throttle for FAILED Emby password logins.

Only an upstream credential rejection counts. A successful login is never
delayed or refused unless its own (address, account) pair or its address is
inside a short block caused by repeated failures, and every block expires on
its own. There is deliberately no account-wide lock: anyone could otherwise
lock a member out by typing wrong passwords for that name from elsewhere.

State is in memory and bounded; a restart clears it. Nothing is persisted, so
an attack cannot grow the database the way per-attempt audit rows would.
"""
from __future__ import annotations

import ipaddress
import logging
import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Upstream statuses that mean "these credentials were rejected".
FAILURE_STATUSES = frozenset({401, 403})


@dataclass(frozen=True)
class Policy:
    window: float  # seconds over which failures are counted
    limit: int     # failures within the window that trigger a block
    block: float   # seconds the key stays blocked


# One address guessing one account: a member mistyping a few times stays far
# below this; a password-guessing loop does not.
PAIR = Policy(window=600, limit=6, block=600)
# One address guessing many accounts. Kept high because carrier NAT and VPN
# egress addresses are shared by many unrelated members.
ADDRESS = Policy(window=600, limit=30, block=900)
MAX_KEYS = 20000


def client_address(host: str | None) -> str:
    """Return a usable per-caller address, or '' when none is known.

    Loopback or unparsable values mean the proxy did not pass the caller's
    address; throttling on them would put every member into one bucket.
    """
    try:
        address = ipaddress.ip_address((host or "").strip())
    except ValueError:
        return ""
    if address.is_loopback or address.is_unspecified:
        return ""
    return str(address)


def account_key(username: str, user_id: str = "") -> str:
    value = (username or user_id or "").strip().casefold()
    return value[:128]


class _Bucket:
    __slots__ = ("blocked_until", "failures")

    def __init__(self) -> None:
        self.failures: deque[float] = deque()
        self.blocked_until = 0.0


class LoginThrottle:
    def __init__(self, pair: Policy = PAIR, address: Policy = ADDRESS,
                 clock: Callable[[], float] = time.monotonic, max_keys: int = MAX_KEYS) -> None:
        self._policies = {"pair": pair, "address": address}
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: OrderedDict[tuple[str, ...], _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def _keys(self, address: str, account: str) -> list[tuple[str, ...]]:
        if not address:
            return []
        keys = [("address", address)]
        if account:
            keys.insert(0, ("pair", address, account))
        return keys

    def retry_after(self, address: str, account: str) -> int:
        """Seconds until this caller may try again; 0 when not blocked."""
        now = self._clock()
        wait = 0.0
        with self._lock:
            for key in self._keys(address, account):
                bucket = self._buckets.get(key)
                if bucket is not None and bucket.blocked_until > now:
                    wait = max(wait, bucket.blocked_until - now)
        return math.ceil(wait) if wait > 0 else 0

    def failure(self, address: str, account: str) -> None:
        now = self._clock()
        with self._lock:
            for key in self._keys(address, account):
                policy = self._policies[key[0]]
                bucket = self._buckets.pop(key, None) or _Bucket()
                self._buckets[key] = bucket  # most recently used last
                while bucket.failures and bucket.failures[0] <= now - policy.window:
                    bucket.failures.popleft()
                bucket.failures.append(now)
                if len(bucket.failures) >= policy.limit and bucket.blocked_until <= now:
                    bucket.blocked_until = now + policy.block
                    bucket.failures.clear()
                    log.warning("login throttle: %s blocked for %ds after %d failed logins (%s)",
                                key[0], policy.block, policy.limit, address)
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)

    def success(self, address: str, account: str) -> None:
        """A correct password clears that pair's failure history."""
        if not address or not account:
            return
        with self._lock:
            self._buckets.pop(("pair", address, account), None)

    def __len__(self) -> int:
        return len(self._buckets)
