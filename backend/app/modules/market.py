"""Real-point, fixed-share cash spot market. Matching and every asset effect are atomic."""
from __future__ import annotations

import json
import secrets
import time

from app.modules.economy_rules import economy_write, encode
from app.modules.group_points import GroupPointsError, reliable_user
from app.modules.market_data import DEFAULTS, fee
from app.modules.play_money import CashBook, PlayError, integer
from app.modules.play_rounds import PlayAccess


class MarketService(PlayAccess):
    def __init__(self, db, members, points, config, enabled, bot_id):
        super().__init__(db, members, lambda chat: True, bot_id)
        self.points, self.cash = points, CashBook(points)
        self.config, self.enabled = config, enabled

    def company(self, code):
        return self.db.one('SELECT * FROM market_companies WHERE code=?', (str(code).upper(),))

    def featured(self):
        cfg = {**DEFAULTS, **self.config()}
        codes = [x.strip().upper() for x in cfg['recommended_codes'].split(',') if x.strip()]
        if codes:
            return [self.company(code) for code in codes if self.company(code)]
        return self.db.query('SELECT * FROM market_companies ORDER BY issue_price,code LIMIT 3')

    def public_book(self, *, code=None, now=None):
        clock = time.time() if now is None else float(now)
        return self.db.query("SELECT o.nonce,o.code,o.side,o.price,o.remaining,c.name FROM market_orders o JOIN market_companies c ON c.code=o.code WHERE o.state='open' AND o.remaining>0 AND o.expires_at>? AND c.halted=0" + (' AND o.code=?' if code else '') + ' ORDER BY o.code,o.side,o.price,o.id', (clock, code) if code else (clock,))

    def position(self, uid, code):
        return self.db.one('SELECT * FROM market_positions WHERE user_id=? AND code=?', (str(uid), code)) or {'user_id': str(uid), 'code': code, 'shares': 0, 'locked': 0, 'cost': 0, 'realized': 0}

    def _capacity(self, conn, uid, code):
        owned = self.position(uid, code)['shares']
        pending = conn.execute("SELECT COALESCE(SUM(remaining),0) n FROM market_orders WHERE user_id=? AND code=? AND side='buy' AND state='open'", (uid, code)).fetchone()['n']
        return owned+pending

    def _check(self, conn, uid, operation, code, quantity, price, cfg):
        c = self.company(code)
        if not c: raise PlayError('公司代码不存在')
        paused={s.strip().upper() for s in str(self.config().get('halted_codes') or '').split(',') if s.strip()}
        if c['halted'] or code in paused: raise PlayError('该公司已暂停交易和认购')
        if not self.enabled(): raise PlayError('模拟股票暂未开放')
        integer(quantity, '股数', 1, cfg['ipo_limit'] if operation == 'ipo' else cfg['order_quantity'])
        integer(price, '限价', 1, cfg['max_price'])
        if quantity*price > cfg['max_notional']: raise PlayError('本单金额超过限额')
        count = conn.execute("SELECT COUNT(*) n FROM market_orders WHERE user_id=? AND state='open'", (uid,)).fetchone()['n']
        if operation != 'ipo' and count >= cfg['max_orders']: raise PlayError('未结委托数已达上限')
        current_cfg = {**DEFAULTS, **self.config()}
        if operation == 'ipo':
            if not cfg['ipo_enabled'] or not current_cfg['ipo_enabled']: raise PlayError('认购暂未开放')
            if price != c['issue_price']: raise PlayError('发行价意图不一致')
            used = conn.execute('SELECT COALESCE(SUM(quantity),0) n FROM market_subscriptions WHERE user_id=? AND code=?', (uid, code)).fetchone()['n']
            if used+quantity > cfg['ipo_limit']: raise PlayError('本公司累计认购已达限额')
            if c['inventory'] < quantity: raise PlayError('发行库存不足')
        elif not cfg['trading_enabled'] or not current_cfg['trading_enabled']: raise PlayError('二级交易暂未开放')
        if operation in ('ipo', 'buy'):
            if self._capacity(conn, uid, code)+quantity > cfg['holding_limit']: raise PlayError('持仓加待买数量超过限额')
            if self.points.balance(uid) < price*quantity+fee(price*quantity, cfg['fee_bps']): raise PlayError('可用积分不足，未扣款')
        elif self.position(uid, code)['shares']-self.position(uid, code)['locked'] < quantity:
            raise PlayError('可卖股数不足，未挂单')

    def preview(self, actor, operation, code, quantity, price=None, *, now=None, request_key=''):
        clock = time.time() if now is None else float(now)
        if operation not in ('ipo', 'buy', 'sell'): raise PlayError('股票操作无效')
        with economy_write(self.db) as conn:
            self._maintain(conn, clock)
            tg, member = self.actor(actor, now=clock)
            code = str(code).upper()
            c = self.company(code)
            if not c: raise PlayError('公司代码不存在')
            cfg = {**DEFAULTS, **self.config()}
            if operation == 'ipo': price = c['issue_price']
            if request_key:
                if not isinstance(request_key,str) or len(request_key)>120: raise PlayError('原消息来源无效')
                prior=conn.execute('SELECT * FROM market_intents WHERE bot_id=? AND user_id=? AND request_key=?',(self.bot_id,member['emby_user_id'],request_key)).fetchone()
                if prior:
                    if (prior['tg_id']!=tg or prior['operation']!=operation or prior['code']!=code or prior['quantity']!=quantity or prior['price']!=price):
                        raise PlayError('原消息已用于其他意图，请发送新消息')
                    return dict(prior)
            self._check(conn, member['emby_user_id'], operation, code, quantity, price, cfg)
            nonce = secrets.token_hex(12)
            conn.execute('INSERT INTO market_intents(nonce,bot_id,user_id,tg_id,operation,code,quantity,price,config_json,created_at,expires_at,request_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                         (nonce, self.bot_id, member['emby_user_id'], tg, operation, code, quantity, price, encode(cfg), clock, clock+120, request_key))
            return self.intent(nonce)

    def intent(self, nonce):
        return self.db.one('SELECT * FROM market_intents WHERE nonce=?', (str(nonce),))

    def confirm(self, nonce, actor, *, now=None):
        clock = time.time() if now is None else float(now)
        with economy_write(self.db) as conn:
            tg, member = self.actor(actor, now=clock)
            intent = self.intent(nonce)
            if (not intent or intent['bot_id'] != self.bot_id or intent['tg_id'] != tg or intent['user_id'] != member['emby_user_id']):
                raise PlayError('仅原确认本人可操作')
            if intent['state'] == 'done': return json.loads(intent['result_json'])
            if intent['state'] != 'preview' or intent['expires_at'] <= clock: raise PlayError('确认已过期，请重新下单')
            self._maintain(conn, clock)
            cfg = json.loads(intent['config_json'])
            uid, code, op, q, price = (intent[k] for k in ('user_id', 'code', 'operation', 'quantity', 'price'))
            self._check(conn, uid, op, code, q, price, cfg)
            if op == 'ipo': result = self._subscribe(conn, intent, cfg, clock)
            else:
                cur = conn.execute('INSERT INTO market_orders(nonce,user_id,tg_id,code,side,price,quantity,remaining,fee_bps,config_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (nonce, uid, tg, code, op, price, q, q, cfg['fee_bps'] if op == 'buy' else 0, encode(cfg), clock, clock+cfg['order_hours']*3600))
                if op == 'buy': self.cash.reserve(conn, 'market', nonce, uid, price*q+fee(price*q, cfg['fee_bps']), now=clock)
                else: self._move(conn, uid, code, nonce, clock, locked=q)
                self._match(conn, cur.lastrowid, clock)
                order = self.db.one('SELECT * FROM market_orders WHERE id=?', (cur.lastrowid,))
                result = {'operation': op, 'order_nonce': nonce, 'code': code, 'quantity': q,
                          'filled': q-order['remaining'], 'price': price, 'state': order['state']}
            conn.execute("UPDATE market_intents SET state='done',result_json=? WHERE nonce=?", (encode(result), nonce))
            return result

    def _subscribe(self, conn, intent, cfg, clock):
        uid, code, q, price, nonce = (intent[k] for k in ('user_id', 'code', 'quantity', 'price', 'nonce'))
        gross, charge = q*price, fee(q*price, cfg['fee_bps'])
        self.cash.reserve(conn, 'ipo', nonce, uid, gross+charge, now=clock)
        self.cash.consume(conn, 'ipo', nonce, uid, gross, 'issuance', code, now=clock)
        if charge: self.cash.consume(conn, 'ipo', nonce, uid, charge, 'fee', 'ipo', now=clock)
        changed = conn.execute('UPDATE market_companies SET inventory=inventory-? WHERE code=? AND inventory>=?', (q, code, q))
        if changed.rowcount != 1: raise PlayError('发行库存已变化')
        self._move(conn, uid, code, nonce, clock, shares=q, cost=gross+charge)
        conn.execute('INSERT INTO market_stock_moves(code,user_id,ref,shares_delta,created_at) VALUES(?,?,?,?,?)', (code, '', nonce, -q, clock))
        conn.execute('INSERT INTO market_subscriptions(nonce,user_id,code,quantity,gross,fee,created_at) VALUES(?,?,?,?,?,?,?)', (nonce, uid, code, q, gross, charge, clock))
        return {'operation': 'ipo', 'code': code, 'quantity': q, 'price': price, 'fee': charge}

    @staticmethod
    def _move(conn, uid, code, ref, clock, *, shares=0, locked=0, cost=0, realized=0):
        conn.execute('INSERT OR IGNORE INTO market_positions(user_id,code) VALUES(?,?)', (uid, code))
        conn.execute('UPDATE market_positions SET shares=shares+?,locked=locked+?,cost=cost+?,realized=realized+? WHERE user_id=? AND code=?', (shares, locked, cost, realized, uid, code))
        conn.execute('INSERT INTO market_stock_moves(code,user_id,ref,shares_delta,lock_delta,cost_delta,realized_delta,created_at) VALUES(?,?,?,?,?,?,?,?)', (code, uid, ref, shares, locked, cost, realized, clock))

    def _valid_order(self, order, clock):
        try:
            _, member = self.actor({'id': int(order['tg_id']), 'is_bot': False}, now=clock)
            return member['emby_user_id'] == order['user_id']
        except (PlayError, ValueError): return False

    def _match(self, conn, order_id, clock):
        while True:
            incoming = dict(conn.execute('SELECT * FROM market_orders WHERE id=?', (order_id,)).fetchone())
            if incoming['state'] != 'open' or not incoming['remaining']: return
            side = incoming['side']
            inequality, ordering = ('<=', 'ASC') if side == 'buy' else ('>=', 'DESC')
            other = conn.execute(f"SELECT * FROM market_orders WHERE code=? AND side=? AND state='open' AND price{inequality}? AND user_id<>? AND tg_id<>? ORDER BY price {ordering},id ASC LIMIT 1",
                                 (incoming['code'], 'sell' if side == 'buy' else 'buy', incoming['price'], incoming['user_id'], incoming['tg_id'])).fetchone()
            if not other: return
            other = dict(other)
            if other['expires_at'] <= clock:
                self._close(conn, other, 'expired', clock)
                continue
            if not self._valid_order(other, clock):
                self._close(conn, other, 'invalid', clock)
                continue
            if not self._valid_order(incoming, clock):
                self._close(conn, incoming, 'invalid', clock)
                return
            buy, sell = (incoming, other) if side == 'buy' else (other, incoming)
            q, price = min(buy['remaining'], sell['remaining']), other['price']
            gross = q*price
            cumulative_fee = fee(buy['gross']+gross, buy['fee_bps'])
            charge = cumulative_fee-buy['fee']
            seller_position = self.position(sell['user_id'], sell['code'])
            cost = seller_position['cost']*q//seller_position['shares']
            self.cash.transfer_held(conn, 'market', buy['nonce'], buy['user_id'], sell['user_id'], gross, now=clock)
            if charge: self.cash.consume(conn, 'market', buy['nonce'], buy['user_id'], charge, 'fee', 'secondary', now=clock)
            cur = conn.execute('INSERT INTO market_trades(code,quantity,price,buy_order,sell_order,buyer,seller,fee,seller_cost,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (buy['code'], q, price, buy['id'], sell['id'], buy['user_id'], sell['user_id'], charge, cost, clock))
            ref = 'trade:'+str(cur.lastrowid)
            self._move(conn, sell['user_id'], sell['code'], ref, clock, shares=-q, locked=-q, cost=-cost, realized=gross-cost)
            self._move(conn, buy['user_id'], buy['code'], ref, clock, shares=q, cost=gross+charge)
            for order in (buy, sell):
                conn.execute("UPDATE market_orders SET remaining=remaining-?,gross=gross+?,fee=fee+?,state=CASE WHEN remaining=? THEN 'filled' ELSE 'open' END WHERE id=?", (q, gross, charge if order['side'] == 'buy' else 0, q, order['id']))
            updated_buy = dict(conn.execute('SELECT * FROM market_orders WHERE id=?', (buy['id'],)).fetchone())
            worst_future_gross = updated_buy['remaining']*updated_buy['price']
            target = worst_future_gross+fee(updated_buy['gross']+worst_future_gross, updated_buy['fee_bps'])-updated_buy['fee']
            held = self.cash.held(conn, 'market', buy['nonce'], buy['user_id'])
            if held < target: raise RuntimeError('buy reserve invariant violated')
            if held > target: self.cash.release(conn, 'market', buy['nonce'], buy['user_id'], held-target, now=clock)

    def _close(self, conn, order, state, clock):
        if order['state'] != 'open': return False
        if order['side'] == 'buy': self.cash.release(conn, 'market', order['nonce'], order['user_id'], now=clock)
        else: self._move(conn, order['user_id'], order['code'], order['nonce'], clock, locked=-order['remaining'])
        conn.execute('UPDATE market_orders SET state=? WHERE id=? AND state=?', (state, order['id'], 'open'))
        return True

    def cancel(self, nonce, actor, *, now=None):
        clock = time.time() if now is None else float(now)
        try: tg = reliable_user({'from': actor})
        except GroupPointsError as exc: raise PlayError(str(exc)) from None
        # Withdrawal/return of already-owned assets stays available for disabled members.
        with economy_write(self.db) as conn:
            member = self.members.find_by_telegram(tg)
            order = conn.execute('SELECT * FROM market_orders WHERE nonce=?', (str(nonce),)).fetchone()
            if not member or not order or order['user_id'] != member['emby_user_id'] or order['tg_id'] != tg: raise PlayError('仅委托本人可撤单')
            self._close(conn, dict(order), 'expired' if order['expires_at'] <= clock else 'cancelled', clock)
            return self.db.one('SELECT * FROM market_orders WHERE id=?', (order['id'],))

    def _maintain(self, conn, clock):
        enabled = self.enabled() and {**DEFAULTS, **self.config()}['trading_enabled']
        validity = {}
        rows = conn.execute("SELECT o.*,c.halted FROM market_orders o JOIN market_companies c ON c.code=o.code WHERE o.state='open' ORDER BY o.id").fetchall()
        count = 0
        for r in rows:
            row = dict(r)
            key = (row['user_id'], row['tg_id'])
            state = 'closed' if not enabled else 'halted' if row['halted'] else 'expired' if row['expires_at'] <= clock else ''
            if not state:
                if key not in validity: validity[key] = self._valid_order(row, clock)
                if not validity[key]: state = 'invalid'
            if state:
                self._close(conn, row, state, clock)
                count += 1
        conn.execute("UPDATE market_intents SET state='expired' WHERE state='preview' AND expires_at<=?", (clock,))
        return count

    def maintain(self, *, now=None):
        clock = time.time() if now is None else float(now)
        with economy_write(self.db) as conn: return self._maintain(conn, clock)

    def portfolio(self, actor):
        _, member = self.actor(actor)
        rows = self.db.query('SELECT p.*,c.name,c.issue_price,(SELECT price FROM market_trades t WHERE t.code=p.code ORDER BY t.id DESC LIMIT 1) last_price FROM market_positions p JOIN market_companies c ON c.code=p.code WHERE p.user_id=? AND (p.shares>0 OR p.realized<>0) ORDER BY p.code', (member['emby_user_id'],))
        for row in rows:
            row['floating'] = row['last_price']*row['shares']-row['cost'] if row['last_price'] is not None and row['shares'] else None
        return rows

    def orders(self, actor, *, include_closed=False):
        _, member = self.actor(actor)
        return self.db.query('SELECT * FROM market_orders WHERE user_id=?'+('' if include_closed else " AND state='open'")+ ' ORDER BY id DESC', (member['emby_user_id'],))

    def trades(self, actor):
        _, member = self.actor(actor)
        uid = member['emby_user_id']
        return self.db.query('SELECT * FROM market_trades WHERE buyer=? OR seller=? ORDER BY id DESC', (uid, uid))
