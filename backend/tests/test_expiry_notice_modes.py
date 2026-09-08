"""Reminder selection must use the same effective term as account access."""
import json

from test_group_billing_modes import stack  # noqa: F401


def test_personal_deadline_is_not_missed_when_base_date_is_far(stack):
    db, groups, members, now, overrides = stack
    overrides['expires_at_override'] = now + 2 * 86400
    db.execute('UPDATE members SET overrides_json=? WHERE emby_user_id=?', (json.dumps(overrides), 'u'))
    assert [m['emby_user_id'] for m in members.expiring_within(7)] == ['u']


def test_traffic_only_stale_deadline_does_not_send_expiry_reminder(stack):
    db, groups, members, now, overrides = stack
    overrides['expires_at_override'] = now + 86400
    db.execute("UPDATE members SET group_id='vip',expires_at=?,overrides_json=? WHERE emby_user_id='u'", (now + 2 * 86400, json.dumps(overrides)))
    assert members.expiring_within(7) == []


def test_explicit_unlimited_overlay_suppresses_base_reminder(stack):
    db, groups, members, now, overrides = stack
    overrides['expires_at_override'] = None
    db.execute("UPDATE members SET expires_at=?,overrides_json=? WHERE emby_user_id='u'", (now + 2 * 86400, json.dumps(overrides)))
    assert members.expiring_within(7) == []
