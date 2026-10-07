"""Formal assets and actual isolated API: independent specs, folded UI, lossless editors."""
import copy
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from app.main import app
from app.modules.economy_rules import DEFAULT_CARDS, encode


def test_actual_percent_activity_and_holiday_editor_save_sources_identity_and_mobile(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR',str(tmp_path.parent))
    artifacts = Path(os.getenv('PACKETS_SHOP_UI_ARTIFACTS',str(tmp_path)))
    artifacts.mkdir(parents=True,exist_ok=True)
    with TestClient(app) as client:
        client.auth=('admin','change-me')
        referenced=app.state.shop.create(dict(DEFAULT_CARDS[1],amount=100,enabled=True))
        a=dict(DEFAULT_CARDS[1],name='isolated A',amount=25,duration_days=3,cost=321)
        b=dict(DEFAULT_CARDS[1],name='isolated B',amount=60,duration_days=5,cost=654)
        h1={'date':'2027-02-06','name':'isolated holiday A','double_ppm':333333,'multiplier':7,
            'drops':[{'ppm':6000,'spec':dict(a,amount=33,duration_days=4)}]}
        h2={'date':'2027-06-09','name':'isolated holiday B','double_ppm':444444,'multiplier':8,
            'drops':[{'ppm':8000,'spec':dict(b,amount=88,duration_days=8)}]}
        original_drops=[{'ppm':1000,'spec':a},{'ppm':2500,'spec':b},{'ppm':4000,'item_id':referenced['id']}]
        response=client.post('/api/plugins/checkin',json={'config':{
            'streak_tiers':encode([{'days':1,'bonus':0},{'days':7,'bonus':15}]),
            'drops':encode(original_drops),'holidays':encode([h1,h2])}})
        assert response.status_code==200
        original_config=copy.deepcopy(response.json()['config'])
        with sync_playwright() as driver:
            browser=driver.chromium.launch(executable_path=os.getenv('MEDIADECK_TEST_CHROMIUM',shutil.which('chromium') or '/usr/bin/chromium'),args=['--no-sandbox'],headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1100})
            page.set_default_timeout(10000)
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            def bridge(route):
                request=route.request
                url=urlsplit(request.url)
                if url.hostname!='panel.test': return route.abort()
                if url.path.startswith(('/api/stream','/api/live')):
                    return route.fulfill(status=200,content_type='text/event-stream',body='')
                headers={k:v for k,v in request.headers.items() if k not in ('host','authorization','content-length','accept-encoding')}
                r=client.request(request.method,url.path+('?' + url.query if url.query else ''),content=request.post_data_buffer,headers=headers)
                route.fulfill(status=r.status_code,body=r.content,headers={k:v for k,v in r.headers.items() if k not in ('content-length','content-encoding')})
            page.route('**/*',bridge)
            page.goto('http://panel.test/#automation')
            page.get_by_role('button',name='积分',exact=True).click()
            card=page.locator('[data-plugin="checkin"]')
            expect(card.locator('[data-tier-percent]').last).to_have_value('15')
            expect(card).to_contain_text('基础正分加成（%）')
            assert card.locator('.plugin-settings-group').count()==3
            for key in ('streak_tiers','drops','holidays'):
                assert not card.locator('#details-pl-checkin-'+key).evaluate('(e)=>e.open')
            assert not card.locator('.holiday-list').evaluate('(e)=>e.open')
            rootdrops=card.locator('[data-field-wrap="checkin-drops"]')
            entries=rootdrops.locator('.item-list > .item-entries > .item-entry')
            expect(entries.nth(0).locator('summary')).to_contain_text('25Mbps')
            expect(entries.nth(0).locator('summary')).to_contain_text('使用起 3 天')
            expect(entries.nth(2).locator('summary')).to_contain_text('100Mbps')
            # Folded entries can be removed/reordered without borrowing another
            # index's spec. Preserve hidden cost and unrelated plugin edits.
            inv=page.locator('#pl-inventory-streams_cap')
            inv.fill('9')
            entries.nth(0).locator('[data-del-entry]').click()
            entries.nth(1).locator('[data-move-entry="-1"]').click()
            entries.nth(1).locator('summary').click()
            entries.nth(1).locator('[data-drop-amount]').fill('75')
            entries.nth(1).locator('[data-drop-days]').fill('6')
            entries.nth(1).locator('summary').click()
            holidays=card.locator('.holiday-list')
            holidays.locator(':scope > summary').click()
            hentries=holidays.locator(':scope > .item-list > .item-entries > .item-entry')
            hentries.nth(1).locator(':scope > summary [data-move-entry="-1"]').click()
            hentries.nth(1).locator(':scope > summary [data-del-entry]').click()
            hentries.nth(0).locator(':scope > summary').click()
            nested=hentries.nth(0).locator('.holiday-drops .item-entry')
            expect(nested.locator('summary')).to_contain_text('88Mbps')
            nested.locator('summary').click()
            nested.locator('[data-drop-amount]').fill('89')
            nested.locator('[data-drop-days]').fill('9')
            hentries.nth(0).locator(':scope > summary').click()
            def save():
                with page.expect_response(lambda r:r.url.endswith('/api/plugins/checkin') and r.request.method=='POST') as saved:
                    card.locator('[data-act="save"]').click()
                assert saved.value.status==200
                return saved.value.json()['config']
            configured=save()
            expected=[{'ppm':4000,'item_id':referenced['id']},{'ppm':2500,'spec':dict(b,amount=75,duration_days=6)}]
            assert json.loads(configured['drops'])==expected
            assert json.loads(configured['holidays'])==[dict(h2,drops=[{'ppm':8000,'spec':dict(b,amount=89,duration_days=9)}])]
            for key in ('weekends','double_ppm','multiplier','streak_tiers'):
                assert configured[key]==original_config[key]
            expect(inv).to_have_value('9')
            assert client.get('/api/plugins/inventory').json()['config']['streams_cap']==10
            # Visual edit -> raw -> visual -> raw, including a parse error.
            advanced=card.locator('#details-pl-checkin-drops')
            advanced.locator('summary').click()
            expect(rootdrops).to_have_attribute('data-editor-source','json')
            textarea=card.locator('#pl-checkin-drops')
            assert json.loads(textarea.input_value())==expected
            textarea.fill('[')
            advanced.locator('summary').click()
            expect(advanced).to_have_attribute('open','')
            expect(rootdrops.locator('[data-editor-error]')).to_contain_text('内容已保留')
            expect(textarea).to_have_value('[')
            raw=[{'ppm':5000,'spec':dict(a,name='isolated RAW',amount=41,duration_days=11)}]
            textarea.fill(json.dumps(raw))
            advanced.locator('summary').click()
            expect(rootdrops).to_have_attribute('data-editor-source','visual')
            expect(rootdrops.locator('.entry-summary')).to_contain_text('41Mbps')
            rootdrops.locator('.item-entry > summary').click()
            rootdrops.locator('[data-drop-amount]').fill('42')
            advanced.locator('summary').click()
            expect(rootdrops).to_have_attribute('data-editor-source','json')
            assert json.loads(textarea.input_value())==[{'ppm':5000,'spec':dict(a,name='isolated RAW',amount=42,duration_days=11)}]
            configured=save()
            assert json.loads(configured['drops'])[0]['spec']['amount']==42
            advanced.locator('summary').click()
            expect(rootdrops).to_have_attribute('data-editor-source','visual')
            # An incomplete inactive visual row must not block a corrected raw
            # configuration; only the explicit active source is validated.
            rootdrops.locator('.item-entry > summary').click()
            rootdrops.locator('[data-drop-val]').fill('')
            advanced.locator('summary').click()
            expect(rootdrops).to_have_attribute('data-editor-source','json')
            textarea.fill(encode([{'ppm':5000,'spec':dict(a,name='isolated corrected',amount=42,duration_days=11)}]))
            assert json.loads(save()['drops'])[0]['spec']['name']=='isolated corrected'
            advanced.locator('summary').click()
            expect(rootdrops).to_have_attribute('data-editor-source','visual')
            # Empty visual list remains a valid explicit no-drop configuration.
            rootdrops.locator('[data-del-entry]').click()
            assert json.loads(save()['drops'])==[]
            card.screenshot(path=str(artifacts/'checkin-settings-desktop.png'))
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+2')
            card.screenshot(path=str(artifacts/'checkin-settings-mobile.png'))
            assert not errors,errors
            browser.close()
