#!/usr/bin/env python3
"""Node measured-flow core — register, count, spool, terminate.

This is the kernel-facing half of measured metering. The panel ledger lives
elsewhere. This file is stdlib-only so it can ship next to loadprobe.

What is counted
---------------
nft per-element counters on a **dedicated** table ``inet mediadeck_meter``.
The unit is **kernel outbound IP-packet bytes** (L3 length as nft sees it):
headers included, possible retransmits included. It is not HTTP body length,
not nginx ``$bytes_sent``, and not a NIC frame capture. Loopback experiments
that showed ~0.3% header overhead are **not** a general error bound.

Register-then-allow
-------------------
nginx ``mirror`` is asynchronous and cannot promise the set element exists
before the first payload. ``register()`` is synchronous: it adds the 4-tuple
and only then returns ``allow=True``. The next round can hang this off
``auth_request``. Importing this module never talks to nft.

Identity
--------
A 4-tuple is bound to one ``utag`` for its lifetime. A second tag on the
same live tuple is refused (HTTP/2 / keepalive mix). Tuple reuse after
close mints a new ``conn_id``.

Persistence
-----------
Counters have **no short timeout**. Closed connections stay in the set and
in the local spool until the panel acknowledges settlement. A process restart mints a new ``boot_id`` (**report epoch** only) and
resends unacked envelopes. Persistent stream identity is ``conn_id`` +
``generation``, which survive in sqlite and in the nft element. The panel
watermarks ``(node, conn_id, generation)`` — never ``boot_id``, or a
live connection would be billed from zero after every agent restart.

A counter that goes backwards under the same ``conn_id`` bumps
``generation`` (table/element flush). A smaller number in a late report
is **not** a new generation by itself.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from typing import Any

TABLE = "mediadeck_meter"
SET_V4 = "media_v4"
SET_V6 = "media_v6"
FAMILY_V4 = "inet"
FAMILY_V6 = "inet6"


def _run(cmd: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 99, "", f"{type(exc).__name__}: {exc}"


def normalize_ip(value: str) -> str:
    return str(ipaddress.ip_address(value.strip()))


def classify_family(ip: str) -> str:
    parsed = ipaddress.ip_address(ip)
    return FAMILY_V6 if parsed.version == 6 else FAMILY_V4


def format_tuple(local_ip: str, local_port: int, remote_ip: str, remote_port: int) -> str:
    return f"{local_ip} . {int(local_port)} . {remote_ip} . {int(remote_port)}"


def parse_concat_counters(text: str, family: str) -> dict[str, dict[str, int]]:
    """Map ``'lip . lport . rip . rport'`` -> {packets, bytes}."""
    if family == FAMILY_V6:
        item = (
            r"([0-9a-fA-F:]+) \. (\d+) \. ([0-9a-fA-F:]+) \. (\d+) "
            r"counter packets (\d+) bytes (\d+)"
        )
    else:
        item = (
            r"(\d+\.\d+\.\d+\.\d+) \. (\d+) \. (\d+\.\d+\.\d+\.\d+) \. (\d+) "
            r"counter packets (\d+) bytes (\d+)"
        )
    out: dict[str, dict[str, int]] = {}
    for lip, lp, rip, rp, pkts, nbytes in re.findall(item, text):
        key = format_tuple(normalize_ip(lip), int(lp), normalize_ip(rip), int(rp))
        out[key] = {"packets": int(pkts), "bytes": int(nbytes)}
    return out


class NftError(RuntimeError):
    pass


class FlowMeter:
    """Local connection registry + nft counters + durable report spool.

    ``enabled`` is off until ``enable()``. Construction, import and unit-test
    helpers never create tables or change default nft policy.
    """

    def __init__(self, persist_path: str, node: str = "local",
                 enabled: bool = False, deny_map_path: str | None = None) -> None:
        self.node = node
        self._deny_map_path = deny_map_path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(persist_path)) or ".", exist_ok=True)
        self._db = sqlite3.connect(persist_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._init_db()
        # Report epoch: identifies this process's envelopes. Distinct from
        # conn_id, which is the durable stream identity stored in ``conns``.
        self._boot_id = uuid.uuid4().hex
        self._seq = 0
        self._enabled = False
        with self._lock:
            self._db.execute(
                "INSERT INTO meta(key,value) VALUES('report_epoch',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self._boot_id,),
            )
            self._db.commit()
        if enabled:
            self.enable()

    # -- lifecycle -----------------------------------------------------------
    def enable(self) -> None:
        """Create the dedicated table and restore unsettled elements.

        Safe to call more than once. Does not flush or modify any other table.
        """
        with self._lock:
            self._ensure_table()
            self._enabled = True
            self._db.execute(
                "INSERT INTO meta(key,value) VALUES('enabled','1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'")
            self._db.commit()
            for row in self._db.execute(
                "SELECT * FROM conns WHERE settled=0"
            ).fetchall():
                # Re-bind the existing element. Do NOT delete+add: that
                # would zero a live kernel counter and look like a new
                # generation after a process restart.
                self._add_element(dict(row))

    def disable(self, *, delete_table: bool = False) -> None:
        with self._lock:
            self._enabled = False
            if delete_table:
                _run(["nft", "delete", "table", "inet", TABLE])
            self._db.execute(
                "INSERT INTO meta(key,value) VALUES('enabled','0') "
                "ON CONFLICT(key) DO UPDATE SET value='0'")
            self._db.commit()

    @property
    def boot_id(self) -> str:
        return self._boot_id

    @property
    def enabled(self) -> bool:
        return self._enabled

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- register / allow ----------------------------------------------------
    def register(self, local_ip: str, local_port: int, remote_ip: str,
                 remote_port: int, utag: str,
                 nginx_cid: str | None = None) -> dict[str, Any]:
        """Synchronously bind a 4-tuple to ``utag`` and install the counter.

        Returns ``allow=True`` only after the nft element exists (or nft is
        disabled in a unit test). Mixed identity on a live tuple is refused.
        A closed kernel socket on the same 4-tuple is reuse, not mix.
        """
        utag = (utag or "").strip()
        if not utag or utag == "-":
            return {"ok": False, "allow": False, "reason": "missing_utag",
                    "conn_id": None}
        if self.is_blocked(utag):
            return {"ok": False, "allow": False, "reason": "blocked",
                    "conn_id": None, "utag": utag}
        try:
            local_ip = normalize_ip(local_ip)
            remote_ip = normalize_ip(remote_ip)
            local_port = int(local_port)
            remote_port = int(remote_port)
        except (ValueError, ipaddress.AddressValueError) as exc:
            return {"ok": False, "allow": False, "reason": f"bad_tuple:{exc}",
                    "conn_id": None}
        if classify_family(local_ip) != classify_family(remote_ip):
            return {"ok": False, "allow": False, "reason": "family_mismatch",
                    "conn_id": None}
        if not (1 <= local_port <= 65535 and 1 <= remote_port <= 65535):
            return {"ok": False, "allow": False, "reason": "bad_port",
                    "conn_id": None}

        family = classify_family(local_ip)
        now = time.time()
        with self._lock:
            live = self._db.execute(
                "SELECT * FROM conns WHERE local_ip=? AND local_port=? "
                "AND remote_ip=? AND remote_port=? AND closed_at IS NULL",
                (local_ip, local_port, remote_ip, remote_port),
            ).fetchone()
            if live:
                still = self._tuple_still_live(
                    local_ip, local_port, remote_ip, remote_port,
                    nginx_cid=nginx_cid, stored=dict(live))
                if live["utag"] != utag:
                    if still:
                        return {
                            "ok": False, "allow": False,
                            "reason": "mixed_identity",
                            "conn_id": live["conn_id"],
                            "existing_utag": live["utag"],
                        }
                    self._db.execute(
                        "UPDATE conns SET closed_at=? WHERE conn_id=? AND closed_at IS NULL",
                        (now, live["conn_id"]))
                    self._db.commit()
                elif still:
                    return {
                        "ok": True, "allow": bool(self._enabled),
                        "reason": "already_registered" if self._enabled else "not_enabled",
                        "conn_id": live["conn_id"], "generation": live["generation"],
                        "utag": utag, "family": family,
                    }
                else:
                    self._db.execute(
                        "UPDATE conns SET closed_at=? WHERE conn_id=? AND closed_at IS NULL",
                        (now, live["conn_id"]))
                    self._db.commit()

            conn_id = uuid.uuid4().hex
            row = {
                "conn_id": conn_id,
                "generation": 1,
                "utag": utag,
                "family": family,
                "local_ip": local_ip,
                "local_port": local_port,
                "remote_ip": remote_ip,
                "remote_port": remote_port,
                "created_at": now,
                "closed_at": None,
                "last_counter": 0,
                "last_packets": 0,
                "settled": 0,
            }
            if self._enabled:
                try:
                    # Replacing the element resets the kernel counter. A new
                    # conn_id on a reused 4-tuple must not inherit the previous
                    # absolute reading (the panel would treat it as a fresh
                    # watermark starting at 0 and credit the leftover).
                    self._delete_element(row)
                    self._add_element(row)
                except NftError as exc:
                    return {"ok": False, "allow": False,
                            "reason": f"nft:{exc}", "conn_id": None}
            self._db.execute(
                "INSERT INTO conns(conn_id,generation,utag,family,local_ip,"
                "local_port,remote_ip,remote_port,created_at,closed_at,"
                "last_counter,last_packets,settled,nginx_cid) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
                (conn_id, 1, utag, family, local_ip, local_port, remote_ip,
                 remote_port, now, None, 0, 0, (nginx_cid or "")),
            )
            self._db.commit()
            return {
                "ok": True, "allow": bool(self._enabled),
                "reason": "registered" if self._enabled else "not_enabled",
                "conn_id": conn_id, "generation": 1, "utag": utag,
                "family": family,
            }

    def mark_closed(self, conn_id: str, at: float | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE conns SET closed_at=? WHERE conn_id=? AND closed_at IS NULL",
                (at if at is not None else time.time(), conn_id))
            self._db.commit()

    # -- observe / spool -----------------------------------------------------
    def collect(self) -> dict[str, Any]:
        """Read nft, bump generation on explicit reset, queue one envelope.

        Returns the envelope that was persisted (also sitting in the spool
        until ``ack``). Missing nft reads do not invent zeros.
        """
        with self._lock:
            observed_at = time.time()
            readings: dict[str, dict[str, int]] | None
            if not self._enabled:
                readings = None
                nft_ok = False
                nft_error = "not_enabled"
            else:
                try:
                    readings = self._read_all()
                    nft_ok = True
                    nft_error = ""
                except NftError as exc:
                    readings = None
                    nft_ok = False
                    nft_error = str(exc)

            samples: list[dict[str, Any]] = []
            live_keys = self._live_tuples() if self._enabled else None
            conns = [dict(r) for r in self._db.execute("SELECT * FROM conns WHERE settled=0")]
            for conn in conns:
                key = format_tuple(conn["local_ip"], conn["local_port"],
                                   conn["remote_ip"], conn["remote_port"])
                if readings is None:
                    continue
                snap = readings.get(key)
                generation = int(conn["generation"])
                if snap is None:
                    # Element gone while unsettled: keep last known, mark closed.
                    if conn["closed_at"] is None:
                        self._db.execute(
                            "UPDATE conns SET closed_at=? WHERE conn_id=?",
                            (observed_at, conn["conn_id"]))
                    samples.append(self._sample_from(conn, conn["last_counter"],
                                                     conn["last_packets"],
                                                     observed_at, closed=True,
                                                     coverage="retained"))
                    continue
                counter = int(snap["bytes"])
                packets = int(snap["packets"])
                last = int(conn["last_counter"])
                if counter < last:
                    # Same conn_id, counter went backwards: table/element reset.
                    generation += 1
                    self._db.execute(
                        "UPDATE conns SET generation=?, last_counter=?, last_packets=? "
                        "WHERE conn_id=?",
                        (generation, counter, packets, conn["conn_id"]))
                    conn["generation"] = generation
                else:
                    self._db.execute(
                        "UPDATE conns SET last_counter=?, last_packets=? WHERE conn_id=?",
                        (counter, packets, conn["conn_id"]))
                closed = conn["closed_at"] is not None
                if (not closed and live_keys is not None
                        and key not in live_keys):
                    self._db.execute(
                        "UPDATE conns SET closed_at=? WHERE conn_id=? AND closed_at IS NULL",
                        (observed_at, conn["conn_id"]))
                    closed = True
                samples.append(self._sample_from(conn, counter, packets,
                                                 observed_at, closed=closed,
                                                 coverage="observed"))
            self._seq += 1
            envelope = {
                "node": self.node,
                "boot_id": self._boot_id,
                "seq": self._seq,
                "observed_at": observed_at,
                "nft_ok": nft_ok,
                "nft_error": nft_error or None,
                "unit": "kernel_outbound_ip_bytes",
                "samples": samples,
            }
            self._db.execute(
                "INSERT INTO pending(seq,boot_id,payload,acked,created_at) "
                "VALUES(?,?,?,0,?)",
                (self._seq, self._boot_id, json.dumps(envelope, sort_keys=True),
                 observed_at))
            self._db.execute(
                "INSERT INTO meta(key,value) VALUES('seq',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(self._seq),))
            self._db.commit()
            return envelope

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM pending WHERE acked=0 ORDER BY seq"
            ).fetchall()
            return [json.loads(r["payload"]) for r in rows]

    def ack(self, boot_id: str, seq: int) -> int:
        """Panel confirmed this envelope. Recycle closed+acked conns."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE pending SET acked=1 WHERE boot_id=? AND seq=? AND acked=0",
                (boot_id, int(seq)))
            n = cur.rowcount or 0
            if n:
                self._db.execute(
                    "DELETE FROM pending WHERE boot_id=? AND seq=? AND acked=1",
                    (boot_id, int(seq)))
                self._recycle_settled()
            self._db.commit()
            return n

    def connections(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM conns")]

    # -- terminate -----------------------------------------------------------
    def terminate(self, local_ip: str, local_port: int, remote_ip: str,
                  remote_port: int) -> dict[str, Any]:
        """Reset the exact 4-tuple. Does not touch any other socket."""
        local_ip = normalize_ip(local_ip)
        remote_ip = normalize_ip(remote_ip)
        src = self._ss_endpoint(local_ip, int(local_port))
        dst = self._ss_endpoint(remote_ip, int(remote_port))
        code, out, err = _run(["ss", "-K", "src", src, "dst", dst])
        with self._lock:
            self._db.execute(
                "UPDATE conns SET closed_at=? WHERE local_ip=? AND local_port=? "
                "AND remote_ip=? AND remote_port=? AND closed_at IS NULL",
                (time.time(), local_ip, int(local_port), remote_ip, int(remote_port)))
            self._db.commit()
        return {"ok": code == 0, "code": code, "out": out, "err": err,
                "src": src, "dst": dst}

    def terminate_utag(self, utag: str) -> list[dict[str, Any]]:
        """Reset every live registered 4-tuple for this tag."""
        utag = (utag or "").strip()
        results = []
        with self._lock:
            rows = [dict(r) for r in self._db.execute(
                "SELECT * FROM conns WHERE utag=? AND closed_at IS NULL AND settled=0",
                (utag,))]
        for row in rows:
            results.append(self.terminate(
                row["local_ip"], row["local_port"],
                row["remote_ip"], row["remote_port"]))
        return results

    def is_blocked(self, utag: str) -> bool:
        utag = (utag or "").strip()
        if not utag:
            return False
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM denied WHERE utag=?", (utag,)).fetchone()
            return row is not None

    def blocked_tags(self) -> list[str]:
        with self._lock:
            return [r["utag"] for r in self._db.execute(
                "SELECT utag FROM denied ORDER BY utag")]

    def apply_policy(self, blocked_tags: list[str] | None = None,
                     *, unblock_tags: list[str] | None = None,
                     terminate: bool = True,
                     snapshot: bool = False) -> dict[str, Any]:
        """Apply deny tags.

        Merge mode (default): empty blocked_tags never clears known blocks.
        Snapshot mode: ``blocked_tags`` is the full authoritative set and
        may be empty. Only a successful panel sync uses snapshot=True.
        """
        want = {str(t).strip() for t in (blocked_tags or []) if str(t).strip()}
        drop = {str(t).strip() for t in (unblock_tags or []) if str(t).strip()}
        killed = 0
        with self._lock:
            now = time.time()
            have = {r["utag"] for r in self._db.execute("SELECT utag FROM denied")}
            if snapshot:
                add = want - have
                drop = have - want
            else:
                add = want - have
                drop = drop & have
            for tag in add:
                self._db.execute(
                    "INSERT INTO denied(utag, blocked_at) VALUES(?,?) "
                    "ON CONFLICT(utag) DO NOTHING", (tag, now))
            for tag in drop:
                self._db.execute("DELETE FROM denied WHERE utag=?", (tag,))
            self._db.commit()
            have_list = [r["utag"] for r in self._db.execute(
                "SELECT utag FROM denied ORDER BY utag")]
        if terminate:
            for tag in add:
                killed += len(self.terminate_utag(tag))
        self.write_deny_map()
        return {"blocked": have_list, "killed": killed, "added": sorted(add),
                "unblocked": sorted(drop), "snapshot": snapshot}

    def write_deny_map(self, path: str | None = None) -> str | None:
        """Atomic nginx map include. Survives a dead meterd HTTP process."""
        target = path or self._deny_map_path
        if not target:
            return None
        tags = self.blocked_tags()
        body = "".join(f'"{tag}" 1;\n' for tag in tags) or "# none\n"
        directory = os.path.dirname(os.path.abspath(target)) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = f"{target}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, target)
        return target

    # -- nft -----------------------------------------------------------------
    def _ensure_table(self) -> None:
        steps = [
            ["nft", "add", "table", "inet", TABLE],
            ["nft", "add", "set", "inet", TABLE, SET_V4,
             "{", "type", "ipv4_addr", ".", "inet_service", ".",
             "ipv4_addr", ".", "inet_service", ";", "counter", ";", "}"],
            ["nft", "add", "set", "inet", TABLE, SET_V6,
             "{", "type", "ipv6_addr", ".", "inet_service", ".",
             "ipv6_addr", ".", "inet_service", ";", "counter", ";", "}"],
            ["nft", "add", "chain", "inet", TABLE, "egress",
             "{", "type", "filter", "hook", "output", "priority", "-10", ";", "}"],
            ["nft", "add", "rule", "inet", TABLE, "egress",
             "ip", "saddr", ".", "tcp", "sport", ".",
             "ip", "daddr", ".", "tcp", "dport", f"@{SET_V4}"],
            ["nft", "add", "rule", "inet", TABLE, "egress",
             "ip6", "saddr", ".", "tcp", "sport", ".",
             "ip6", "daddr", ".", "tcp", "dport", f"@{SET_V6}"],
        ]
        for cmd in steps:
            code, _out, err = _run(cmd)
            if code != 0 and "File exists" not in err and "exists" not in err.lower():
                raise NftError(err.strip() or "nft failed: " + " ".join(cmd))

    def _set_name(self, family: str) -> str:
        return SET_V6 if family == FAMILY_V6 else SET_V4

    def _add_element(self, conn: dict[str, Any]) -> None:
        elem = format_tuple(conn["local_ip"], conn["local_port"],
                            conn["remote_ip"], conn["remote_port"])
        code, _out, err = _run([
            "nft", "add", "element", "inet", TABLE, self._set_name(conn["family"]),
            "{", elem, "}",
        ])
        if code != 0 and "File exists" not in err and "exists" not in err.lower():
            raise NftError(err.strip() or "add element failed")

    def _delete_element(self, conn: dict[str, Any]) -> None:
        elem = format_tuple(conn["local_ip"], conn["local_port"],
                            conn["remote_ip"], conn["remote_port"])
        _run([
            "nft", "delete", "element", "inet", TABLE, self._set_name(conn["family"]),
            "{", elem, "}",
        ])

    def _read_all(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for family, name in ((FAMILY_V4, SET_V4), (FAMILY_V6, SET_V6)):
            code, text, err = _run(["nft", "list", "set", "inet", TABLE, name])
            if code != 0:
                raise NftError(err.strip() or f"list {name} failed")
            out.update(parse_concat_counters(text, family))
        return out

    def _recycle_settled(self) -> None:
        # A conn may be recycled only when closed AND every pending sample
        # that mentions it has been acked. We approximate: closed and no
        # unacked envelopes remain for this boot (panel acks in order).
        unacked = self._db.execute(
            "SELECT COUNT(*) AS n FROM pending WHERE acked=0"
        ).fetchone()["n"]
        if unacked:
            return
        rows = self._db.execute(
            "SELECT * FROM conns WHERE closed_at IS NOT NULL AND settled=0"
        ).fetchall()
        for row in rows:
            if self._enabled:
                self._delete_element(dict(row))
            self._db.execute(
                "UPDATE conns SET settled=1 WHERE conn_id=?", (row["conn_id"],))

    def _sample_from(self, conn: dict[str, Any], counter: int, packets: int,
                     observed_at: float, *, closed: bool,
                     coverage: str) -> dict[str, Any]:
        return {
            "conn_id": conn["conn_id"],
            "generation": int(conn["generation"]),
            "utag": conn["utag"],
            "family": conn["family"],
            "local_ip": conn["local_ip"],
            "local_port": int(conn["local_port"]),
            "remote_ip": conn["remote_ip"],
            "remote_port": int(conn["remote_port"]),
            "counter_bytes": int(counter),
            "counter_packets": int(packets),
            "observed_at": observed_at,
            "closed": bool(closed),
            "coverage": coverage,
        }

    @staticmethod
    def _ss_endpoint(ip: str, port: int) -> str:
        parsed = ipaddress.ip_address(ip)
        if parsed.version == 6:
            return f"[{ip}]:{port}"
        return f"{ip}:{port}"

    def _init_db(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conns (
                conn_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                utag TEXT NOT NULL,
                family TEXT NOT NULL,
                local_ip TEXT NOT NULL,
                local_port INTEGER NOT NULL,
                remote_ip TEXT NOT NULL,
                remote_port INTEGER NOT NULL,
                created_at REAL NOT NULL,
                closed_at REAL,
                last_counter INTEGER NOT NULL DEFAULT 0,
                last_packets INTEGER NOT NULL DEFAULT 0,
                settled INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_conns_tuple
                ON conns(local_ip, local_port, remote_ip, remote_port);
            CREATE TABLE IF NOT EXISTS pending (
                seq INTEGER NOT NULL,
                boot_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                acked INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                PRIMARY KEY (boot_id, seq)
            );
            CREATE TABLE IF NOT EXISTS denied (
                utag TEXT PRIMARY KEY,
                blocked_at REAL NOT NULL
            );
            """
        )
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(conns)")}
        if "nginx_cid" not in cols:
            self._db.execute(
                "ALTER TABLE conns ADD COLUMN nginx_cid TEXT NOT NULL DEFAULT ''")
        self._db.commit()

    def _tuple_still_live(self, local_ip: str, local_port: int,
                          remote_ip: str, remote_port: int, *,
                          nginx_cid: str | None, stored: dict[str, Any]) -> bool:
        """True if this sqlite row is still the same kernel/nginx connection.

        ss/nft sampling failure does not invent a close. Unit tests with
        nft disabled keep the sqlite live-row meaning.
        """
        if nginx_cid and stored.get("nginx_cid") and nginx_cid != stored.get("nginx_cid"):
            return False
        if not self._enabled:
            return True
        live = self._live_tuples()
        if live is None:
            return True
        key = format_tuple(local_ip, local_port, remote_ip, remote_port)
        return key in live

    def _live_tuples(self) -> set[str] | None:
        """Established 4-tuples from ss. None means the sample failed."""
        code, out, _err = _run(["ss", "-Htn", "state", "established"])
        if code != 0:
            return None
        keys: set[str] = set()
        for line in out.splitlines():
            parts = line.split()
            endpoints = [p for p in parts if ":" in p and p not in {"timer:", "users:("}]
            if len(endpoints) < 2:
                continue
            parsed_l = self._split_endpoint(endpoints[-2])
            parsed_r = self._split_endpoint(endpoints[-1])
            if parsed_l is None or parsed_r is None:
                continue
            try:
                keys.add(format_tuple(parsed_l[0], parsed_l[1], parsed_r[0], parsed_r[1]))
            except (ValueError, ipaddress.AddressValueError):
                continue
        return keys

    @staticmethod
    def _split_endpoint(token: str) -> tuple[str, int] | None:
        token = token.strip()
        if not token or token == "*":
            return None
        if token.startswith("["):
            end = token.find("]")
            if end < 0 or end + 2 > len(token) or token[end + 1] != ":":
                return None
            try:
                return normalize_ip(token[1:end]), int(token[end + 2:])
            except (ValueError, ipaddress.AddressValueError):
                return None
        if ":" not in token:
            return None
        host, port = token.rsplit(":", 1)
        try:
            return normalize_ip(host), int(port)
        except (ValueError, ipaddress.AddressValueError):
            return None
