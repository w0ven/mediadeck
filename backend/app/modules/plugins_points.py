"""Points plugins: check-in and transfer.

These two are plugins rather than hard-coded features for one reason: the
operator needs a switch. A server that does not want a points economy should
be able to turn it off completely, and the bot keyboard has to follow that
decision -- offering a 「签到」 button that answers "this is disabled" is worse
than not offering it.

Both differ from the task plugins in when they act. A task plugin does its
work inside ``run()`` on a timer; these do their work when a member taps a
button, and ``run()`` only reports. That is deliberate: awarding points on a
schedule would mean the panel deciding that someone checked in, and the whole
point of a check-in is that the member showed up.

So ``run()`` here answers "is this working, and how much is it paying out",
which is the question an operator actually has when looking at the card.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from typing import Any

from app.core.config import settings
from app.modules.economy_rules import (
    DEFAULT_CARDS,
    PPM,
    RULE_VERSION,
    activity_for,
    day_bounds,
    default_holidays,
    draw,
    economy_write,
    encode,
    percent_tiers,
    receipt,
    save_receipt,
    validate_card,
    validate_checkin,
    watch_progress,
)
from app.modules.inventory import InventoryService
from app.modules.plugins import Field, Plugin, Spec


def _today(now: float | None = None) -> str:
    return day_bounds(time.time() if now is None else now)[0]


def _yesterday(now: float | None = None) -> str:
    return _today((time.time() if now is None else now) - 86400)


CHECKIN_BASE_SIZE = 1000
CHECKIN_BASE_RULE = 'checkin-weighted-base-v1'
# Per-integer slots: 10*1 + 10*60 + 10*30 + 10*9 = 1000.
CHECKIN_BASE_BANDS = ((-10, -1, 1), (1, 10, 60), (11, 20, 30), (21, 30, 9))


def checkin_base(roll: int) -> int:
    """Map one unbiased HMAC draw to the confirmed fixed base distribution."""
    if type(roll) is not int or not 0 <= roll < CHECKIN_BASE_SIZE:
        raise ValueError('签到基础抽样范围无效')
    for low, high, slots in CHECKIN_BASE_BANDS:
        width = (high - low + 1) * slots
        if roll < width:
            return low + roll // slots
        roll -= width
    raise ValueError('签到基础权重无效')


class CheckinPlugin(Plugin):
    """Daily check-in. One payout per member per calendar day.

    Beijing-day verified watch evidence gates admission. The base draw is
    weighted at 1/60/30/9 percent over -10..-1/1..10/11..20/21..30, with no
    zero or guaranteed loss; lucky positive base is multiplied before the configured
    highest qualifying streak tier is added. The last tier remains in force.

    A missed day resets the streak to 1 rather than to 0: the member did check
    in today, and starting them at zero would mean today's check-in paid no
    streak at all.
    """

    spec = Spec(
        id="checkin",
        name="每日签到",
        description="北京当日有效观影满600秒可签到；基础-10～-1占1%、1～10占60%、11～20占30%、21～30占9%，各区间整数等概率、0不抽取，无保底。连签按阶梯，断签重算。"
        "签到动作由成员触发，这里的「立即运行」只统计不发放。",
        category="points",
        icon="✅",
        interval=0,
        fields=[
            Field(
                "streak_tiers",
                "连签加成（%）",
                kind="text",
                default=encode(
                    [
                        {"days": 1, "percent": 0},
                        {"days": 3, "percent": 5},
                        {"days": 7, "percent": 15},
                        {"days": 30, "percent": 50},
                    ]
                ),
                help="仅最高达标档：基础正积分×百分比，向下取整；负积分不加成，加成不参与幸运翻倍。断签回第1天，基础扣分总概率1%，无保底。",
            ),
            Field("weekends", "周末活动", kind="bool", default=True),
            Field(
                "double_ppm", "正积分幸运概率（ppm）", kind="int", default=100000, min=0, max=PPM
            ),
            Field("multiplier", "幸运倍率", kind="int", default=2, min=1, max=100),
            Field(
                "drops",
                "活动掉落（JSON）",
                kind="text",
                default=encode(
                    [
                        {"ppm": ppm, "spec": spec}
                        for ppm, spec in zip((5000, 3000, 2000), DEFAULT_CARDS[:3])
                    ]
                ),
                help='每次最多1件，概率互斥，总和<=1000000；可用 {"ppm":5000,"item_id":商品ID} 指定道具。幸运与掉落独立。',
            ),
            Field(
                "holidays",
                "节日具体日期（JSON）",
                kind="text",
                default=encode(default_holidays()),
                help="预置2026/2027中国与国外主要节日。农历按年度具体日期；后续年度须维护。节日优先于周末，不叠加。每项可覆盖double_ppm/multiplier/drops。",
            ),
        ],
    )

    # -- the action a member triggers ---------------------------------------

    def checkin(self, user_id: str, now: float | None = None) -> dict[str, Any]:
        """Award today's points, or refuse because today is already paid.

        The refusal is a normal return rather than an exception: "already
        checked in" is the expected answer for anyone who taps twice, and the
        bot has to show it as a message, not an error.
        """
        db = self.ctx.db
        points = getattr(self.ctx, "points", None)
        if db is None or points is None:
            return {"ok": False, "reason": "积分服务不可用"}

        user_id = str(user_id or "")
        if not user_id:
            return {"ok": False, "reason": "账号不存在"}

        with economy_write(db) as conn:
            return self._checkin(conn, points, user_id, now)

    def _checkin(self, conn: Any, points: Any, user_id: str, now: float | None) -> dict[str, Any]:
        db = self.ctx.db
        config = self._config()
        now = time.time() if now is None else now
        day = _today(now)

        existing = db.one("SELECT * FROM checkins WHERE emby_user_id=? AND day=?", (user_id, day))
        if existing:
            return {
                "ok": False,
                "reason": "今天已签到",
                "streak": int(existing.get("streak") or 1),
                "balance": points.balance(user_id),
                "saved_result": json.loads(existing.get("result_json") or "{}"),
            }

        prior = db.one(
            "SELECT streak FROM checkins WHERE emby_user_id=? AND day=?", (user_id, _yesterday(now))
        )
        streak = int((prior or {}).get("streak") or 0) + 1

        day, watched = watch_progress(db, user_id, now)
        if watched < 600:
            return {
                "ok": False,
                "reason": f"今日有效观影 {int(watched)}/600 秒，还需 {max(0, int(600 - watched + 0.999))} 秒",
                "watched_seconds": watched,
                "required_seconds": 600,
                "balance": points.balance(user_id),
            }
        member = self.ctx.members.get(user_id) if self.ctx.members else None
        if not member:
            return {"ok": False, "reason": "账号不存在"}
        validate_checkin(config)
        secret = settings().mediadeck_checkin_secret
        base_roll = draw(secret, user_id, day, "base", CHECKIN_BASE_SIZE)
        base = checkin_base(base_roll)
        tiers = json.loads(config["streak_tiers"])
        percent = max((t for t in tiers if t["days"] <= streak), key=lambda t: t["days"])["percent"]
        bonus = max(base, 0) * percent // 100
        activity = activity_for(config, now)
        lucky_roll = draw(secret, user_id, day, "lucky", PPM)
        multiplier = (
            activity["multiplier"] if base > 0 and lucky_roll < activity["double_ppm"] else 1
        )
        drop_roll = draw(secret, user_id, day, "drop", PPM)
        drop = None
        cumulative = 0
        for candidate in activity["drops"]:
            cumulative += candidate["ppm"]
            if drop_roll < cumulative:
                if "item_id" in candidate:
                    shop = getattr(self.ctx, "shop", None)
                    drop = shop.get(candidate["item_id"]) if shop else None
                    if not drop:
                        raise ValueError("活动掉落商品不存在，请管理员修正配置")
                else:
                    drop = dict(candidate["spec"])
                break
        award = base * multiplier + bonus
        result = {
            "ok": True,
            "points": award,
            "base": base,
            "bonus": bonus,
            "streak_percent": percent,
            "calculation_version": "base-percent-v2",
            "base_rule_version": CHECKIN_BASE_RULE,
            "streak": streak,
            "multiplier": multiplier,
            "watched_seconds": watched,
            "activity": activity["name"],
            "rule_version": RULE_VERSION,
            "rolls": {"base": base_roll, "lucky": lucky_roll, "drop": drop_roll},
            "rule_snapshot": config,
            "drop_spec": drop,
        }
        if drop:
            result["card_id"] = InventoryService.add(conn, user_id, drop, "checkin:" + day, now)
        result["balance"] = points._apply(conn, user_id, award, "checkin", day, "checkin", int(now))
        conn.execute(
            "INSERT INTO checkins(emby_user_id,day,streak,points,created_at,result_json) VALUES(?,?,?,?,?,?)",
            (user_id, day, streak, award, int(now), encode(result)),
        )
        return result

    def normalize_config(self, config):
        return dict(config, streak_tiers=encode(percent_tiers(config['streak_tiers'])))

    def validate_config(self, config):
        validate_checkin(config)
        candidates = json.loads(config["drops"])
        for holiday in json.loads(config["holidays"]):
            candidates += holiday.get("drops", [])
        shop = getattr(self.ctx, "shop", None)
        for candidate in candidates:
            if "item_id" in candidate:
                item = shop.get(candidate["item_id"]) if shop else None
                if not item:
                    raise ValueError("掉落商品不存在")
                validate_card(item)
        return config

    def _config(self) -> dict[str, Any]:
        registry = getattr(self.ctx, "registry", None)
        if registry is None:
            return self.defaults()
        try:
            return registry.config(self.spec.id)
        except Exception:  # noqa: BLE001 - a broken store must not block a check-in
            return self.defaults()

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        db = self.ctx.db
        if db is None:
            return {"ok": False, "error": "数据库不可用"}
        day = _today()
        today = (
            db.one(
                "SELECT COUNT(*) AS n, COALESCE(SUM(points),0) AS pts FROM checkins WHERE day=?",
                (day,),
            )
            or {}
        )
        total = db.one("SELECT COUNT(*) AS n FROM checkins") or {}
        longest = (
            db.one("SELECT COALESCE(MAX(streak),0) AS s FROM checkins WHERE day=?", (day,)) or {}
        )
        return {
            "今日签到人数": int(today.get("n") or 0),
            "今日发出积分": int(today.get("pts") or 0),
            "今日最长连签": int(longest.get("s") or 0),
            "累计签到次数": int(total.get("n") or 0),
        }


class PointsTransferPlugin(Plugin):
    """Member-to-member transfers, with a daily cap and an optional fee.

    The daily cap is the anti-abuse control that matters: without it, a
    compromised account can be emptied in one message, and points funnelled
    through a chain of accounts are untraceable in practice. A cap makes both
    slow enough to notice.

    The fee is destroyed rather than collected. A treasury account would have
    to belong to someone, and "who may spend the fees" is a question with no
    good answer on a server run by one person.
    """

    spec = Spec(
        id="points_transfer",
        name="积分转账",
        description="成员之间互相转赠积分。关闭后机器人不再显示转账按钮。"
        "每日上限用于限制被盗号后的损失，手续费直接销毁。",
        category="points",
        icon="💸",
        interval=0,
        fields=[
            Field(
                "restrict_receivers",
                "禁止失效/封禁账号收款",
                kind="bool",
                default=False,
                help="默认不限制收款；转出仅正常有效账号可用。",
            ),
            Field(
                "enabled_for_members",
                "允许成员转账",
                kind="bool",
                default=True,
                help="关闭后仅保留管理员调整，成员看不到转账按钮",
            ),
            Field(
                "daily_limit",
                "每日转出上限",
                kind="int",
                default=500,
                min=0,
                max=1_000_000,
                help="单个成员每天最多转出多少；0 表示不限",
            ),
            Field("min_amount", "单次最小数量", kind="int", default=1, min=1, max=1_000_000),
            Field(
                "fee_percent",
                "手续费（%）",
                kind="int",
                default=0,
                min=0,
                max=90,
                help="从转出方扣除并销毁，收款方按扣除后到账",
            ),
        ],
    )

    def _config(self) -> dict[str, Any]:
        registry = getattr(self.ctx, "registry", None)
        if registry is None:
            return self.defaults()
        try:
            return registry.config(self.spec.id)
        except Exception:  # noqa: BLE001 - a broken store must not block a transfer
            return self.defaults()

    def fee_for(self, amount: int, config: dict[str, Any] | None = None) -> int:
        config = config or self._config()
        percent = int(config.get("fee_percent", 0) or 0)
        if percent <= 0:
            return 0
        return int(amount) * percent // 100

    def can_transfer(
        self, from_id: str, amount: int, *, now: float | None = None
    ) -> tuple[bool, str]:
        """Everything that can refuse a transfer, checked before any write."""
        points = getattr(self.ctx, "points", None)
        if points is None:
            return False, "积分服务不可用"
        config = self._config()
        if not config.get("enabled_for_members", True):
            return False, "转账功能已关闭"

        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return False, "请输入正整数"
        minimum = int(config.get("min_amount", 1) or 1)
        if amount < minimum:
            return False, f"单次至少转 {minimum} 积分"

        balance = points.balance(from_id)
        if balance < amount:
            return False, f"积分不足，当前余额 {balance}"

        cap = int(config.get("daily_limit", 0) or 0)
        if cap > 0:
            midnight = day_bounds(time.time() if now is None else now)[1]
            sent = points.spent_since(from_id, "transfer.out", int(midnight))
            if sent + amount > cap:
                return False, f"超出每日转出上限（{cap}），今日已转 {sent}"
        return True, ""

    def transfer(
        self,
        from_id: str,
        to_id: str,
        amount: int,
        *,
        request_id: str | None = None,
        expected_fee: int | None = None,
        conn: Any = None,
        actor: str = "member",
    ) -> dict[str, Any]:
        """Regular debit transfer; supplied conn joins the group confirmation transaction."""
        points = getattr(self.ctx, "points", None)
        if points is None:
            raise ValueError("积分服务不可用")
        from_id, to_id = str(from_id), str(to_id)
        if isinstance(amount, (bool, float)):
            raise ValueError("转账数量必须是正整数")  # noqa: TRY004 - public validation contract
        request_id = request_id or uuid.uuid4().hex
        request = {"to_id": to_id, "amount": int(amount), "expected_fee": expected_fee}
        with (economy_write(points._db) if conn is None else contextlib.nullcontext(conn)) as tx:
            prior = receipt(tx, "transfer", request_id, from_id, request)
            if prior is not None:
                return prior
            if from_id == to_id:
                raise ValueError("不能转给自己")
            sender = self.ctx.members.get(from_id)
            target = self.ctx.members.get(to_id)
            if not sender or not target:
                raise ValueError("账号不存在")
            if sender.get("state") != "active" or sender.get("emby_missing_since"):
                raise ValueError("封禁或失效账号不能转出")
            if self._config().get("restrict_receivers") and (
                target.get("state") != "active" or target.get("emby_missing_since")
            ):
                raise ValueError("当前配置禁止封禁或失效账号收款")
            now = time.time()
            ok, reason = self.can_transfer(from_id, amount, now=now)
            if not ok:
                raise ValueError(reason)
            fee = self.fee_for(int(amount))
            if expected_fee is not None and fee != expected_fee:
                raise ValueError("手续费已变化，请重新确认")
            result = points.transfer(
                from_id, to_id, int(amount), actor=actor, fee=fee, conn=tx, now=int(now)
            )
            save_receipt(tx, "transfer", request_id, from_id, request, result)
            return result

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        db = self.ctx.db
        if db is None:
            return {"ok": False, "error": "数据库不可用"}
        midnight = int(day_bounds(time.time())[1])
        today = (
            db.one(
                "SELECT COUNT(*) AS n, COALESCE(SUM(-delta),0) AS total "
                "FROM points_ledger WHERE reason='transfer.out' AND created_at>=?",
                (midnight,),
            )
            or {}
        )
        total = db.one("SELECT COUNT(*) AS n FROM points_ledger WHERE reason='transfer.out'") or {}
        return {
            "状态": "开启" if config.get("enabled_for_members", True) else "关闭",
            "今日转账笔数": int(today.get("n") or 0),
            "今日转出积分": int(today.get("total") or 0),
            "累计转账笔数": int(total.get("n") or 0),
            "每日上限": int(config.get("daily_limit", 0) or 0) or "不限",
        }


class EconomyPlugin(Plugin):
    spec = Spec(
        id="inventory",
        name="背包与称号",
        category="points",
        icon="🎒",
        interval=0,
        description="此卡用于权益配置；开关不影响商品上下架或已有权益，立即运行只显示上限。道具不可赠送，数值相加、每张独立到期，原权益不改。称号使用普通群成员标签，不授予管理权限。",
        fields=[
            Field(
                "bandwidth_cap_mbps",
                "额外道具带宽上限（Mbps）",
                kind="int",
                default=0,
                min=0,
                max=100000,
                help="0=不封顶；正数仅限制未到期道具的总加成，不包含原基础带宽。每张独立到期。1Mbps=1000kbps=0.125MB/s。",
            ),
            Field("streams_cap", "同播叠加后的上限", kind="int", default=10, min=1, max=100),
            Field(
                "title_chat",
                "称号目标群",
                default="",
                help="留空仅在Bot群交互白名单恰好1个群时自动使用；多个群须明确选择。",
            ),
        ],
    )

    async def run(self, config):
        return {"额外道具带宽上限Mbps": config["bandwidth_cap_mbps"] or "不封顶", "同播上限": config["streams_cap"]}


class RedPacketsPlugin(Plugin):
    spec = Spec(
        id='red_packets', name='群积分红包', category='points', icon='🧧', interval=0,
        description='授权群内的拼手气/等额红包，须本人确认。新红包不自动过期，未领积分持续留包；自动尝试置顶原领取消息，领完独立结果卡。关闭仅停止新建和确认；已有红包可继续领取，旧包按原规则收尾。',
        fields=[
            Field('enabled_for_members', '开放普通成员发红包', kind='bool', default=True),
            Field('max_total', '每包总积分上限', kind='int', default=5000, min=1, max=1000000),
            Field('max_parts', '每包最多份数', kind='int', default=50, min=1, max=200),
        ],
    )

    def readonly_status(self):
        db = self.ctx.db
        active = db.one("SELECT COUNT(*) AS n,COALESCE(SUM(remaining),0) AS remaining FROM red_packets WHERE status='active'") or {}
        errors = db.one("SELECT COUNT(*) AS n FROM red_packets WHERE settlement_error<>'' OR publish_error<>'' OR pin_error<>'' OR unpin_error<>'' OR receipt_error<>'' OR (card_send_state='sending' AND card_message_id IS NULL)") or {}
        reward = db.one("SELECT COALESCE(SUM(c.amount),0) AS n FROM red_packet_claims c JOIN red_packets r ON r.nonce=c.nonce WHERE r.funding='reward'") or {}
        return {'进行中红包': active.get('n', 0), '尚未领取积分': active.get('remaining', 0),
                '奖励已入账积分': reward.get('n', 0), '待重试或核实': errors.get('n', 0),
                '结果需核实': db.one("SELECT COUNT(*) n FROM red_packets WHERE receipt_state='unknown'")['n'],
                '置顶未确认': db.one("SELECT COUNT(*) n FROM red_packets WHERE pin_state IN ('failed','unknown')")['n'],
                '新红包': '无截止时间'}

    async def run(self, config):
        return {**self.readonly_status(),'总额上限':config['max_total'],'最多份数':config['max_parts']}


POINTS_PLUGINS = (CheckinPlugin, PointsTransferPlugin, EconomyPlugin, RedPacketsPlugin)
