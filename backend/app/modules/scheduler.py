"""Load-aware streaming node scheduler (302 dispatch core).

Two selection policies, chosen by the operator at runtime:

``least-load``
    Pick the enabled+healthy node with the lowest utilisation
    (``active_streams / capacity``).  Simple, but with multiple nodes the same
    title gets served from different nodes on every request, so each node
    pulls and caches its own copy of the file from the origin.

``affinity`` (default)
    Hash the requested path onto the ring of healthy nodes so a given file is
    always served by the same node.  That node caches it once; repeat viewers
    hit a warm cache instead of re-pulling tens of GB from the origin.  If the
    preferred node is overloaded (utilisation above ``load_threshold``) or
    unhealthy, the request walks the ring to the next candidate, so affinity
    never wins over availability.

Popular titles distribute naturally across the ring, so load stays balanced
without giving up cache locality.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from app.core.config import StreamNode

POLICIES = ("affinity", "least-load")
PROBE_INTERVAL = 3.0
METRIC_STALE_AFTER = 15.0


def sample_time(value: Any, received: float) -> tuple[float, str]:
    if value is None:
        return received, 'probe_received'
    try:
        stamp = float(value)
        if math.isfinite(stamp) and 0 < stamp <= received + 5:
            return stamp, 'collector'
    except (ValueError, TypeError):
        pass
    return 0.0, 'invalid'


def nonnegative(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError):
        return None



@dataclass
class NodeState:
    node: StreamNode
    ok: bool = False
    active_streams: int = 0
    egress_mbps: float = 0.0
    last_probe_ts: float = 0.0
    consecutive_failures: int = 0
    manually_disabled: bool = False
    # Anonymised user tag -> measured bytes/second on this node's wire, as
    # reported by the node's speed collector. Display-only; never billing.
    user_speeds: dict[str, int] = None  # type: ignore[assignment]
    user_speeds_at: float = 0.0
    user_speeds_ok: bool = False
    probe_error: str | None = None
    last_success_ts: float = 0.0
    egress_ok: bool = False
    egress_at: float = 0.0
    egress_time_basis: str = 'probe_received'
    egress_window_seconds: float | None = None
    user_speeds_time_basis: str = 'probe_received'
    user_speeds_window_seconds: float | None = None
    user_speeds_source: str = 'legacy_socket'

    def __post_init__(self) -> None:
        if self.user_speeds is None:
            self.user_speeds = {}

    def utilisation(self) -> float:
        """Node load uses concurrent stream capacity, not retired byte-rate telemetry."""
        return self.active_streams / max(self.node.capacity, 1.0)

    def available(self) -> bool:
        return (
            self.node.enabled
            and not self.manually_disabled
            and self.ok
            and self.consecutive_failures < 3
        )


class Scheduler:
    HISTORY_MAX = 3600     # probe snapshots kept per node (~3h at 3s interval)
    DISPATCH_MAX = 1000    # recent dispatch decisions kept
    # How much busier the affinity-preferred node may be before a request goes
    # to the quietest peer instead. Affinity hashes a path to a fixed node, so
    # without this every viewer of a hot title lands on one machine while the
    # others idle. Small enough that popularity spreads promptly; large enough
    # that ordinary probe jitter does not bounce a cold title between nodes and
    # throw away its cache locality.
    SPREAD_MARGIN = 0.15

    def __init__(
        self,
        nodes: list[StreamNode],
        probe: Any,
        policy: str = "affinity",
        load_threshold: float = 0.8,
    ) -> None:
        # Set whenever the node list changes so the probe loop can wake up and
        # report a fresh node's health immediately instead of leaving it shown
        # as unavailable until the next 15s tick.
        self._wake = asyncio.Event()
        self._probe = probe
        self._policy = policy if policy in POLICIES else "affinity"
        self._load_threshold = load_threshold
        self._states: dict[str, NodeState] = {}
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._dispatch_log: deque[dict[str, Any]] = deque(maxlen=self.DISPATCH_MAX)
        self.reconfigure(nodes)

    # -- runtime reconfiguration --------------------------------------------
    def reconfigure(self, nodes: list[StreamNode]) -> None:
        """Apply a new node list without dropping live probe state.

        Called whenever the operator edits nodes in the UI, so changes take
        effect on the next dispatch instead of requiring a restart.
        """
        seen: set[str] = set()
        for node in nodes:
            seen.add(node.name)
            existing = self._states.get(node.name)
            if existing:
                existing.node = node        # keep probe history/health
            else:
                self._states[node.name] = NodeState(node=node)
                self._history[node.name] = deque(maxlen=self.HISTORY_MAX)
        for name in list(self._states):
            if name not in seen:
                self._states.pop(name, None)
                self._history.pop(name, None)
        self._wake.set()

    async def wait_for_change(self, timeout: float) -> None:
        """Sleep until the node list changes, or the timeout elapses."""
        with contextlib.suppress(TimeoutError):
            # wait_for can swallow outer cancellation when the wake completes
            # concurrently on Python 3.11.2, leaving shutdown stuck forever.
            async with asyncio.timeout(timeout):
                await self._wake.wait()
        self._wake.clear()

    def set_policy(self, policy: str, load_threshold: float | None = None) -> None:
        if policy in POLICIES:
            self._policy = policy
        if load_threshold is not None:
            self._load_threshold = max(0.0, float(load_threshold))

    @property
    def policy(self) -> str:
        return self._policy

    @property
    def load_threshold(self) -> float:
        return self._load_threshold

    # -- probing -------------------------------------------------------------
    async def refresh(self) -> None:
        # A slow/unreachable peer must not delay every other node's live data.
        await asyncio.gather(*(self._refresh_node(st) for st in list(self._states.values())))

    async def _refresh_node(self, st: NodeState) -> None:
        try:
            data = await self._probe.load(st.node.probe_url)
            if not isinstance(data, dict):
                raise TypeError("invalid probe payload")
            if data.get('ok'):
                active = nonnegative(data.get('active_streams', 0))
                if active is None:
                    raise ValueError("invalid stream count")
        except Exception:  # noqa: BLE001 - isolate failed probes, never leak URLs
            data = {'ok': False, 'error': 'probe_failed'}
        if self._states.get(st.node.name) is not st:
            return
        st.last_probe_ts = time.time()
        if data.get('ok'):
            st.ok = True
            st.last_success_ts = st.last_probe_ts
            st.consecutive_failures = 0
            st.active_streams = int(active)
            # Discard byte-rate fields even from agents not yet upgraded.
            st.egress_mbps, st.egress_ok = 0, False
            st.user_speeds, st.user_speeds_ok = {}, False
            st.probe_error = None
        else:
            st.ok = False
            st.consecutive_failures += 1
            st.user_speeds_ok = False
            st.egress_ok = False
            st.probe_error = str(data.get('error') or 'probe_failed')
        self._history[st.node.name].append({
            'ts': st.last_probe_ts, 'ok': st.ok,
            'active_streams': st.active_streams if st.ok else None,
        })

    @staticmethod
    def _egress_fresh(st: NodeState) -> bool:
        return st.ok and st.egress_ok and 0 <= time.time() - st.egress_at <= METRIC_STALE_AFTER

    # -- selection -----------------------------------------------------------
    @staticmethod
    def _ring_key(path: str, node_name: str) -> str:
        return hashlib.sha256(f"{path}\x00{node_name}".encode()).hexdigest()

    def _affinity_order(self, path: str, candidates: list[NodeState]) -> list[NodeState]:
        """Rendezvous (highest-random-weight) hashing.

        Unlike a modulo ring, adding or removing a node only remaps the files
        that belonged to it — everything else keeps its current node, so cache
        churn on topology changes stays proportional to the change.
        """
        return sorted(
            candidates,
            key=lambda s: self._ring_key(path, s.node.name),
            reverse=True,
        )

    def pick(self, record: bool = True, context: str = "",
             predicate: Any = None) -> NodeState | None:
        """Choose a node.

        ``predicate`` filters to nodes that can actually serve this request --
        a node only mirroring one media root must never be handed a file from
        another root, or the client gets a 404 from a "healthy" node.
        """
        candidates = [s for s in self._states.values() if s.available()]
        if predicate is not None:
            candidates = [s for s in candidates if predicate(s)]
        chosen: NodeState | None = None
        reason = "none-available"

        if candidates:
            if self._policy == "affinity" and context:
                ordered = self._affinity_order(context, candidates)
                lightest = min(candidates, key=lambda s: (s.utilisation(), s.egress_mbps))
                preferred = None
                for state in ordered:
                    if state.utilisation() <= self._load_threshold:
                        preferred = state
                        break

                if preferred is None:
                    # Every preferred node is over threshold: availability wins.
                    chosen, reason = lightest, "affinity-overflow"
                elif preferred.utilisation() - lightest.utilisation() > self.SPREAD_MARGIN:
                    # Affinity keeps a title on one node for cache locality,
                    # but a popular title would otherwise pin every viewer to
                    # that same node while peers sit idle. Step aside once the
                    # preferred node is measurably busier than the quietest.
                    chosen, reason = lightest, "affinity-spread"
                else:
                    chosen, reason = preferred, "affinity"
            else:
                chosen = min(candidates, key=lambda s: (s.utilisation(), s.egress_mbps))
                reason = "least-load"

        if record:
            self._dispatch_log.append({
                "ts": time.time(),
                "node": chosen.node.name if chosen else None,
                "utilisation": round(chosen.utilisation(), 3) if chosen else None,
                "candidates": len(candidates),
                "policy": self._policy,
                "reason": reason,
                "context": context[:200],
            })
        return chosen

    # -- introspection -------------------------------------------------------
    def user_speeds(self) -> dict[str, int]:
        return {}

    def user_speed_view(self, *, stale_after: float = METRIC_STALE_AFTER) -> dict[str, dict[str, Any]]:
        """No aggregate byte-rate telemetry is collected from nodes."""
        return {}

    def history(self, name: str, limit: int = 240) -> list[dict[str, Any]]:
        entries = self._history.get(name)
        if entries is None:
            raise KeyError(name)
        return list(entries)[-max(1, min(limit, self.HISTORY_MAX)):]

    def dispatch_log(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self._dispatch_log)[-max(1, min(limit, self.DISPATCH_MAX)):]

    def set_disabled(self, name: str, disabled: bool) -> bool:
        st = self._states.get(name)
        if not st:
            return False
        st.manually_disabled = disabled
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "name": s.node.name,
                "base_url": s.node.base_url,
                "probe_url": s.node.probe_url,
                "capacity": s.node.capacity,
                "enabled": s.node.enabled,
                "ok": s.ok,
                "available": s.available(),
                "manually_disabled": s.manually_disabled,
                "active_streams": s.active_streams,
                "last_success_ts": s.last_success_ts,
                "utilisation": round(s.utilisation(), 3),
                "last_probe_ts": s.last_probe_ts,
            }
            for s in self._states.values()
        ]
