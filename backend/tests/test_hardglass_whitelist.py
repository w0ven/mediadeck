"""Reserved whitelist identity is decorative, never an administrator role."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from test_admin_commands import bot as base_bot  # noqa: F401

from app.core.errors import ConfigError
from app.main import app
from app.modules.groups import WHITELIST_GROUP_ID


@pytest.fixture
def subject(request):
    return request.getfixturevalue('base_bot')


def test_fixed_whitelist_cannot_be_deleted_even_when_empty(subject):
    before=subject.groups.get(WHITELIST_GROUP_ID)
    with pytest.raises(ConfigError,match='固定系统分组'):
        subject.groups.delete(WHITELIST_GROUP_ID)
    assert subject.groups.get(WHITELIST_GROUP_ID)==before
    assert subject.groups.ensure_whitelist()==0
    rows=[g for g in subject.groups.list() if g['is_builtin']]
    assert len(rows)==1 and rows[0]['id']==WHITELIST_GROUP_ID


def test_decorations_do_not_reset_custom_limits_or_add_roles(subject):
    subject.groups.update(WHITELIST_GROUP_ID,{'name':'内部成员','max_streams':3,'max_devices':4})
    subject.members.upsert('u1','alice',{'group_id':WHITELIST_GROUP_ID})
    before=subject.db.one("SELECT * FROM members WHERE emby_user_id='u1'")
    member=subject.members.get('u1')
    home,keys=subject._home('901','Alice')
    assert '白名单 · 专属成员' in home and '内部成员' in home
    assert 'admin' not in {b.get('callback_data') for row in keys for b in row}
    assert member['roles']==[] and member['max_streams']==3 and member['max_devices']==4
    assert '白名单 · 专属成员' in subject._user_card(member)
    assert '白名单 · 专属成员' in subject._usage_text(member)
    assert subject.db.one("SELECT * FROM members WHERE emby_user_id='u1'")==before
    subject.groups.seed_defaults()
    assert subject.groups.get(WHITELIST_GROUP_ID)['name']=='内部成员'
    assert subject.groups.get(WHITELIST_GROUP_ID)['max_streams']==3


def test_white_named_vip_and_admin_role_are_not_whitelist(subject):
    subject.groups.update('vip',{'name':'白名单 VIP'})
    subject.members.upsert('u1','alice',{'group_id':'vip'})
    home,_=subject._home('901','Alice')
    assert '白名单 · 专属成员' not in home and '固定分组' not in home
    admin_home,keys=subject._home('900','Admin')
    assert '白名单 · 专属成员' not in admin_home
    assert 'admin' in {b.get('callback_data') for row in keys for b in row}


def test_white_user_is_still_refused_admin_mutation(subject):
    subject.members.upsert('u1','alice',{'group_id':WHITELIST_GROUP_ID})
    before=subject.members.get('admin1')['group_id']
    asyncio.run(subject._handle_message({'chat':{'id':'901','type':'private'},'from':{'id':'901'},'text':'/prouser root'}))
    assert subject.members.get('admin1')['group_id']==before
    assert not any('admin' in str(keyboard) for _,_,keyboard in subject.sent)


def test_reserved_group_id_survives_renaming_and_name_is_escaped(subject):
    subject.groups.update(WHITELIST_GROUP_ID,{'name':'<b>Internal</b>'})
    subject.members.upsert('u1','alice',{'group_id':WHITELIST_GROUP_ID})
    home,_=subject._home('901','Alice')
    assert '&lt;b&gt;Internal&lt;/b&gt;' in home
    assert '白名单 · 专属成员' in home


def test_fixed_group_api_refusal_and_theme_assets_are_served():
    with TestClient(app) as client:
        auth=('admin','change-me')
        before=app.state.groups.get(WHITELIST_GROUP_ID)
        response=client.delete('/api/groups/whitelist',auth=auth)
        assert response.status_code==422 and '固定系统分组' in response.text
        assert app.state.groups.get(WHITELIST_GROUP_ID)==before
        page=client.get('/',auth=auth)
        assert 'hardglass.css?v=' in page.text and 'workspace.js?v=' in page.text
        css=client.get('/static/hardglass.css')
        assert css.status_code==200 and '#member-detail:not(.hidden)' in css.text
