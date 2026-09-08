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
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from app.core.config import StreamNode

POLICIES = ("affinity", "least-load")


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

    def __post_init__(self) -> None:
        if self.user_speeds is None:
            self.user_speeds = {}

    def utilisation(self) -> float:
        """Fraction of this node's capacity currently in use (0.0 - 1.0+).

        Takes the worse of two independent pressures:

        - stream count against the node's configured capacity
        - measured egress against the node's real link ceiling

        Stream count on its own misreads a busy node. A 1 Gbit box showing 10
        streams looks 20% loaded against a capacity of 48, while five
        direct-play 4K clients on that same box have already filled the pipe.
        Whichever limit sits closer to its ceiling is the one that decides,
        so a node is never handed work its wire cannot carry.

        Absolute, not relative: 0.8 means "80% full" on a small node and on a
        large one alike, so one threshold works for a mixed fleet. Nodes with
        no declared ceiling keep the stream-count-only behaviour.
        """
        capacity = max(self.node.capacity, 1.0)
        stream_load = self.active_streams / capacity

        ceiling = float(getattr(self.node, "bandwidth_mbps", 0) or 0)
        if ceiling <= 0:
            return stream_load
        return max(stream_load, self.egress_mbps / ceiling)

    def available(self) -> bool:
        return (
            self.node.enabled
            and not self.manually_disabled
            and self.ok
            and self.consecutive_failures < 3
        )


class Scheduler:
    HISTORY_MAX = 720      # probe snapshots kept per node (~3h at 15s interval)
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
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
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
        for st in self._states.values():
            data = await self._probe.load(st.node.probe_url)
            st.last_probe_ts = time.time()
            if data.get("ok"):
                st.ok = True
                st.consecutive_failures = 0
                st.active_streams = int(data.get("active_streams", 0))
                st.egress_mbps = float(data.get("egress_mbps", 0.0))
                speeds = data.get("user_speeds")
                st.user_speeds = ({str(k): int(v) for k, v in speeds.items()}
                                  if isinstance(speeds, dict) else {})
                st.user_speeds_at = st.last_probe_ts
                st.user_speeds_ok = True
                st.probe_error = None
            else:
                st.ok = False
                st.consecutive_failures += 1
                st.user_speeds_ok = False
                st.probe_error = str(data.get("error") or "probe_failed")
                # Keep last speeds with a stale timestamp; do not pretend 0.
            self._history[st.node.name].append({
                "ts": st.last_probe_ts,
                "ok": st.ok,
                "active_streams": st.active_streams if st.ok else None,
                "egress_mbps": st.egress_mbps if st.ok else None,
            })

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
        """Anonymised user tag -> bytes/second, summed across fresh node samples."""
        view = self.user_speed_view()
        return {tag: int(v["bps"]) for tag, v in view.items()
                if v.get("bps") is not None}

    def user_speed_view(self, *, stale_after: float = 45.0) -> dict[str, dict[str, Any]]:
        """Per-tag rate with collection time and coverage. Missing is not zero.

        NIC ``egress_mbps`` on the node snapshot is the whole interface and
        is a different series; it is not added into these user rates.
        """
        now = time.time()
        out: dict[str, dict[str, Any]] = {}
        for st in self._states.values():
            fresh = bool(st.user_speeds_ok) and (now - (st.user_speeds_at or 0)) <= stale_after
            collected = st.user_speeds_at or None
            if not fresh:
                continue
            for tag, bps in (st.user_speeds or {}).items():
                if not tag:
                    continue
                cur = out.get(tag)
                if cur is None:
                    out[tag] = {
                        "bps": int(bps),
                        "collected_at": collected,
                        "coverage": "direct",
                        "source": "node",
                        "nodes": [st.node.name],
                    }
                else:
                    cur["bps"] = int(cur["bps"]) + int(bps)
                    cur["nodes"].append(st.node.name)
                    if collected and (cur["collected_at"] or 0) < collected:
                        cur["collected_at"] = collected
        return out

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
                "egress_mbps": s.egress_mbps,
                "utilisation": round(s.utilisation(), 3),
                "last_probe_ts": s.last_probe_ts,
            }
            for s in self._states.values()
        ]
