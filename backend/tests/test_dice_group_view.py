"""Group-card presentation only: compact surface, retained public proof and ACK button."""
from html.parser import HTMLParser

import pytest

from app.modules.dice_view import group_card


class QuoteParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.attrs = []
        self.quoted = []
        self.surface = []
        self.in_quote = False

    def handle_starttag(self, tag, attrs):
        if tag == 'blockquote':
            self.attrs.append(dict(attrs))
            self.in_quote = True

    def handle_endtag(self, tag):
        if tag == 'blockquote':
            self.in_quote = False

    def handle_data(self, data):
        (self.quoted if self.in_quote else self.surface).append(data)


@pytest.mark.parametrize('state', ['opening', 'open', 'locked', 'fixed'])
def test_group_card_keeps_complete_public_commitment_in_expandable_quote(state):
    row = {'nonce': '1234567890abcdef12345678', 'round_number': 6543563,
           'commitment': 'ab'*32, 'deadline': 1791737887, 'state': state}
    body, keys = group_card(row, 8, 'MediaDeckDemoBot')
    parsed = QuoteParser()
    parsed.feed(body)
    assert parsed.attrs == [{'expandable': None}]
    surface, proof = ''.join(parsed.surface), ''.join(parsed.quoted)
    for value in (row['nonce'], row['commitment'], str(row['round_number']), '10分钟'):
        assert value not in surface
        assert value in proof
    collapsed_prefix = '\n'.join(proof.splitlines()[:3])
    assert row['commitment'] not in collapsed_prefix and row['nonce'] not in collapsed_prefix
    assert '封盘' in surface and '已下注 8 人' in surface
    assert '玩家对赌 · 确认后不可撤注' in surface
    assert '无有效开奖数据则全局退款，已有结果按原结果结算' in proof
    assert body.count(row['commitment']) == 1
    assert len(keys) == 1
    assert keys[0][0]['url'] == 'https://t.me/MediaDeckDemoBot?start=dice_'+row['nonce']
    assert keys[0][1] == {'text': '查看本局', 'callback_data': 'dcg:'+row['nonce']}
