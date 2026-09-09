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

import time
from typing import Any

from app.modules.plugins import Field, Plugin, Spec


def _today(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now or time.time()))


def _yesterday(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime((now or time.time()) - 86400))


class CheckinPlugin(Plugin):
    """Daily check-in. One payout per member per calendar day.

    The streak bonus is capped because an uncapped one is a promise the
    operator has to keep: at +5/day, a member who never misses is earning 1800
    points a year from consistency alone, and the cap is what keeps the top of
    the ledger from running away from everyone else.

    A missed day resets the streak to 1 rather than to 0: the member did check
    in today, and starting them at zero would mean today's check-in paid no
    streak at all.
    """

    spec = Spec(
        id="checkin",
        name="每日签到",
        description="成员每天在机器人里签到领积分。连续签到有额外奖励，断签重新计算。"
                    "签到动作由成员触发，这里的「立即运行」只统计不发放。",
        category="points",
        icon="✅",
        interval=0,
        fields=[
            Field("points_per_day", "每日积分", kind="int", default=10,
                  min=1, max=1000, help="每次签到的基础积分"),
            Field("streak_bonus", "连签奖励", kind="int", default=5,
                  min=0, max=1000,
                  help="连续签到时每多一天额外增加的积分；0 表示关闭"),
            Field("max_streak_bonus", "连签奖励上限", kind="int", default=50,
                  min=0, max=100000, help="连签奖励最多加到多少，防止无限累积"),
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

        with db.write() as conn:
            return self._checkin(conn, points, user_id, now)

    def _checkin(self, conn: Any, points: Any, user_id: str,
                 now: float | None) -> dict[str, Any]:
        db = self.ctx.db
        config = self._config()
        now = time.time() if now is None else now
        day = _today(now)

        existing = db.one(
            "SELECT * FROM checkins WHERE emby_user_id=? AND day=?",
            (user_id, day))
        if existing:
            return {"ok": False, "reason": "今天已签到",
                    "streak": int(existing.get("streak") or 1),
                    "balance": points.balance(user_id)}

        prior = db.one(
            "SELECT streak FROM checkins WHERE emby_user_id=? AND day=?",
            (user_id, _yesterday(now)))
        streak = int((prior or {}).get("streak") or 0) + 1

        base = int(config.get("points_per_day", 10))
        per_day = int(config.get("streak_bonus", 5))
        cap = int(config.get("max_streak_bonus", 50))
        # The first day of a streak earns no bonus: the bonus is for having
        # come back, and day one is not coming back.
        bonus = min(per_day * (streak - 1), cap) if per_day > 0 else 0
        award = base + bonus

        # The row goes in first: its primary key is what makes a double tap
        # impossible, so writing it before paying means the second tap is
        # refused by the database rather than by a check that already passed.
        conn.execute(
            "INSERT INTO checkins(emby_user_id,day,streak,points,created_at) "
            "VALUES(?,?,?,?,?)", (user_id, day, streak, award, int(now)))
        balance = points._apply(conn, user_id, award, 'checkin', day, 'checkin', int(now))
        return {"ok": True, "points": award, "base": base, "bonus": bonus,
                "streak": streak, "balance": balance}

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
        today = db.one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(points),0) AS pts "
            "FROM checkins WHERE day=?", (day,)) or {}
        total = db.one(
            "SELECT COUNT(*) AS n FROM checkins") or {}
        longest = db.one(
            "SELECT COALESCE(MAX(streak),0) AS s FROM checkins WHERE day=?",
            (day,)) or {}
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
            Field("enabled_for_members", "允许成员转账", kind="bool", default=True,
                  help="关闭后仅保留管理员调整，成员看不到转账按钮"),
            Field("daily_limit", "每日转出上限", kind="int", default=500,
                  min=0, max=1_000_000, help="单个成员每天最多转出多少；0 表示不限"),
            Field("min_amount", "单次最小数量", kind="int", default=1,
                  min=1, max=1_000_000),
            Field("fee_percent", "手续费（%）", kind="int", default=0,
                  min=0, max=90, help="从转出方扣除并销毁，收款方按扣除后到账"),
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

    def can_transfer(self, from_id: str, amount: int) -> tuple[bool, str]:
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
            midnight = time.mktime(time.strptime(_today(), "%Y-%m-%d"))
            sent = points.spent_since(from_id, "transfer.out", int(midnight))
            if sent + amount > cap:
                return False, f"超出每日转出上限（{cap}），今日已转 {sent}"
        return True, ""

    def transfer(self, from_id: str, to_id: str, amount: int) -> dict[str, Any]:
        """Check, then move. Returns the ledger result or raises ValueError."""
        points = getattr(self.ctx, "points", None)
        if points is None:
            raise ValueError("积分服务不可用")
        # The cap check and both ledger entries share a transaction/lock.
        with points._db.write() as conn:
            ok, reason = self.can_transfer(from_id, amount)
            if not ok:
                raise ValueError(reason)
            return points.transfer(from_id, to_id, int(amount), actor="member",
                                   fee=self.fee_for(int(amount)), conn=conn)

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        db = self.ctx.db
        if db is None:
            return {"ok": False, "error": "数据库不可用"}
        midnight = int(time.mktime(time.strptime(_today(), "%Y-%m-%d")))
        today = db.one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(-delta),0) AS total "
            "FROM points_ledger WHERE reason='transfer.out' AND created_at>=?",
            (midnight,)) or {}
        total = db.one(
            "SELECT COUNT(*) AS n FROM points_ledger "
            "WHERE reason='transfer.out'") or {}
        return {
            "状态": "开启" if config.get("enabled_for_members", True) else "关闭",
            "今日转账笔数": int(today.get("n") or 0),
            "今日转出积分": int(today.get("total") or 0),
            "累计转账笔数": int(total.get("n") or 0),
            "每日上限": int(config.get("daily_limit", 0) or 0) or "不限",
        }


POINTS_PLUGINS = (CheckinPlugin, PointsTransferPlugin)
