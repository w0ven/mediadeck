"""Account audit: isolated real SQLite, no external service calls."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from app.core.db import Database
from app.modules.enforcement import EnforcementService, desired_policy
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.plugins_points import CheckinPlugin
from app.modules.points import PointsService
from app.modules.registration import RegistrationService
from app.modules.requests import RequestError, RequestService
from app.modules.shop import ShopError, ShopService
from app.modules.telegram import TelegramBot


@pytest.fixture
def stack(tmp_path):
    db = Database(tmp_path / 'accounts.db')
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    members.upsert('u1', 'viewer', {'group_id': 'standard'})
    points = PointsService(db)
    points.add('u1', 1000, 'admin.adjust')
    yield SimpleNamespace(db=db, groups=groups, members=members, points=points,
                          shop=ShopService(db, members, points))
    db.close()


def test_shop_extends_effective_override_not_hidden_base(stack):
    expires = int(time.time()) + 90 * 86400
    stack.members.set_overrides('u1', {'expires_at_override': expires, 'max_streams': 4})
    item = stack.shop.create({'kind': 'days', 'name': 'Term', 'cost': 10, 'amount': 7})
    stack.shop.redeem('u1', item['id'])
    member = stack.members.get('u1')
    assert member['expires_at_effective'] == expires + 7 * 86400
    assert member['overrides']['max_streams'] == 4


@pytest.mark.parametrize('kind,group', [('days', 'whitelist'), ('traffic', 'whitelist')])
def test_shop_never_charges_for_ineffective_reward(stack, kind, group):
    stack.members.upsert('u1', 'viewer', {'group_id': group})
    before = stack.members.get('u1')
    item = stack.shop.create({'kind': kind, 'name': 'Reward', 'cost': 10, 'amount': 7})
    with pytest.raises(ShopError):
        stack.shop.redeem('u1', item['id'])
    assert stack.points.balance('u1') == 1000
    assert not stack.shop.orders('u1')
    assert stack.members.get('u1')['expires_at'] == before['expires_at']


def test_checkin_payment_failure_does_not_spend_day(stack):
    plugin = CheckinPlugin(SimpleNamespace(db=stack.db, points=stack.points))
    stack.db.execute("CREATE TRIGGER fail_checkin BEFORE INSERT ON points_ledger WHEN NEW.reason='checkin' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(Exception, match='injected'):
        plugin.checkin('u1')
    assert stack.db.query('SELECT * FROM checkins') == []
    assert stack.points.balance('u1') == 1000
    stack.db.execute('DROP TRIGGER fail_checkin')
    assert plugin.checkin('u1')['ok']
    assert not plugin.checkin('u1')['ok']


def test_request_quota_rechecked_after_enrichment(stack):
    stack.groups.update('standard', {'request_quota': 1})
    class Metadata:
        async def resolve(self, kind, ident):
            await asyncio.sleep(0)
            return kind, None
    requests = RequestService(stack.db, stack.members, stack.groups, Metadata())
    async def run():
        return await asyncio.gather(requests.create('u1', 'movie', 101),
                                    requests.create('u1', 'movie', 102), return_exceptions=True)
    results = asyncio.run(run())
    assert sum(isinstance(r, RequestError) for r in results) == 1
    assert len(requests.list()) == requests.used('u1') == 1


def test_request_deleted_during_enrichment_is_refused(stack):
    class Metadata:
        async def resolve(self, kind, ident):
            stack.members.delete('u1')
            return kind, None
    requests = RequestService(stack.db, stack.members, stack.groups, Metadata())
    with pytest.raises(RequestError):
        asyncio.run(requests.create('u1', 'movie', 101))
    assert not requests.list()


def test_enforcement_dry_run_does_not_mutate_fingerprint(stack):
    class Emby:
        async def list_users(self):
            return [{'Id': 'u1', 'Policy': desired_policy(stack.members.get('u1'))}]
    result = asyncio.run(EnforcementService(stack.db, stack.members, Emby()).reconcile())
    assert result['applied'] == 0
    assert not stack.members.get('u1')['applied_fingerprint']


@pytest.mark.parametrize('method', ['enforce_now', 'reconcile'])
def test_enforcement_refreshes_member_after_remote_read(stack, method):
    stack.members.set_status('u1', 'suspended')
    class Emby:
        def __init__(self):
            self.policies = []
        async def list_users(self):
            stack.members.set_status('u1', 'active')
            return [{'Id': 'u1', 'Policy': {}}]
        async def apply_member_policy(self, uid, policy):
            self.policies.append(policy)
            return {'status': 'applied'}
    emby = Emby()
    enforcement = EnforcementService(stack.db, stack.members, emby)
    asyncio.run(enforcement.enforce_now('u1') if method == 'enforce_now' else enforcement.reconcile(apply=True))
    assert emby.policies and emby.policies[0]['IsDisabled'] is False


def test_invite_cannot_be_consumed_after_expiry(stack, monkeypatch):
    registration = RegistrationService(stack.db, stack.groups)
    invite = registration.issue_invite('u1', ttl_days=1)
    admission = registration.resolve('12', invite['code'])
    monkeypatch.setattr('app.modules.registration.time.time', lambda: invite['expires_at'])
    assert not registration.consume(admission, 'u2')
    assert registration.get_invite(invite['code'])['uses_left'] == 1


def test_bot_reports_effective_not_stored_status(stack):
    stack.members.upsert('u1', 'viewer', {'expires_at': int(time.time()) - 10})
    assert '过期' in TelegramBot._status_label(stack.members.get('u1'))


def test_delete_empty_remote_list_is_not_proof_of_absence(stack):
    from app.modules.member_ops import execute_delete
    class Emby:
        async def list_users(self):
            return []
        async def delete_user(self, uid):
            return False
    result = asyncio.run(execute_delete(stack.members, Emby(), 'u1', actor='test'))
    assert not result['ok']
    assert stack.members.get('u1')


@pytest.mark.parametrize('text', ['{"password": "example secret"}',
                                  'Authorization: Bearer example-secret',
                                  'https://api.telegram.org/bot123:example-secret/sendMessage'])
def test_remote_errors_are_secret_safe(text):
    from app.modules.member_ops import redact
    assert 'example' not in redact(text)


def test_checkin_double_call_threads_returns_normal_refusal(stack):
    from concurrent.futures import ThreadPoolExecutor
    plugin = CheckinPlugin(SimpleNamespace(db=stack.db, points=stack.points))
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: plugin.checkin('u1'), range(2)))
    assert sum(r['ok'] for r in results) == 1
    assert stack.points.balance('u1') == 1010


def test_group_default_switch_failure_keeps_original_default(stack):
    stack.db.execute("CREATE TRIGGER fail_group BEFORE INSERT ON groups WHEN NEW.id='broken' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(Exception, match='injected'):
        stack.groups.create({'id': 'broken', 'name': 'Broken', 'billing_mode': 'none', 'is_default': 1})
    assert stack.groups.default_group_id() == 'standard'


def test_invite_mint_failure_refunds_quota_atomically(stack):
    registration = RegistrationService(stack.db, stack.groups)
    registration.adjust_quota('u1', 1)
    stack.db.execute("CREATE TRIGGER fail_invite BEFORE INSERT ON invite_codes BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(ValueError, match='生成邀请码失败'):
        registration.spend_quota_for_invite('u1')
    assert registration.invite_quota('u1') == 1
    assert not registration.list_invites('u1')


def test_bind_failure_does_not_detach_previous_holder(stack):
    stack.members.upsert('u2', 'other', {'group_id': 'standard'})
    stack.members.bind_telegram('u1', '12')
    stack.db.execute("CREATE TRIGGER fail_bind BEFORE UPDATE OF tg_user_id ON members WHEN NEW.emby_user_id='u2' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(Exception, match='injected'):
        stack.members.bind_telegram('u2', '12')
    assert stack.members.find_by_telegram('12')['emby_user_id'] == 'u1'


def test_invite_slot_cannot_be_spent_by_concurrent_mints(stack, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    registration = RegistrationService(stack.db, stack.groups)
    registration.adjust_quota('u1', 1)
    original = registration.invite_quota
    def quota(uid):
        value = original(uid)
        time.sleep(0.01)
        return value
    monkeypatch.setattr(registration, 'invite_quota', quota)
    def mint(_):
        try:
            registration.spend_quota_for_invite('u1')
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(mint, range(8)))
    assert sum(results) == 1
    assert len(registration.list_invites('u1')) == 1


def test_confirmed_request_type_is_preserved_at_creation(stack):
    class Metadata:
        async def resolve(self, kind, ident):
            return 'tv', {'title': 'Different work'}
    service = RequestService(stack.db, stack.members, stack.groups, Metadata())
    row = asyncio.run(service.create('u1', 'movie', 101, confirmed_type=True))
    assert row['media_type'] == 'movie' and not row['title']
