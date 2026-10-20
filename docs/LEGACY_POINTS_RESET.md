# 旧积分冲销：保留切点后所得

`backend/tools/reset_legacy_points.py` 是独立离线维护工具，不启动应用、不迁移结构、不访问外部服务。只能在经过授权的停服维护窗口、取得一致性备份并核对预览后应用；默认仅只读预览。等级迁移本身仍不改余额，此工具是独立授权的积分操作。

## 规则与拒绝条件

- 所有账本账号均参与，包括白名单、管理员、已删除账号遗留余额以及当前无余额账号；没有身份豁免。
- 截止前净额作为旧资金桶，截止时刻（含）之后按 `created_at,id` 顺序处理。直接所得 `checkin` / `watch.reward` 入新桶；`shop.redeem` / `activity.renew` 消费优先扣旧桶再扣新桶。最终只保留新桶未消费金额，不退消费、不动库存、不增加余额、不造债。
- 今日转账、玩法、冻结、红包、退款、未知reason或不符预期方向全部拒绝，需先报告具体流水与来源歧义，不得随意扩大允许列表。负余额或时间序列资金缺口亦拒绝。
- 非终态游戏/红包以及任何非零 `play_escrows` / `play_funds` 阻止执行；缺少必要表视为不兼容而非零资金。工具不会替业务结算冻结资金。
- 仅追加 `admin.reset_legacy` 负数流水，不删改既有历史。单个 `BEGIN IMMEDIATE` 事务包含全部冲销及 `economy_receipts` 中 `points.legacy_reset` 批次回执。所有用户明细、摘要及预览哈希写入回执。
- 预览哈希覆盖完整账本及参与账号。账本/成员变化导致确认失败，必须重新预览。相同批次和切点重试只返回原回执，不再扣分；不影响之后新发积分。不同批次或不同切点不能绕过既有回执再清一次。

## 操作

以下为占位符，切点、批次和数据库必须由操作者明确指定，源码不内置生产值。

```sh
python tools/reset_legacy_points.py --database "$DB" --cutoff "$CUTOFF_UNIX" \
  --batch "$BATCH" --output /private/reset-preview.json
# 审阅私有逐账号旧/新桶、冲销/保留/当前余额；确认冻结为零、最终快照可靠后：
python tools/reset_legacy_points.py --database "$DB" --cutoff "$CUTOFF_UNIX" \
  --batch "$BATCH" --apply --confirm-hash "$PREVIEW_HASH" \
  --output /private/reset-applied.json
```

明细输出采用排他创建、权限600，不覆盖既有审计；标准输出只含聚合及哈希。应用前应在数据库隔离副本完成演练，验证账本、库存、旧签到和幂等；冲销必须先于新观影奖励启用。无新业务发生时可恢复最终停服备份；服务恢复并产生新流水后，不能直接丢弃新业务回退数据库，应先保全新状态并报告。
