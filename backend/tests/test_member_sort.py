from app.modules.member_ops import sort_rows


def test_expiry_sort_respects_explicit_unlimited_override():
    rows = [
        {"username": "permanent", "expires_at": 200, "expires_at_effective": None},
        {"username": "timed", "expires_at": 100, "expires_at_effective": 100},
    ]
    assert sort_rows(rows, "expires", "desc")[0]["username"] == "timed"


def test_activity_sort_uses_displayed_verified_playback_not_emby_visit():
    rows = [
        {"username": "older", "last_played_at": 100, "last_activity": "2099-09-07T01:00:00Z", "last_seen_at": 9999999999},
        {"username": "recent", "last_played_at": 200.5, "last_seen_at": 1},
        {"username": "unknown", "last_activity": "2099-09-08T01:00:00Z", "last_seen_at": 9999999999},
    ]
    for key in ('last_seen', 'last_played'):
        assert [m['username'] for m in sort_rows(rows, key, 'desc')] == ['recent', 'older', 'unknown']
