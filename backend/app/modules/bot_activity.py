"""Member activity UX lives only in the private Bot; admin Web is separate."""
from __future__ import annotations

import secrets
import time
from datetime import datetime
from html import escape

from app.modules.bot_views import bandwidth_lines, duration, quota_lines
from app.modules.economy_rules import BEIJING, economy_write, encode
from app.modules.levels import DAY


def beijing_stamp(value):
    return datetime.fromtimestamp(value, BEIJING).strftime('%Y-%m-%d %H:%M 北京时间') if value else '不限期'


def renewal_blockers(info):
    reasons = []
    if not info['managed']:
        reasons.append('⏳ 账号尚未启用等级续费')
    if info['exempt']:
        reasons.append('🛡️ 白名单保留原豁免，无需普通等级续费')
    if info['lifecycle'] != 'ready':
        reasons.append('🗑️ 删除结果待确认，请联系管理员')
    if not info['window_open']:
        reasons.append('⏰ 仅到期前72小时内可续30天')
    q = info['qualification']
    if not q['eligible']:
        reasons.append(f"🎬 近30天需80分钟/3有效日（当前{int(q['seconds']//60)}分钟/{q['days']}日）")
    if info['shortfall']:
        reasons.append(f"🪙 可用积分不足，还缺{info['shortfall']}积分")
    return reasons


class ActivityBotMixin:
    def _activity_summary(self, member):
        levels = self._members._levels
        info = levels.status(member['emby_user_id'])
        level, q, today = info['level'], info['qualification'], info['today']
        title = info['worn_title'] or level['title']
        lines = [f"👤 <b>{escape(str(member.get('username') or '—'))}</b>",
                 self._group_label(member, title=title),
                 self._status_label(member),
                 f"📈 成长 {level['growth']}" + (f" / {level['next_threshold']}" if level.get('next_threshold') else ' · 专属等级' if level['level'] == 8 else ' · 满级'),
                 f"🎁 今日观影奖励 {today['points']}/48积分 · {today['growth']}/60成长"]
        if info['exempt']:
            lines += [f"🪙 积分 {info['balance']}", '🛡️ 白名单 · ♾ 永久']
        else:
            lines += [f"🎬 近30天核验 {int(q['seconds']//60)}/80分钟 · {q['days']}/3有效日",
                      f"🪙 积分 {info['balance']} · 本级续费 {info['renewal_price']}积分/30天",
                      '🤖 自动保号：' + ('已开启' if info['auto_renew'] else '未开启（自愿）'),
                      '⏳ 到期：' + beijing_stamp(info['expires_at']),
                      '🗑️ 删除候选：' + beijing_stamp(info['delete_at'])]
        return lines, info

    @staticmethod
    def _activity_menu(info):
        rows = []
        if info['managed'] and not info['exempt']:
            rows += [[{'text':'⏳ 续费30天 · 预览', 'callback_data':'activity:renew'}],
                     [{'text':'🛑 关闭自动保号' if info['auto_renew'] else '🤖 开启自动保号',
                       'callback_data':'activity:auto:off' if info['auto_renew'] else 'activity:auto:on'}]]
        rows += [[{'text':'🎒 背包','callback_data':'bag'}, {'text':'◀ 我的账号','callback_data':'me'}]]
        return rows

    async def _activity_action(self, chat_id, message_id, member, action, *, actor, in_group=False, callback=False):
        # Even a forwarded/foreign group card never performs a private money action.
        if in_group or str(chat_id) != str(actor):
            await self.send(chat_id, '💬 请私聊Bot，打开「我的账号 → 等级权益与保号」')
            return
        levels = getattr(self._members, '_levels', None)
        if not member or levels is None or str(member.get('tg_user_id')) != str(actor):
            await self.send(chat_id, '🔗 请先绑定账号；等级服务未就绪。')
            return
        uid = member['emby_user_id']
        async def show(text, keyboard):
            if callback:
                return await self._edit(chat_id, message_id, text, keyboard)
            return await self._show(chat_id, text, keyboard)
        back = [[{'text':'◀ 等级权益','callback_data':'activity:status'}]]
        try:
            if action.startswith('confirm:'):
                nonce = action.split(':', 1)[1]
                result = levels.renew(uid, 'bot-renew:'+nonce, bot_intent=(nonce, str(actor), str(chat_id)))
                warning = await self._after_member_change(member)
                await show(f"✅ 续费30天已入账，扣除{result['cost']}积分\n"
                           f"⏪ 原到期：{beijing_stamp(result['previous_expires_at'])}\n"
                           f"⏩ 新到期：{beijing_stamp(result['expires_at'])}\n"
                           'ℹ️ 不重置用量，不解除人工禁用'+warning, back)
                return
            if action.startswith('cancel:'):
                nonce = action.split(':', 1)[1]
                with economy_write(levels.db) as conn:
                    conn.execute('UPDATE activity_renew_intents SET expires_at=0 WHERE nonce=? AND user_id=? '
                                 'AND tg_user_id=? AND chat_id=? AND result_json IS NULL',
                                 (nonce, uid, str(actor), str(chat_id)))
                await show('🔙 已取消本次确认，未新增扣费', back)
                return
            if action in ('auto:on', 'auto:off'):
                enabling = action == 'auto:on'
                text = ('🤖 <b>自动保号</b> · 默认关闭，需主动确认\n\n'
                        '⏰ 到期前24小时\n🎬 近30天80分钟 / 3有效日（每天核验满10分钟）\n'
                        '🪙 余额充足时，自动按当时等级价格扣积分续30天\n'
                        'ℹ️ 未达标不扣费；不解除人工禁用\n'
                        '⚠️ 到期停播，保留72小时后可删除（以管理员开关和复核为准）' if enabling else
                        '🛑 关闭后不再自动扣积分续期\n⏳ 现有到期时间不变，仍可手动续费')
                nonce = secrets.token_hex(12)
                self._pending[self._pkey(chat_id)] = ('activity_auto', time.time()+300,
                    {'nonce':nonce, 'uid':uid, 'actor':str(actor), 'enabled':enabling,
                     'binding':self._account_binding_revision(uid)})
                await show(text, [[{'text':'✅ 确认开启自动扣分续期' if enabling else '🛑 确认关闭自动保号',
                                   'callback_data':'activity:auto-confirm:'+nonce}], *back])
                return
            if action.startswith('auto-confirm:'):
                pending = self._pending.get(self._pkey(chat_id))
                nonce = action.split(':', 1)[1]
                if (not pending or pending[0] != 'activity_auto' or pending[1] <= time.time()
                        or pending[2]['nonce'] != nonce or pending[2]['uid'] != uid or pending[2]['actor'] != str(actor)
                        or pending[2]['binding'] != self._account_binding_revision(uid)):
                    raise ValueError('自动保号确认已失效，请重新选择')
                enabled = pending[2]['enabled']
                levels.set_auto(uid, enabled, tg_uid=str(actor), binding_revision=pending[2]['binding'])
                self._pending.pop(self._pkey(chat_id), None)
                await show('🤖 自动保号已开启：达标才自动扣分续期' if enabled else '🛑 自动保号已关闭', back)
                return
            self._pending.pop(self._pkey(chat_id), None)
            lines, info = self._activity_summary(member)
            if action == 'renew':
                now = int(time.time())
                reasons = renewal_blockers(info)
                expiry = info['expires_at']
                new_expiry = max(now, expiry)+30*DAY if expiry else None
                text = (f"🧾 <b>续费确认 · 30天</b>\n🏷️ 本次价格：{info['renewal_price']}积分\n"
                        f"🪙 可用余额：{info['balance']}积分\n⏪ 原到期：{beijing_stamp(expiry)}\n"
                        f"⏩ 预计新到期：{beijing_stamp(new_expiry)}\n"
                        'ℹ️ 已到期从确认成功起算30天，未到期从原到期顺延\n不重置用量，不解除人工禁用\n')
                if reasons:
                    await show(text+'\n🚫 暂不能续费：\n'+'\n'.join(escape(r) for r in reasons), back)
                    return
                nonce = secrets.token_hex(12)
                snapshot = {'expiry': expiry, 'price': info['renewal_price'], 'level': info['level']['level'],
                            'revision': levels.config()['revision'], 'binding': levels.binding_revision(uid)}
                with economy_write(levels.db) as conn:
                    conn.execute('INSERT INTO activity_renew_intents VALUES(?,?,?,?,?,?,NULL)',
                                 (nonce, uid, str(actor), str(chat_id), encode(snapshot), now+300))
                await show(text+'\n✅ 资格已达标，5分钟内由本人确认（确认时重核）',
                           [[{'text':f"✅ 确认扣{info['renewal_price']}积分续30天", 'callback_data':'activity:confirm:'+nonce}],
                            [{'text':'✖️ 取消，不扣分', 'callback_data':'activity:cancel:'+nonce}]])
                return
            if action != 'status':
                raise ValueError('操作已失效，请重新打开等级权益')
            lines += ['', '👑 <b>最终有效权益</b>', *quota_lines(member, compact=True), *bandwidth_lines(member, compact=True),
                      f"📺 同播：{member.get('max_streams') or '不限'}路 · 🎬 月求片：{member['group'].get('request_quota') or '不限'}次"]
            lines += ['📱 ' + self._device_count_text(member).split('\n')[0],
                      '📥 下载：'+('允许' if member.get('allow_download') else '不允许')+' · 🎞 转码：'+('允许' if member.get('allow_transcode') else '不允许')]
            if self._stats is not None and hasattr(self._stats, 'watch_summary'):
                history = self._stats.watch_summary(uid) or {}
                lines += ['', '📊 观看记录',
                          '近24小时 '+duration(history.get('seconds_24h'))+' · 近30天 '+duration(history.get('seconds_30d')),
                          '累计 '+duration(history.get('recorded_seconds'))]
            if member.get('note'):
                lines.append('📝 备注：'+escape(str(member['note'])))
            await show('\n'.join(lines), self._activity_menu(info))
        except ValueError as exc:
            await show('⚠️ 操作未完成：'+escape(str(exc)), back)
        except Exception:  # noqa: BLE001 - do not expose internal/transport details or invent success
            retry = [[{'text':'🔄 重试原续费确认（不会重复扣款）', 'callback_data':'activity:'+action}]] if action.startswith('confirm:') else []
            await show('❓ 结果暂未确认，请重试原请求或联系管理员核查，勿重复购买', retry+back)
