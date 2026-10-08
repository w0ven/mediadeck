"""Public simulated company news and real-trade-only chart; neither touches assets."""
from __future__ import annotations

import secrets
import time
from datetime import datetime
from io import BytesIO

from PIL import Image, ImageDraw

from app.modules.economy_rules import BEIJING, economy_write
from app.modules.market_data import COMPANIES, DEFAULTS
from app.modules.plugins import Field, Plugin, Spec
from app.modules.poker_image import font

NEWS = [
    ('研发进展', '公司披露新项目试验取得阶段进展，后续量产仍存在不确定性。'),
    ('订单扩张', '公司发布新增意向订单信息，最终履约取决于后续商业谈判。'),
    ('成本压力', '公司报告部分原料供应趋紧，正调整采购与生产安排。'),
    ('工期调整', '公司披露一项项目交付延后，计划加强质量复核。'),
    ('行业观察', '公司公布行业需求调研，市场参与者可自行判断长期影响。'),
    ('管理更新', '公司发布经营沟通纪要，未作收益或回购承诺。'),
]


def news_tick(db, *, now=None):
    clock = time.time() if now is None else float(now)
    slot = int(clock//3600)
    with economy_write(db) as conn:
        if conn.execute('SELECT 1 FROM market_news WHERE slot=?', (slot,)).fetchone(): return False
        c, (title, body) = secrets.choice(COMPANIES), secrets.choice(NEWS)
        conn.execute('INSERT INTO market_news(slot,code,title,body,created_at) VALUES(?,?,?,?,?)',
                     (slot, c['code'], title, body, clock))
        return True  # only the current hour, never catch-up events after downtime


def chart(company, trades):
    """Each point/bar is an actual secondary fill, not an IPO or synthetic quote."""
    im = Image.new('RGB', (1040, 670), '#111e2e')
    d = ImageDraw.Draw(im)
    d.text((40, 26), company['name']+'  '+company['code'], font=font(32), fill='#eff5ff')
    d.text((42, 77), '实际成交序列 · 最近 '+str(len(trades))+' 笔', font=font(19), fill='#8ba1bd')
    d.text((680, 36), '固定认购价 '+str(company['issue_price']), font=font(21), fill='#c6b59a')
    d.text((680, 68), '认购不计二级行情', font=font(17), fill='#8293a8')
    left, right, top, bottom = 92, 986, 143, 443
    if not trades:
        d.rounded_rectangle((42, 122, 996, 565), radius=18, fill='#19293b')
        d.text((399, 293), '暂无成交', font=font(38), fill='#cbd5e2')
        d.text((348, 356), '未生成价格或成交量', font=font(23), fill='#8ba1bd')
    else:
        prices = [t['price'] for t in trades]
        low, high = min(prices), max(prices)
        spread = max(1, high-low)
        low, high = max(0, low-spread*.12), high+spread*.12
        for i in range(5):
            y = top+i*(bottom-top)/4
            value = high-i*(high-low)/4
            d.line((left, y, right, y), fill='#263b50', width=1)
            d.text((20, y-12), f'{value:.1f}', font=font(16), fill='#8ba1bd')
        points = [(left+(right-left)*i/(len(trades)-1) if len(trades)>1 else (left+right)/2,
                   bottom-(t['price']-low)/(high-low)*(bottom-top)) for i,t in enumerate(trades)]
        if len(points)>1: d.line(points, fill='#4ce2b3', width=4)
        for x,y in points: d.ellipse((x-5,y-5,x+5,y+5), fill='#66f0c5')
        max_q = max(t['quantity'] for t in trades)
        w = min(18, max(2, (right-left)/len(trades)*.7))
        for (x,_), t in zip(points,trades,strict=True):
            h=t['quantity']/max_q*80
            d.rectangle((x-w/2, 556-h, x+w/2, 556), fill='#3b7890')
        d.text((43, 580), f'最新 {prices[-1]} 积分/股  ·  本图真实成交 {sum(t["quantity"] for t in trades)} 股', font=font(22), fill='#e0edf8')
        d.text((left, 454), datetime.fromtimestamp(trades[0]['created_at'],BEIJING).strftime('%m-%d %H:%M'), font=font(16), fill='#8ba1bd')
        d.text((right-134, 454), datetime.fromtimestamp(trades[-1]['created_at'],BEIJING).strftime('%m-%d %H:%M'), font=font(16), fill='#8ba1bd')
    d.text((43, 625), '模拟现货市场 · 价格仅由玩家真实成交形成', font=font(18), fill='#7f94ab')
    stream=BytesIO()
    im.save(stream,format='PNG',optimize=True)
    return stream.getvalue()


class MarketPlugin(Plugin):
    spec = Spec(id='stock_market', name='模拟股票', icon='📈', category='points', description='固定供给现货；玩家限价撮合，已有积分交易。', fields=[
        Field('ipo_enabled','开放认购',kind='bool',default=True),
        Field('trading_enabled','开放二级交易',kind='bool',default=True),
        Field('fee_bps','新买单与认购手续费基点（0=免费）',kind='int',default=0,min=0,max=100),
        Field('recommended_codes','首页推荐公司（3个代码，逗号分隔）',default='',help='留空稳定推荐发行价最低的3家公司，其余60公司仍可搜索。'),
        Field('digest_enabled','授权互动群定时资讯',kind='bool',default=False,help='仅现有授权群ID；不使用注册通知群，不启动即推或补发历史。'),
        Field('digest_times','群资讯北京时间（最多两个）',default='10:00,18:00',help='09:00～21:59，每群每天最多两条；未知送达不自动重发。'),
        Field('ipo_limit','每人每公司累计认购上限',kind='int',default=50,min=1,max=1000),
        Field('holding_limit','每人每公司持仓含待买上限',kind='int',default=200,min=1,max=1000),
        Field('order_quantity','单委托最大股数',kind='int',default=100,min=1,max=100),
        Field('max_price','整数价格上限',kind='int',default=1000,min=30,max=1000),
        Field('max_notional','单委托金额上限',kind='int',default=5000,min=30,max=5000),
        Field('max_orders','每人未结委托上限',kind='int',default=20,min=1,max=20),
        Field('order_hours','委托有效小时',kind='int',default=24,min=1,max=168),
        Field('news_enabled','每小时模拟资讯',kind='bool',default=True),
        Field('halted_codes','暂停公司代码（逗号分隔）',default='',help='暂停新单、认购并返还已有未结单资金或股数。固定发行数量和价格不可改。')])

    def validate_config(self,cfg):
        valid={c['code'] for c in COMPANIES}
        codes={x.strip().upper() for x in cfg['halted_codes'].replace('，',',').split(',') if x.strip()}
        if codes-valid: raise ValueError('暂停列表须使用MD001～MD060公司代码')
        cfg['halted_codes']=','.join(sorted(codes))
        if cfg['ipo_limit']>cfg['holding_limit']: raise ValueError('认购上限不能超过持仓上限')
        picks=[x.strip().upper() for x in cfg['recommended_codes'].replace('，',',').split(',') if x.strip()]
        if picks and (len(picks)!=3 or len(set(picks))!=3 or set(picks)-valid):
            raise ValueError('推荐须为3个不同的MD001～MD060代码，或留空自动推荐')
        cfg['recommended_codes']=','.join(picks)
        from app.modules.market_digest import times
        cfg['digest_times']=','.join(f'{m//60:02}:{m%60:02}' for m in times(cfg['digest_times']))

    async def run(self,config):
        if self.ctx.telegram: await self.ctx.telegram._market_tick()
        return self.readonly_status()

    def readonly_status(self):
        db=self.ctx.db
        sinks={r['kind']:r['n'] for r in db.query("SELECT kind,SUM(amount) n FROM play_funds WHERE kind IN ('issuance','fee') GROUP BY kind")}
        from app.modules.market_digest import DigestService
        digest=DigestService(db,getattr(self.ctx.telegram,'_active_bot_id','')).summary()
        return {**digest,'公司':db.one('SELECT COUNT(*) n FROM market_companies')['n'],
                '未发行股数':db.one('SELECT SUM(inventory) n FROM market_companies')['n'],
                '发行资金回收':sinks.get('issuance',0),'手续费回收':sinks.get('fee',0),
                '未结委托':db.one("SELECT COUNT(*) n FROM market_orders WHERE state='open'")['n'],
                '真实成交笔数':db.one('SELECT COUNT(*) n FROM market_trades')['n']}


def market_help(cfg):
    cfg={**DEFAULTS,**cfg}
    return ('📈 <b>模拟股票 · 新手</b>\n\n'
            '先看首页3家公司和真实挂单，1股起；其余60家公司可搜索或看全部。\n'
            '认购按固定发行价（最低10积分/股）买库存，本金回收；玩家转让由买卖单撮合，本金付卖家。每家公司固定1000股，不加发。\n'
            f'新买单/认购费 {cfg["fee_bps"]/100:g}%；确认页显示总额，本人确认后才冻结。无对手盘仅挂单，不保证成交；卖出必须有真实买家。\n'
            '小群流动性低，资讯不会自动涨价；无杠杆、做空、分红、赠送本金或保底回购。\n'
            '未成交可在本人私聊“委托”撤单，退未用积分/股票。原委托保留旧费率。\n\n'
            '<code>/股票</code> 看市场 · <code>/股票 关键词</code> 搜索\n'
            '<code>/持仓</code> · <code>/委托</code> · <code>/成交</code>')


def market_rules(cfg):
    cfg={**DEFAULTS,**cfg}
    return ('📈 <b>模拟股票 · 完整规则</b>\n\n'
            '新手：先看首页3家低价公司和真实挂单，1股起，再在本人私聊确认。新单默认0手续费。其余60家公司可搜索或看全部。\n'
            '小群成交可能很少；卖出必须有真实买家，不承诺随时卖出。资讯不会自动涨价，也不保底回购。\n\n'
            '认购：按固定发行价向公司购买未发行股票，本金回收，不是玩家成交；玩家转让：买卖挂单撮合，本金付给卖家。60家虚构公司、10个行业，每家一次发行1000股，发行价10/15/20/25/30积分，不加发。\n\n'
            '只做现货，无杠杆、做空、分红、赠送本金或保底回购。公司页可认购、买入、卖出；整数股数、整数每股积分限价，先确认后冻结。\n'
            '买单按最高总金额含手续费冻结已有积分，卖单冻结已有股票。买高卖低优先，同价先来先得，成交用在簿被动单价格。跳过本人订单，无对手盘只挂单、不保证成交，不造价格和成交量。\n'
            f'仅买方收费 {cfg["fee_bps"]/100:g}%，卖方不收费；按每个买单累计真实金额向上取整，分次成交只收累计差额。认购按本次金额同费率收费，费用独立回收，不发给其他玩家。\n'
            '部分成交立即退价格差和多冻结费用；撤单或过期退全部未用现金/股票。成本含实买费，卖出按整数比例分摊成本、清仓取全剩余成本；浮盈仅按最新真实二级成交，未成交不虚构估值。\n'
            f'当前每人每公司累计认购最多{cfg["ipo_limit"]}股，持仓含冻结卖股和待买容量最多{cfg["holding_limit"]}股；单委托1～{cfg["order_quantity"]}股，价格1～{cfg["max_price"]}，金额最多{cfg["max_notional"]}，最多{cfg["max_orders"]}个未结委托，{cfg["order_hours"]}小时过期。确认120秒，操作输入10分钟过期。\n'
            '资讯明确标记模拟，不改变成交价、现金或股票。停市/公司暂停取消未结单并退还冻结；费率和限额按确认意图快照，新风险仍受当前开关控制。\n\n'
            '<code>/股票 关键词</code> 搜索 · <code>/认购 MD001 5</code>\n'
            '<code>/买入 MD001 5 10</code> · <code>/卖出 MD001 5 12</code>\n'
            '<code>/持仓</code> · <code>/委托</code> · <code>/成交</code> · <code>/自选</code>\n'
            '交易、持仓和历史仅在本人私聊；群里仅公开公司资讯和实际行情。')
