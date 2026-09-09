"""Blocking operator actions must not stall playback/event-loop work."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.mark.asyncio
@pytest.mark.parametrize("state_name,method,route", [
    ("updater", "check", main.update_check),
    ("updater", "version", main.update_version),
    ("pipeline", "snapshot", main.pipeline),
    ("mounts", "snapshot", main.mounts),
    ("tasks", "snapshot", main.tasks),
    ("storage", "list_remotes", main.storage_list_remotes),
])
async def test_slow_operations_do_not_block_other_coroutines(monkeypatch, state_name, method, route):
    order = []

    def blocking():
        time.sleep(0.08)
        order.append("operation")
        return {}

    monkeypatch.setattr(main.app.state, state_name, SimpleNamespace(**{method: blocking}), raising=False)

    async def heartbeat():
        await asyncio.sleep(0.005)
        order.append("heartbeat")

    await asyncio.gather(route(), heartbeat())
    assert order == ["heartbeat", "operation"]


@pytest.mark.asyncio
async def test_worker_storage_calls_keep_read_modify_write_serialized():
    state = {"value": 0}

    def read_modify_write():
        old = state["value"]
        time.sleep(0.02)
        state["value"] = old + 1

    await asyncio.gather(*(asyncio.to_thread(main._storage_call, read_modify_write)
                           for _ in range(4)))
    assert state["value"] == 4


def test_plugin_string_false_is_rejected_without_changing_switch():
    with TestClient(main.app) as client:
        auth = ("admin", "change-me")
        path = "/api/plugins/rankings_post"
        before = client.get(path, auth=auth).json()["enabled"]
        response = client.post(path, auth=auth, json={"enabled": "false"})
        assert response.status_code == 422
        assert client.get(path, auth=auth).json()["enabled"] is before
        assert client.post(path, auth=auth, json={"enabled": False}).status_code == 200
        assert client.get(path, auth=auth).json()["enabled"] is False
