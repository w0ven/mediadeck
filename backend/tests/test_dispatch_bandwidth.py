"""Dispatch must follow the wire, not the stream count.

Two failure modes these tests pin down:

- A node's stream count says "quiet" while its link is already saturated,
  so more playback keeps landing on a node that cannot carry it.
- Affinity hashes a title to a fixed node, so a popular title pins every
  viewer to one machine while its peers idle.
"""
from __future__ import annotations

from app.core.config import StreamNode
from app.modules.scheduler import NodeState, Scheduler


def _node(name: str, capacity: float = 48, bandwidth: float = 0) -> StreamNode:
    return StreamNode(
        name=name,
        base_url=f"https://{name}.example",
        probe_url=f"http://127.0.0.1:9800/load#{name}",
        capacity=capacity,
        bandwidth_mbps=bandwidth,
    )


def _state(name: str, *, streams: int = 0, egress: float = 0.0,
           capacity: float = 48, bandwidth: float = 0) -> NodeState:
    return NodeState(
        node=_node(name, capacity=capacity, bandwidth=bandwidth),
        ok=True,
        active_streams=streams,
        egress_mbps=egress,
    )


# -- utilisation ------------------------------------------------------------

def test_reported_egress_no_longer_overrides_stream_count() -> None:
    # Observed egress Mbps is retired as a dispatch signal
    # (docs/TRAFFIC_RETIREMENT.md): 10 of 48 streams stays 10/48 even when an
    # old agent still reports 760 of 800 Mbps.
    state = _state("ca1", streams=10, egress=760.0, capacity=48, bandwidth=800)
    assert state.utilisation() == 10 / 48


def test_many_light_streams_are_not_mistaken_for_a_full_pipe() -> None:
    # 40 of 48 streams is 83% by count and that is genuinely busy; the wire
    # being idle must not talk the node down into looking free.
    state = _state("ca1", streams=40, egress=50.0, capacity=48, bandwidth=800)
    assert state.utilisation() > 0.8


def test_node_without_declared_ceiling_keeps_stream_count_behaviour() -> None:
    state = _state("legacy", streams=24, egress=999.0, capacity=48, bandwidth=0)
    assert state.utilisation() == 0.5


# -- spreading --------------------------------------------------------------

def _scheduler(states: list[NodeState], threshold: float = 0.45) -> Scheduler:
    """Scheduler wired to fixed node states, with no probe loop running.

    The probe is only used by the background refresh task, which these tests
    never start; the states are injected directly so each case pins one exact
    load situation.
    """
    scheduler = Scheduler(
        nodes=[s.node for s in states],
        probe=None,
        policy="affinity",
        load_threshold=threshold,
    )
    scheduler._states = {s.node.name: s for s in states}
    return scheduler


def test_hot_title_moves_off_a_node_its_hash_prefers() -> None:
    """A saturated favourite must not keep collecting viewers."""
    busy = _state("busy", streams=6, egress=780.0, capacity=48, bandwidth=800)
    idle = _state("idle", streams=0, egress=0.0, capacity=48, bandwidth=800)
    scheduler = _scheduler([busy, idle])

    # Whichever node the hash prefers, a link at 97% must not win the request.
    chosen = scheduler.pick(record=False, context="/Media/TV/hot-show/s01e01.mkv")
    assert chosen is not None
    assert chosen.node.name == "idle"


def test_cold_title_stays_put_when_nodes_are_evenly_loaded() -> None:
    """Locality is the point of affinity; jitter must not shuffle nodes."""
    a = _state("a", streams=4, egress=100.0, capacity=48, bandwidth=800)
    b = _state("b", streams=5, egress=110.0, capacity=48, bandwidth=800)
    scheduler = _scheduler([a, b])

    path = "/Media/Movies/quiet-film.mkv"
    first = scheduler.pick(record=False, context=path)
    repeats = {scheduler.pick(record=False, context=path).node.name for _ in range(20)}

    assert first is not None
    assert repeats == {first.node.name}


def test_spread_decision_is_labelled_for_operators() -> None:
    busy = _state("busy", streams=2, egress=790.0, capacity=48, bandwidth=800)
    idle = _state("idle", streams=0, egress=0.0, capacity=48, bandwidth=800)
    scheduler = _scheduler([busy, idle])

    scheduler.pick(record=True, context="/Media/TV/hot/s01e02.mkv")
    reason = scheduler.dispatch_log()[-1]["reason"]
    assert reason in {"affinity-spread", "affinity", "affinity-overflow"}


def test_every_node_saturated_still_returns_the_least_bad_one() -> None:
    """Fail-open: a full fleet must still answer, never return nothing."""
    a = _state("a", streams=48, egress=800.0, capacity=48, bandwidth=800)
    b = _state("b", streams=40, egress=700.0, capacity=48, bandwidth=800)
    scheduler = _scheduler([a, b])

    chosen = scheduler.pick(record=True, context="/Media/TV/busy/s01e03.mkv")
    assert chosen is not None
    assert chosen.node.name == "b"
    assert scheduler.dispatch_log()[-1]["reason"] == "affinity-overflow"
