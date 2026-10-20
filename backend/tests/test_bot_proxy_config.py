import time
from unittest.mock import AsyncMock

import pytest
from test_bot_routing_rules import actions, tap
from test_bot_routing_rules import bot as bot  # noqa: PLC0414
from test_request_bot import bot as request_bot  # noqa: F401

from app.core.config import StreamNode
from app.core.errors import ConfigError
from app.modules.entry_proxy import member_proxy_config


def nodes():
    return [StreamNode(name='edge-a', base_url='https://edge-a.example.com', probe_url='https://edge-a.example.com/load',
                       sign_secret='private-signing-secret', report_token='private-report-secret',
                       pools=[{'name': 'main', 'emby_prefix': '/media', 'node_path': '/private/media',
                               'url_prefix': '/s/main'}])]


def test_proxy_button_next_to_rules_and_same_card_navigation(bot):
    bot.cfg['playback_routing_rules'] = 'rules: []'
    row = tap(bot, 'me_nodes')['reply_markup']['inline_keyboard'][0]
    assert [b['callback_data'] for b in row] == ['me_nodes', 'me_routing', 'me_proxy']
    page = tap(bot, 'me_proxy')
    assert actions(page) == {'me_proxy:nginx', 'me_proxy:caddy', 'me_nodes'}
    for text in ('proxy.example.com', 'GD', '国内盘', 'nginx -t', 'caddy validate', '勿覆盖', '不直接适用于子路径入口或 Worker'):
        assert text in page['text']
    assert len(page['text']) < 4096
    assert len(bot.transport.messages) == 1


@pytest.mark.parametrize('server,name', [('nginx', 'emby-proxy.conf'), ('caddy', 'Caddyfile')])
def test_download_exact_config_private_and_credential_free(bot, server, name):
    bot._proxy_config = lambda engine: member_proxy_config('https://emby.example.com', nodes(), engine)
    bot._call_multipart = AsyncMock(return_value={'message_id': 888})
    tap(bot, 'me_proxy:'+server)
    method, fields, files = bot._call_multipart.call_args.args
    assert method == 'sendDocument' and fields['chat_id'] == '900'
    filename, content, mime = files['document']
    assert filename == name and mime.startswith('text/plain')
    assert content.decode() == bot._proxy_config(server)
    assert b'private-signing-secret' not in content and b'private-report-secret' not in content
    assert b'/private/media' not in content
    assert b'proxy.example.com' in content and b'/_n/edge-a' in content
    assert b'edge-a.example.com' in content


@pytest.mark.parametrize('action', ['me_proxy', 'me_proxy:nginx', 'me_proxy:caddy'])
def test_no_export_to_group_or_unbound_users_and_pending_input_cleared(bot, action):
    bot._call_multipart = AsyncMock()
    assert tap(bot, action, group=True) == {}
    assert '还没有账号' in tap(bot, action, actor='99999')['text']
    bot._call_multipart.assert_not_called()
    key = bot._pkey('900')
    bot._pending[key] = ('local_draft', time.time()+60, {})
    tap(bot, action)
    assert key not in bot._pending


def test_unavailable_invalid_and_delivery_failure_are_explicit(bot):
    bot._call_multipart = AsyncMock(return_value=None)
    assert '尚未就绪' in tap(bot, 'me_proxy:nginx')['text']
    assert '请选择' in tap(bot, 'me_proxy:unsupported')['text']
    bot._call_multipart.assert_not_called()
    bot._proxy_config = lambda server: 'complete example'
    assert '未确认送达' in tap(bot, 'me_proxy:caddy')['text']


@pytest.mark.parametrize('server', ['nginx', 'caddy'])
def test_generator_is_fixed_upstream_and_retains_security_controls(server):
    config = member_proxy_config('https://emby.example.com', nodes(), server)
    assert 'private-signing-secret' not in config
    assert 'private-report-secret' not in config
    assert 'private, no-store' in config
    assert '600s' in config
    assert 'Location' in config if server == 'caddy' else 'proxy_redirect' in config
    assert 'tls_server_name emby.example.com' in config if server == 'caddy' else 'proxy_ssl_verify on;' in config
    assert 'return 404' in config if server == 'nginx' else 'respond 404' in config


def test_bad_origin_and_ambiguous_nodes_refuse_generation():
    with pytest.raises(ConfigError):
        member_proxy_config('http://emby.example.com', nodes(), 'nginx')
    with pytest.raises(ConfigError):
        member_proxy_config('https://emby.example.com', nodes()*2, 'caddy')
