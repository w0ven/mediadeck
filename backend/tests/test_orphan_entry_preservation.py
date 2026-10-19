"""A removed node must not block unrelated integration maintenance."""
from copy import deepcopy

import pytest
from test_entry_pinned import ADMIN, PINNED
from test_entry_pinned import client as client  # noqa: PLC0414 - fixture re-export

from app.core.errors import ConfigError
from app.main import app
from app.modules.entries import validate_entries

OLD = {**PINNED, "proxy_key": "synthetic-existing-entry-key"}


@pytest.mark.parametrize("restricted", [False, True])
def test_unchanged_orphan_entry_roundtrip_preserves_every_stored_field(restricted):
    old = {**OLD, **({"whitelist_only": True} if restricted else {})}
    public = {k: v for k, v in old.items() if k != "proxy_key"}
    public["proxy_key_set"] = True
    assert validate_entries([old], [old], node_names=set()) == [old]
    assert validate_entries([public], [old], node_names=set()) == [old]


@pytest.mark.parametrize("change", [
    {"id": "new-entry"},
    {"origin": "https://changed-origin.example.com"},
    {"stream_origin": "https://changed-stream.example.com"},
    {"node": "another-missing-node"},
    {"whitelist_only": True},
    {"rotate_proxy_key": True},
])
def test_orphan_binding_cannot_be_created_or_changed(change):
    with pytest.raises(ConfigError):
        validate_entries([{**OLD, **change}], [OLD], node_names=set())
    with pytest.raises(ConfigError):
        validate_entries([OLD], [], node_names=set())


def test_orphan_does_not_block_save_or_healthy_entry_rotation(client):
    url = "/api/settings/integration"
    before = client.get(url, auth=ADMIN).json()
    stored = deepcopy(app.state.settings_service.integration_config()["external_entries"])
    assert client.delete("/api/nodes/edge-b", auth=ADMIN).status_code == 200
    same = client.put(url, auth=ADMIN, json=before)
    assert same.status_code == 200
    assert same.json()["external_entries_revision"] == before["external_entries_revision"]
    assert client.put(url, auth=ADMIN, json={"tmdb_language": "en-US"}).status_code == 200
    assert app.state.settings_service.integration_config()["external_entries"] == stored

    request = client.get(url, auth=ADMIN).json()
    next(e for e in request["external_entries"] if e["id"] == "routed-one")["rotate_proxy_key"] = True
    rotated = client.put(url, auth=ADMIN, json=request)
    assert rotated.status_code == 200
    current = app.state.settings_service.integration_config()["external_entries"]
    assert current[0] == stored[0]
    assert current[1]["proxy_key"] != stored[1]["proxy_key"]
    assert client.put(url, auth=ADMIN, json=request).status_code == 409
