# Roadmap

Owner sets priorities when he wants to; otherwise this plan is executed
top-down without asking. Every item ships as its own PR + release tag.

## Confirmed aggregate streaming rate by account
- Standard and whitelist baseline allowances are decimal 15 MB/s and 35 MB/s;
  existing bandwidth cards and individual increases remain additive.
- All parallel signed video Ranges for one account share its final effective
  node-local byte budget; preserve auth, Range, billing and admission paths.
- Publish via isolated PR/CI/tag; update the two live groups through the existing
  update path and switch the private node route only during authorized release.

## Confirmed unified viewing statistics and progress collection
- Restore one cumulative viewing value from existing non-duplicated ledgers; do
  not invent missing history, dated/title allocations, or change money/traffic.
- Personal weekly reports use the previous complete Beijing Monday-to-Monday
  week, with seconds, session counts and detail from one snapshot and window.
- Feed authenticated, upstream-accepted progress into the existing strict proof
  gate; preserve pause/seek/rate/identity checks and report/poll idempotency.
- Native Sessions without PlaySessionId may use only an existing issued and
  observed admission lease, with current user/device/session/item matching and
  transactional replacement protection; never trust an unbound self-report.
- Show sourced recent playback and observation/unknown states; keep verified
  activity and ranking rules unchanged. Ship through normal PR/CI/tag gates.

## Confirmed shared scratch card and group interaction package
- Shared nine cells, owner fee confirmation, independent system rewards, immutable
  configuration and future-only scheduled rooms; probability configuration is Web-only.
- New Niuniu fixed four-stake banker collateral and up to four one-stake guests,
  pairwise basic bull comparison, no voluntary leaving, unused collateral release,
  system cancellation refunds and legacy money snapshots.
- New Niuniu original picture from lobby through result; old text stays text.
  Delete only responded-to current trusted group commands, never private/ordinary/
  reply-target/history messages. Preserve the packet result/unpin exception.
- Isolated handler/money/concurrency/transport/render tests and normal PR/CI/release
  gates precede separately authorized minimal deployment; no live gambling probes.

## Full-source functionality and interaction audit — local corrective cycle
- [x] Cover all 69 production source files plus project configuration with explicit
      ownership and evidence; preserve the approved business rules and Hard Glass UI.
- [x] Reproduce and locally correct account/Bot, playback/metering, Web interaction,
      background-integration, persistence and adapter defects.
- [x] Integrate cross-module fixes and verify: 1562 passed, 56 skipped, 1 strict
      expected failure; all 25 pages exercised in isolated Chromium.
- [x] Replace ambiguous v1 playback signatures with v2 HMAC-SHA256 and an
      independent probe verification gate; original security XFAIL now passes.
      Owner approved rejecting old links without a v1 grace period.
- [x] Final v0.30.0 candidate: 1606 passed, 56 skipped, no XFAIL; real nginx
      file/transparent-proxy/Range/HEAD and verification-failure tests pass.
- Upgrade contract: `SIGNING-V2.md`; nodes and panel must be switched together.
- [x] Owner separately authorized coordinated release/deployment, retaining
      existing proxy entry points and accepting v1-link invalidation.
- [ ] Complete coordinated rollout and verify actual running revisions/traffic.
- Historical audit evidence: `AUDIT-2026-09-09.md`; current protocol: `SIGNING-V2.md`.
- The original audit was local-only. The subsequent v2 rollout has separate,
  bounded authorization; no absolute bug-freedom or unexecuted live check is claimed.

## Hard Glass and reserved whitelist — approved local implementation
- [x] Shared dark hard-glass theme across the existing workspaces and controls.
- [x] Member list interaction rebuild and viewport-fixed details with safe drafts,
      accessible focus, mobile layout and non-destructive live updates.
- [x] Fixed whitelist group identity and silver/lavender Web badge plus Telegram
      decoration; preserve group configuration and keep administrator roles separate.
- [x] Full regressions and actual local browser acceptance including earlier flows.
- [ ] Separately authorized publication and deployment; production is not modified
      by this implementation phase.

## Workspace and watch-time follow-up — local delivery
- [x] Six primary workspaces with retained secondary pages, a single live-playback
      surface and category-based independently saved configuration.
- [x] Member-initiated Telegram handoffs plus new-account rescue verification;
      group-only review and retryable notification delivery; retire Web review.
- [x] Durable sampled watch intervals/checkpoints/totals, clipped rolling windows
      and explicit uncertainty for old sessions that cannot be split precisely.
- [x] Use measured monthly traffic in operational views and remove mixed direct-log
      or bitrate-derived figures from member usage presentation.
- [x] Isolated local API/Chromium acceptance and existing regression coverage.
- [ ] Separately authorized upstream release and deployment. No live state change
      is included in this local implementation phase.

## User-management rebuild — live UI foundation (local development)
- [x] Replace SSE-triggered full-page loaders with cached-model DOM patches.
- [x] Keep editor/selection/scroll state and guard route/reconnect races.
- [x] Provide a live-page registration contract and query-aware navigation.
- [x] Reproduce the original failure and verify in isolated Chromium.
- [x] Integrate member lifecycle, measured-quota modules and real browser/kernel/nginx verification.
- [ ] Operator-approved deployment, measured-baseline cutover and legacy-user migration (separate phase).
- No deployment, node configuration change, or existing-user migration in this step.

## Done
- [x] Backend scaffold, env-only config, mock/live adapter split
- [x] Load-aware 302 stream scheduler (weights, health, kick, history, log)
- [x] Node load probe agent (stdlib single file)
- [x] Pipeline overview (sanitized host collector -> panel)
- [x] Emby user management (create/disable/password/policy)
- [x] Import lane skeleton (job lifecycle + API)
- [x] Web-triggered self-update to release tags
- [x] Admin panel shell (grouped sidebar, stat cards, page registry)
- [x] Media library overview
- [~] Acquisition/downloads: dropped by owner decision (stays external)

## Next — Registered external playback entries (draft PR)
- Register HTTPS entries through the existing integration settings API.
- Authenticate each proxy's entry assertion; keep Emby auth and node signing.
- Return entry-local `/_n/<node>/s/...` URLs and isolate playback caches.
- Export a fixed-upstream Caddy template; document the origin proxy hop and
  local verification separately from operator-side end-to-end acceptance.
- Delivery stops at a tested draft PR; rollout is a separate operator action.

## v0.6.0 — Mount health (shipped)
Storage is the single most failure-prone layer in this stack: FUSE mounts go
stale, a union mount loses allow_other and the whole library 403s, ffprobe
wedges in D-state. This page makes all of it visible in one place.
- [x] Collector: per-mount liveness (readdir probe), backend type, options
- [x] Detect stuck I/O (processes blocked in uninterruptible sleep per mount)
- [x] VFS cache usage vs configured limits; free-space floor per filesystem
- [x] Panel page: mount table, health tags, stuck-process alerts

## v0.7.0 — Scheduled task center (shipped)
- [x] Snapshot schema: per-job schedule, last run, status, duration, exit code,
      failure streak
- [x] Panel page: stat cards, task table, alert list
- [ ] Host collector wiring (in progress)
- [ ] Manual trigger (allowlisted) — deferred

## v0.8.0 — Invites & access (shipped in v0.10 panel)
- [x] Invite code issuing with quota/expiry, redemption -> Emby user creation
- [x] Access control view: per-user device/stream limits at a glance
- [x] Public redeem page at `/invite/{code}` with rate limiting

## v0.9.0 — Playback reports & notifications
- [x] Playback history aggregation (top titles, per-user minutes, node split)
- [ ] Notification center with routing rules and delivery log

## v0.10.0 — 运营中枢 (working tree, uncommitted)
- [x] Membership / plans / invites / stats / storage / audit panel pages
- [x] Node enroll: name-only create, one-line install, call-home report, rotate token
- [x] Global storage remotes + mounts; nodes pick mounts instead of pasting rclone.conf
- [x] Referential integrity (plan/remote in use -> HTTP 409)
- [x] device registration is observational (no registration cap)
- [x] shared session admission across PlaybackInfo, direct dispatch and HLS; durable leases, advisory Stop only
- [x] generic nginx integration and local gateway compatibility validation (operator deployment separate)

## v1.0.0 — Live import executor + UI pass
- [ ] Bridge import jobs to real host-side workers (sanitized IPC)
- [ ] Visual design pass across all pages

---

## 观影活跃保号与等级体系实施计划

日期：2026-10-20（北京时间）
范围：源码开发、隔离测试、迁移预览工具与上线方案。当前不改线上数据库、配置，不重启服务，不推送 GitHub、不发布，也不执行真实删号；这些在开发验收后另行授权。
开发基线：d265011；独立功能分支进行本地开发，部署目录保持不动。

## 1. 产品目标与首版规则

正常用户每周看一集20～40分钟电视剧即可靠观影奖励稳定保号，不依赖积分游戏。积分余额与成长值分离；成长决定等级，正常消费不掉级。积分不足或缺乏有效观看而未续费的账号到期停止播放，保留72小时后删除。

### 静默观影结算

- 只使用现有可核验观影证据，不使用旧版展示总时长、导入估值或未核实的客户端上报直接发积分。
- 北京时间每日累计，前20分钟每满5分钟：8积分、5成长值；21～60分钟每满5分钟：2积分、5成长值。
- 每日上限48积分、60成长值。跨午夜拆分归属；同一天碎片可累计，多设备重叠不重复，暂停/快进不当成额外时长。
- 建立单用户单日结算水位与账本，重复通知、断线恢复、进程重启均不重复发放。只发规则生效后的新增观影奖励，历史迁移另列。
- 静默任务自动结算，不要求打开资料卡或执行签到才入账。结算与积分、成长账本原子提交。

### 等级默认值

| 等级 | 名称 | 默认称号 | 成长门槛 | 30天积分价 | 月流量TiB | Mbps | 同播 | 月求片 |
|---|---|---|---:|---:|---:|---:|---:|---:|
| 1 | 布衣 | 偷得浮生半日闲 | 0 | 80 | 1 | 120 | 2 | 3 |
| 2 | 游侠 | 一身侠气走江湖 | 120 | 75 | 1 | 120 | 2 | 3 |
| 3 | 逍遥 | 醉卧美人膝 | 360 | 70 | 1.2 | 140 | 2 | 4 |
| 4 | 剑仙 | 一剑霜寒十四州 | 900 | 65 | 1.5 | 160 | 2 | 5 |
| 5 | 真君 | 笑问青天几时老 | 1800 | 60 | 2 | 180 | 3 | 6 |
| 6 | 道尊 | 坐看万古浮沉 | 3600 | 55 | 2.5 | 220 | 3 | 8 |
| 7 | 帝君 | 一念山河定 | 7200 | 50 | 3 | 280 | 4 | 10 |

- 用户可见的用户组管理改为等级与权益；主界面聚焦等级及白名单/特殊权益，旧组编辑和新建收进默认折叠的兼容维护区，明确不能代替普通等级配置。后端旧组关联保留，不能仅改标签。
- 各级采用不同图标、两字身份与古风后缀；个人佩戴称号优先。生命周期提醒与Web期限显式使用北京时间（Asia/Shanghai），不依赖宿主或浏览器本地时区。
- 图标、名称、称号、门槛、价格、额度可由后台配置，校验门槛单调、价格和额度合法。
- 个人覆盖、有效道具和特殊资格不被升级清空；设备登记继续不限量，下载与转码权限不凭空扩大。
- 资料卡展示等级短名＋默认或已佩戴个性称号；保留现有 TG 群标签人工编辑保护。

### 续费与保号

- 续费资格：近30天有效观看至少80分钟，且至少3个北京时间观影日，每日不少于10分钟。
- 只允许30天一档；距到期不超过72小时才可续费，不囤多月时长。
- 未到期从原到期时间追加30天；已到期从购买成功时追加30天。不得通过续费重置流量、求片或解除人工禁用。
- 自动保号由用户主动开启，默认关闭；到期前24小时检查，条件达标后原子扣分和续期；重试不可重复收费。
- 提醒包含观影时长、达标日数、余额缺口、到期与删除时间。实际数据或远端状态未知时不能猜测成功。
- 首次有效开通30天启动期；已存在明确个人更长期限不缩短。老不活跃普通用户一次免费30天首期，取消此前无过渡期方案；人工禁用不自动解除。
- 第一期不增加新的积分游戏、不开发成就系统。积分预留保护为后续可选项，首期资料卡清楚展示续费预算，不擅自改变现有游戏可用余额。

### 普通签到与运气签到

- 普通签到 `/checkin` / “签到”：最终固定10积分。
- 运气签到 `/luckcheckin` / “运气签到”：1～10占65%、11～20占25%、21～30占10%，区间内整数等概率，含首尾；无偏1000槽各整数分别65/25/10槽，期望10分。覆盖普通签到结果，不是叠加，也不保留普通10分底线。
- 两者均需当日有效观影满10分钟；成长值不因签到增加。
- 未签到→普通签到→运气签到，或未签到→运气签到；运气签到为当日终态，不能恢复普通或重抽。
- 覆盖只对同日已发签到净额做差额调整；例如10→3扣回7，10→26补16。最终签到奖励始终正数，退款/调整账本可为负。
- 普通签到提示明确写出“结果可能更低，覆盖后不可恢复”。奖励抽取持久化，重复请求/并发/响应丢失不能重新抽签。
- 防止普通奖励花光后免费逃避差额：抽取前需有可用余额覆盖最大可能回收额；不足则在抽取前拒绝，告知缺口，不抽后反悔，不制造新负债。与已有玩法资金冻结保持一致。
- 普通固定10、运气1～30为最终积分，取消连签、节日、幸运倍率对积分的叠加；保留连签展示与单次活动道具，双命令不得重复计连签或掉落。

### 到期与自动删除

- 到期停止播放；保留72小时期间仅允许查看账号和满足条件后的续费，不赠送播放时间。
- 删除前事务内重新检查有效到期时间、续费/删除互斥状态、豁免、临时卡和远端结果，不能使用旧快照删掉刚续费用户。
- 复用现有删除流程，外部 Emby 删除失败或结果不明需可重试；不能先清账本再发现远端删不掉。
- 需处理未结算游戏资金/背包/绑定等关联关系，业务账本和必要审计不能被无差别级联擦除。
- 新清理替代普通等级用户旧“不活跃即停用”规则，避免仍有期限的低频正常用户被旧逻辑提前停用。
- 正式启用前先给出删除候选预览，真实删除始终关闭，直到用户明确批准上线启用。

## 2. 实施顺序

### 第一段：观影奖励与成长账本
新增规则、持久化表/迁移、静默任务、等级计算和单元测试；接入现有核验观影数据，不改动播放证据采集规则。

### 第二段：等级权益与续费
接入 MemberService 有效权益计算、时间计费、续费事务与自动保号；保持播放限速/并发/个人覆盖/道具原有语义；配置版本与请求幂等。

### 第三段：双签到、用户Bot与管理员Web
用户只使用Bot，不提供成员网页、登录码或成员Web API。替换签到状态机与差额账本；私聊资料卡通过中文按钮进入等级权益、续费预览/本人确认、自动保号，展示成长、奖励进度、30天资格与期限。主卡保持简洁，详细规则放入等级权益页。管理员Web保留等级配置、迁移及生命周期管理。

### 第四段：迁移、生命周期和验收
完成老账号预览/确认迁移、旧插件兼容、72小时到期清理（默认不开启）、提醒与回滚材料，执行整体回归与 mock/browser 验收。

## 3. 验收标准

1. 4次20分钟观影合计128积分、80成长值；4次普通签到后168积分；即使4次运气签到均为1，也有132积分，足够Lv1的80积分续费。
2. 同一天60/120分钟均仅48积分、60成长值；重复任务、并发和重启不加倍；跨午夜正确拆分。
3. 1～30全部可能被抽取，低/中/高区概率65%/25%/10%，区间内整数等概率，无取模偏差；重复请求结果一致；普通→运气按净差入账。
4. 积分够但有效观看不达标不续费；观看达标余额不足不扣分；成功后只延长一次，不清使用量或解除处罚。
5. 领取原签到后花费/转账/游戏冻结不能导致覆盖差额绕过或新负债；页面能解释拒绝原因。
6. 升级后只改变等级基础权益，个人覆盖、提速/同播卡和特殊资格保留；到期道具正确回落。
7. 过期71小时59分不删；72小时到达且仍符合条件才进入删除流程；临界续费与删除并发不误删；外部失败可恢复。
8. 老迁移重复运行不重复赠送天数、成长或积分；没有历史奖励追溯增发；已存在有限期限不缩短。
9. 管理员Web与用户Bot显示一致，Bot资料卡与中文按钮完整可用；到期账号仍能查看/达标续费。扣分先预览后本人确认，重试幂等、他人回调拒绝；不存在成员Web页面、路由和登录码。只读操作不发奖或删除。
10. 相关回归、全套测试与静态检查，隔离 mock 启动及浏览器关键流程验证。不以本地测试冒称已上线。

## 4. 已确认产品边界与最终迁移口径

- 已确认：已有永久白名单/永久卡保留，继续出售白名单，维持原豁免；资料卡等级后附白名单字样。
- 已确认（最新修订）：迁移窗口固定为北京时间2026-10-01 00:00至迁移时；核验观影严格大于100分钟且有效观看日严格大于3（至少4个日各满10分钟）的普通老用户授予专属Lv8「道祖 / 一炁化三清」，其余Lv1/成长0。核验起点晚于窗口时：本次迁移专用采用起点之前有日期的旧watch_samples、起点之后严格watch_verified_samples，合并重叠并按北京时间拆日；优先可靠watch_verified_since，否则显示实际首次核验证据起点，未知则拒绝确认。旧采样仅补全资历，不发积分/成长、不进入日常续费资格；展示各来源时长及缺失，不补造历史。
- Lv8基础额度沿Lv7，后续30天续费1积分；日常续费仍需近30天80分钟、3个合格观看日。Lv8仅本次迁移或管理员明确授予，不自然升级或因成长降回Lv7。
- 所有普通老用户首期统一迁移时起30天，不扣积分、不受余额影响。明确个人特殊更长期限在预览列异常，不暗缩短；人工禁用保留。白名单仍豁免。该规则替代14天门槛及Lv8首期扣1分方案。
- 不按历史签到补成长、不追发观影积分。人群统计由独立只读工作负责，代码仅包含泛化规则和mock预览/确认。
- 已确认：普通10/运气1～30为最终积分，取消旧积分加成；运气采用65%/25%/10%分区概率。

## 5. 交付与发布边界

交付包含代码、测试、迁移预览、变更清单与回滚说明。GitHub 推送/PR、发布标签、线上数据库迁移、服务重启和开启自动删除均待后续具体授权。上线遵守仓库→PR→CI→Release/tag→部署，不在部署目录做热补丁，不重启 Emby。
