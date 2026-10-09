"""Retired collection warnings must not return in the member list."""
import pytest
from playwright.sync_api import expect
from test_audit_web_browser import browser, page, server  # noqa: F401


@pytest.mark.parametrize('nodes', [[], [{'name': 'unavailable-edge', 'ok': False}]])
@pytest.mark.parametrize('used,status,label', [
    (None, 'unavailable', '未测'),
    (0, 'partial', '0 B'),
    (512, 'partial', '512 B'),
    (None, 'no_usage_records', '本月尚无实测记录'),
])
def test_member_usage_omits_retired_warning_without_faking_values(
        page, server, nodes, used, status, label):  # noqa: F811 - shared browser fixtures
    payload = page.request.get(server + '/api/members?page=1&page_size=50').json()
    member = next(row for row in payload['members'] if row['emby_user_id'] == 'audit-viewer')
    member.update(quota_source='measured', measured_used_bytes=used,
                  traffic_used_bytes=999999, traffic_quota_bytes=1024,
                  metering={'measured_used_bytes': used, 'measurement_status': status,
                            'coverage': {'degraded': True, 'nodes': nodes,
                                         'reason': 'no_node_reports'}})
    payload.update(members=[member], total=1)
    page.route('**/api/members?*', lambda route: route.fulfill(json=payload))
    page.evaluate("go('members')")
    row = page.locator('tr[data-id="audit-viewer"]')
    expect(row).to_be_visible()
    usage = row.locator('td').nth(5)
    expect(usage).to_contain_text(label)
    text = usage.inner_text()
    assert '采集不完整' not in text
    assert '待恢复核实' not in text
    assert 'unavailable-edge' not in text
    assert '999999' not in text and '976.6' not in text
    if used is None and status != 'no_usage_records':
        assert '0 B' not in text
    # This is presentation-only: never turn the source coverage into healthy.
    assert member['metering']['coverage']['degraded'] is True
    assert member['measured_used_bytes'] is used
