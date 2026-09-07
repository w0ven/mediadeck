from app.modules.member_ops import sort_rows


def test_expiry_sort_respects_explicit_unlimited_override():
    rows = [
        {"username": "permanent", "expires_at": 200, "expires_at_effective": None},
        {"username": "timed", "expires_at": 100, "expires_at_effective": 100},
    ]
    assert sort_rows(rows, "expires", "desc")[0]["username"] == "timed"


def test_activity_sort_uses_displayed_emby_timestamp():
    rows = [
        {"username": "recent", "last_activity": "2026-09-08T01:00:00.1234567Z", "last_seen_at": 1},
        {"username": "older", "last_activity": "2026-09-07T01:00:00Z", "last_seen_at": 9999999999},
    ]
    assert sort_rows(rows, "last_seen", "desc")[0]["username"] == "recent"
