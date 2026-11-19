"""Scheduled watch settlement, disabled until an explicit operator cutover."""
from app.modules.plugins import Field, Plugin, Spec
from app.modules.usage import run_usage_io
from app.modules.watch_rewards import ActivityService


class WatchRewardsPlugin(Plugin):
    spec = Spec(
        id="watch_rewards", name="观影奖励结算", icon="🎬", interval=60,
        description="静默结算核验观影积分与成长；北京时间每日最多48积分/60成长。"
                    "首次启用从运行时刻生效，不追发历史奖励。关闭开关只暂停结算，恢复时补结新增证据。",
        fields=[Field("activate_rewards", "确认启用新观影奖励", kind="bool", default=False,
                      help="默认关闭。任务开关与本开关均启用才发奖；立即运行不会绕过开关。")],
    )

    async def run(self, config):
        registry = self.ctx.registry
        if not registry or not registry.enabled(self.spec.id) or not config["activate_rewards"]:
            return {"ok": True, "状态": "未启用，不发放奖励"}
        service = getattr(self.ctx, "activity", None) or ActivityService(self.ctx.db, self.ctx.points)
        await run_usage_io(service.activate)
        result = await run_usage_io(service.settle)
        promotions = result.pop("promotions", None) or []
        bot = getattr(self.ctx, "telegram", None)
        members = getattr(self.ctx, "members", None)
        if promotions and bot is not None and members is not None and hasattr(bot, "notify_realm_breakthrough"):
            for uid, old_lv, new_lv in promotions:
                member = await run_usage_io(members.get, uid)
                if member:
                    await bot.notify_realm_breakthrough(member, old_lv, new_lv, granted_special=False)
        return result
