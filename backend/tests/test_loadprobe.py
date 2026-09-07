"""Node agent regression tests.

The agent ships as a standalone single file to the nodes, so it is loaded by
path here rather than imported as a package.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2] / "agent" / "loadprobe.py"


def _load():
    spec = importlib.util.spec_from_file_location("loadprobe", AGENT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_user_and_egress_windows_stay_aligned_and_long_enough() -> None:
    """A 3s window made a buffered viewer jump 0 <-> peak every poll.

    Players fill, go quiet, burst again. Averaging over a few seconds still
    tracks the wire, but is long enough that the dashboard does not read as
    a different movie every refresh. User rate and node egress must stay on
    the same span or attributed traffic disagrees with the node total.
    """
    module = _load()
    assert module.SpeedLog.RATE_WINDOW == module.Sampler.WINDOW
    assert module.Sampler.WINDOW >= 8.0


def test_parses_every_log_shape_nodes_actually_write() -> None:
    """Regression: live speed was permanently blank on every node.

    Nodes write ``<msec> u=<tag> r=<rate> <bytes> <secs>``, but the parser
    read fields positionally, so ``int("r=10000000")`` raised on *every*
    line and every line was discarded. user_speeds stayed empty forever, the
    panel silently fell back to its estimate, and the dashboard showed a
    speed belonging to nobody.

    The agent updates independently of each node's nginx template, so all
    three generations must parse. These are verbatim production lines.
    """
    parse = _load().SpeedLog.parse
    # current: address *and* port, so one socket maps to exactly one member
    assert parse("1788065790.925 a=1.2.3.4 p=51314 u=0d5cd0aa2c r=0 "
                 "114130944 5.676") == (
        1788065790.925, "0d5cd0aa2c", 114130944, 5.676, "1.2.3.4", "51314")
    # previous: address only
    assert parse("1788065790.925 a=1.2.3.4 u=0d5cd0aa2c r=0 114130944 5.676") == (
        1788065790.925, "0d5cd0aa2c", 114130944, 5.676, "1.2.3.4", "")
    # earlier: labelled but no address
    assert parse("1788035116.082 u=5e51160de9 r=10000000 246314907 5.383") == (
        1788035116.082, "5e51160de9", 246314907, 5.383, "", "")
    # original: purely positional
    assert parse("1788035116.082 5e51160de9 246314907 5.383") == (
        1788035116.082, "5e51160de9", 246314907, 5.383, "", "")
    # suffix s=/uri must not be read as bytes/seconds
    assert parse("1788616301.177 a=10.0.0.2 p=21956 u=68249a8004 r=15728625 "
                 "167100416 31.282 s=206 /s/pool/Some/Path/file.mkv") == (
        1788616301.177, "68249a8004", 167100416, 31.282, "10.0.0.2", "21956")


def test_unattributable_or_empty_requests_are_ignored() -> None:
    """Anonymous and zero-byte requests must not invent a user or a rate."""
    parse = _load().SpeedLog.parse
    assert parse("1788035116.082 u= r=0 12345 1.0") is None      # no user tag
    assert parse("1788035116.082 - 12345 1.0") is None           # nginx placeholder
    assert parse("1788035116.082 u=abc r=0 0 1.0") is None       # nothing sent
    assert parse("hello world") is None
    assert parse("") is None
    # "-" is nginx's empty-value placeholder, not a real address.
    assert parse("1788065790.925 a=- u=abc r=0 100 1.0")[4] == ""
    assert parse("1788065790.925 a=1.2.3.4 p=- u=abc r=0 100 1.0")[5] == ""


def test_in_flight_sockets_are_measured_not_just_finished_requests() -> None:
    """Regression: active viewers showed no measurement at all.

    nginx logs a request when it *ends*. In production the median request
    runs 27s and 46% run over a minute (observed max 3869s), so deriving
    live speed from completed lines alone left most active viewers with no
    data and the panel fell back to its estimate for nearly every session.

    Rate must therefore come from the kernel's bytes_acked on open sockets,
    with the log supplying only peer-address -> user attribution.
    """
    import time
    module = _load()
    log = module.SpeedLog("")  # no tailing thread; drive it directly
    now = time.time()

    # A request that started long ago and has NOT finished: no log line yet.
    log._ingest(f"{now - 600:.3f} a=9.9.9.9 p=44321 u=viewer r=0 1000 1.0")
    # Two socket samples one second apart: 20 MB moved while still open.
    log._conns["9.9.9.9:44321"] = [(now - 1.0, 0), (now, 20 * 1024 * 1024)]

    speeds = log.speeds()
    assert "viewer" in speeds
    assert speeds["viewer"] > 19 * 1024 * 1024


def test_shared_address_is_left_unattributed_rather_than_guessed() -> None:
    """Carrier NAT: one address, two members, no port -> credit neither."""
    import time
    module = _load()
    log = module.SpeedLog("")
    now = time.time()
    log._ingest(f"{now:.3f} a=5.5.5.5 u=alice r=0 1000 1.0")
    log._ingest(f"{now:.3f} a=5.5.5.5 u=bob r=0 1000 1.0")
    log._conns["5.5.5.5:1234"] = [(now - 1.0, 0), (now, 50 * 1024 * 1024)]
    # Neither member may be credited with the other's traffic.
    assert log._owner_of("5.5.5.5", now) is None
    # The 50 MB/s on that shared socket must reach neither member. Both still
    # show the tiny rate implied by their own completed 1000-byte request,
    # which is measured, not guessed.
    speeds = log.speeds()
    for tag in ("alice", "bob"):
        assert speeds.get(tag, 0) < 1024 * 1024


def test_shared_address_is_split_correctly_when_ports_are_known() -> None:
    """Regression: households and CGNAT were permanently unattributable.

    Falling back to address-only matching, one egress address seen under
    several members is ambiguous and gets dropped -- measured in production,
    a single address appeared under four different tags within hours, so
    every viewer behind it read as unmeasured forever.

    nginx logs the peer port, and the request and the streaming that follows
    share that socket, so each connection resolves to exactly one member.
    """
    import time
    module = _load()
    log = module.SpeedLog("")
    now = time.time()
    log._ingest(f"{now:.3f} a=5.5.5.5 p=1111 u=alice r=0 1000 1.0")
    log._ingest(f"{now:.3f} a=5.5.5.5 p=2222 u=bob r=0 1000 1.0")
    log._conns["5.5.5.5:1111"] = [(now - 1.0, 0), (now, 30 * 1024 * 1024)]
    log._conns["5.5.5.5:2222"] = [(now - 1.0, 0), (now, 10 * 1024 * 1024)]

    speeds = log.speeds()
    assert set(speeds) == {"alice", "bob"}
    # Each member gets their own socket's bytes, not a merged or dropped total.
    assert speeds["alice"] > 29 * 1024 * 1024
    assert speeds["bob"] < 11 * 1024 * 1024


def test_completed_requests_still_cover_clients_with_no_known_address() -> None:
    """A brand-new viewer must not be invisible before their first log line."""
    import time
    module = _load()
    log = module.SpeedLog("")
    now = time.time()
    log._ingest(f"{now:.3f} u=newcomer r=0 100000000 5.0")
    speeds = log.speeds()
    assert speeds.get("newcomer", 0) > 0
