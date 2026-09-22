"""Redeem cards through the API: minting, masking, revoking and export.

A card is a bearer credential -- whoever reads it can spend it -- so the two
things these tests hold onto are that the list view masks them and that the
audit trail never records a full one. The CSV export deliberately does *not*
mask: it is the operator's own download, and cards they cannot read are cards
they cannot hand out.
"""
from __future__ import annotations

import csv
import io
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app.main import app
from app.modules.registration import REDEEM_LENGTH, Admission

ADMIN = ("admin", "change-me")


def _group_id(client) -> str:
    return client.get("/api/groups", auth=ADMIN).json()[0]["id"]


def _mint(client, **kwargs) -> dict:
    payload = {"group_id": _group_id(client), "days": 30, "count": 1}
    payload.update(kwargs)
    r = client.post("/api/redeem/generate", auth=ADMIN, json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# -- generation --------------------------------------------------------------

def test_a_batch_is_minted_whole_and_returned_once() -> None:
    with TestClient(app) as client:
        made = _mint(client, count=10, batch="launch", days=90)
        assert made["count"] == 10
        values = [c["code"] for c in made["codes"]]
        assert len(set(values)) == 10
        assert all(len(v) == REDEEM_LENGTH for v in values)
        assert all(c["days"] == 90 for c in made["codes"])
        assert all(c["batch"] == "launch" for c in made["codes"])
        assert all('link' not in c for c in made['codes'])


def test_optional_card_links_and_csv_resolve_to_the_same_unused_cards(monkeypatch) -> None:
    async def local_only(*args, **kwargs):
        return None

    with TestClient(app) as client:
        monkeypatch.setattr(app.state.telegram, '_call', local_only)
        app.state.settings_service.save_telegram({'enabled': True, 'bot_token': '123:local-only'})
        app.state.telegram._check_bot_identity()
        app.state.telegram._bot_username = 'MediaDeckDemoBot'
        made = _mint(client, count=2, batch='with-links', include_links=True)
        for card in made['codes']:
            parsed = urlsplit(card['link'])
            assert parsed.scheme == 'https' and parsed.netloc == 't.me'
            assert parsed.path == '/MediaDeckDemoBot'
            admission = app.state.registration.resolve('765', parse_qs(parsed.query)['start'][0])
            assert admission.allowed and admission.via == 'redeem'
            assert admission.credential == card['code'] and card['status'] == 'unused'
        exported = client.get('/api/redeem/export.csv?batch=with-links&include_links=true', auth=ADMIN)
        assert exported.status_code == 200
        rows = list(csv.DictReader(io.StringIO(exported.text)))
        assert {row['registration_link'] for row in rows} == {c['link'] for c in made['codes']}
        audit = str(client.get('/api/audit?limit=20', auth=ADMIN).json())
        assert all(card['code'] not in audit and card['link'] not in audit for card in made['codes'])


def test_requesting_links_without_a_ready_bot_mints_nothing() -> None:
    with TestClient(app) as client:
        payload = {'group_id': _group_id(client), 'days': 30, 'count': 2, 'include_links': True}
        assert client.post('/api/redeem/generate', auth=ADMIN, json=payload).status_code == 409
        assert client.get('/api/redeem', auth=ADMIN).json()['stats']['total'] == 0
        payload['include_links'] = 'false'
        assert client.post('/api/redeem/generate', auth=ADMIN, json=payload).status_code == 400
        assert client.get('/api/redeem', auth=ADMIN).json()['stats']['total'] == 0


def test_generated_cards_start_unused_and_are_counted() -> None:
    with TestClient(app) as client:
        _mint(client, count=4, batch="counted")
        listing = client.get("/api/redeem", auth=ADMIN).json()
        assert listing["stats"]["unused"] == 4
        assert listing["stats"]["used"] == 0
        assert listing["stats"]["revoked"] == 0
        assert "counted" in listing["batches"]


def test_generation_rejects_bad_input() -> None:
    with TestClient(app) as client:
        gid = _group_id(client)
        for payload in ({"group_id": "", "days": 30, "count": 1},
                        {"group_id": "ghost-group", "days": 30, "count": 1},
                        {"group_id": gid, "days": 30, "count": 0},
                        {"group_id": gid, "days": 30, "count": 5000},
                        {"group_id": gid, "days": "abc", "count": 1}):
            r = client.post("/api/redeem/generate", auth=ADMIN, json=payload)
            assert r.status_code >= 400, payload


def test_an_unnamed_batch_still_gets_a_label() -> None:
    """Cards with no batch cannot be found again as a group."""
    with TestClient(app) as client:
        made = _mint(client, count=2, batch="")
        assert made["codes"][0]["batch"]
        assert made["codes"][0]["batch"] == made["codes"][1]["batch"]


# -- masking -----------------------------------------------------------------

def test_the_list_masks_cards_but_keeps_them_identifiable() -> None:
    with TestClient(app) as client:
        made = _mint(client, count=1)
        full = made["codes"][0]["code"]

        row = client.get("/api/redeem", auth=ADMIN).json()["codes"][0]
        assert row["masked"].startswith(full[:4])
        assert row["masked"].endswith(full[-4:])
        assert full[4:-4] not in row["masked"]


def test_the_audit_trail_records_the_count_not_the_cards() -> None:
    with TestClient(app) as client:
        made = _mint(client, count=3, batch="quiet")
        body = str(client.get("/api/audit?limit=20", auth=ADMIN).json())
        assert "redeem.generate" in body
        for card in made["codes"]:
            assert card["code"] not in body


def test_revoking_does_not_write_the_card_into_the_log() -> None:
    with TestClient(app) as client:
        value = _mint(client, count=1)["codes"][0]["code"]
        client.post(f"/api/redeem/{value}/revoke", auth=ADMIN)
        body = str(client.get("/api/audit?limit=20", auth=ADMIN).json())
        assert "redeem.revoke" in body
        assert value not in body


# -- revoke ------------------------------------------------------------------

def test_an_unused_card_can_be_voided() -> None:
    with TestClient(app) as client:
        value = _mint(client, count=1)["codes"][0]["code"]
        r = client.post(f"/api/redeem/{value}/revoke", auth=ADMIN)
        assert r.status_code == 200
        assert r.json()["status"] == "revoked"
        assert client.get("/api/redeem", auth=ADMIN).json()["stats"]["revoked"] == 1


def test_revoking_an_unknown_card_is_404() -> None:
    with TestClient(app) as client:
        assert client.post("/api/redeem/NOSUCHCODE99/revoke",
                           auth=ADMIN).status_code == 404


def test_a_spent_card_cannot_be_rewritten_as_revoked() -> None:
    """History is what the member was actually given; it does not get edited."""
    with TestClient(app) as client:
        value = _mint(client, count=1)["codes"][0]["code"]
        # Spent directly rather than through resolve(): with no bot token
        # stored, every channel reads as closed, which is a different test.
        spent = app.state.registration.consume(
            Admission(allowed=True, via="redeem", credential=value),
            "emby-buyer")
        assert spent is True

        r = client.post(f"/api/redeem/{value}/revoke", auth=ADMIN)
        assert r.status_code >= 400
        assert app.state.registration.get_redeem(value)["status"] == "used"


def test_channels_read_as_closed_until_the_bot_is_configured() -> None:
    """A card is only redeemable through a bot that is actually running."""
    with TestClient(app) as client:
        value = _mint(client, count=1)["codes"][0]["code"]
        verdict = app.state.registration.resolve("777", value)
        assert verdict.allowed is False
        assert app.state.registration.get_redeem(value)["status"] == "unused"


# -- filters -----------------------------------------------------------------

def test_cards_can_be_filtered_by_status_and_batch() -> None:
    with TestClient(app) as client:
        first = _mint(client, count=2, batch="alpha")
        _mint(client, count=3, batch="beta")
        client.post(f"/api/redeem/{first['codes'][0]['code']}/revoke", auth=ADMIN)

        alpha = client.get("/api/redeem?batch=alpha", auth=ADMIN).json()["codes"]
        assert len(alpha) == 2
        beta = client.get("/api/redeem?batch=beta", auth=ADMIN).json()["codes"]
        assert len(beta) == 3
        revoked = client.get("/api/redeem?status=revoked", auth=ADMIN).json()["codes"]
        assert len(revoked) == 1
        unused = client.get("/api/redeem?status=unused", auth=ADMIN).json()["codes"]
        assert len(unused) == 4
        assert client.get("/api/redeem?batch=nothing",
                          auth=ADMIN).json()["codes"] == []


def test_the_list_names_the_group_a_card_is_worth() -> None:
    with TestClient(app) as client:
        _mint(client, count=1)
        row = client.get("/api/redeem", auth=ADMIN).json()["codes"][0]
        assert row["group_name"]
        assert row["group_name"] != "(已删除)"


# -- CSV export --------------------------------------------------------------

def test_the_export_is_csv_and_carries_usable_cards() -> None:
    with TestClient(app) as client:
        made = _mint(client, count=3, batch="export", days=60)
        r = client.get("/api/redeem/export.csv?batch=export", auth=ADMIN)

        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert "attachment" in r.headers.get("content-disposition", "")

        rows = list(csv.DictReader(io.StringIO(r.text)))
        assert len(rows) == 3
        exported = {row["code"] for row in rows}
        assert exported == {c["code"] for c in made["codes"]}
        assert all(row["days"] == "60" for row in rows)
        assert all(row["status"] == "unused" for row in rows)


def test_the_export_respects_the_batch_filter() -> None:
    with TestClient(app) as client:
        _mint(client, count=2, batch="keep")
        _mint(client, count=5, batch="skip")

        body = client.get("/api/redeem/export.csv?batch=keep", auth=ADMIN).text
        assert len(list(csv.DictReader(io.StringIO(body)))) == 2


def test_an_empty_export_is_still_valid_csv() -> None:
    with TestClient(app) as client:
        body = client.get("/api/redeem/export.csv?batch=none", auth=ADMIN).text
        reader = csv.DictReader(io.StringIO(body))
        assert list(reader) == []
        assert reader.fieldnames and "code" in reader.fieldnames


def test_a_note_containing_a_comma_does_not_break_the_columns() -> None:
    with TestClient(app) as client:
        _mint(client, count=1, batch="tricky", note='a,b "quoted"')
        rows = list(csv.DictReader(io.StringIO(
            client.get("/api/redeem/export.csv?batch=tricky", auth=ADMIN).text)))
        assert len(rows) == 1
        assert rows[0]["batch"] == "tricky"


# -- auth --------------------------------------------------------------------

def test_every_redeem_route_requires_auth() -> None:
    with TestClient(app) as client:
        assert client.get("/api/redeem").status_code == 401
        assert client.post("/api/redeem/generate",
                           json={"group_id": "x", "days": 1, "count": 1}
                           ).status_code == 401
        assert client.post("/api/redeem/ABC/revoke").status_code == 401
        assert client.get("/api/redeem/export.csv").status_code == 401


def test_wrong_credentials_are_refused() -> None:
    with TestClient(app) as client:
        pw_value = "definitely" + "-not-the-password"
        assert client.get("/api/redeem",
                          auth=("admin", pw_value)).status_code == 401
