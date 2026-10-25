# 游戏中心 Mini App API

版本：v1，2026-10-25。权威规则见 `GAME-CENTER.md`。根路径 `/api/app/games`。
炸金花 `zjh`、德州扑克 `holdem`、五子棋 `gomoku` 和象棋 `xiangqi` 均已接入，完整安装时 `available:true`。后台总开关及各游戏开关仍默认关闭，关闭时开桌/入桌返回 `503 disabled`。牛牛 `niuniu`、斗地主 `doudizhu` 为 coming_soon。棋类完整合同见“棋类（象棋、五子棋）”节。

## 1. 通用约定

- 除 SSE 连接外，所有接口携带 `X-Telegram-Init-Data: <Telegram.WebApp.initData>`。复用会员面板校验，必须绑定会员；绝不接受 body 指定操作者。
- JSON，整数积分/欢乐豆，时间为 **Unix 秒（可有小数）**，空值 `null`。客户端倒计时依据 `server_time` 校正，不能自行开局/结算。
- 所有 POST 必须有 `request_id`（客户端生成 UUID，8–80 个 ASCII 字母数字、`_`、`-`）。同用户同 request_id 同请求重复提交只执行一次，返回当下视图；不同请求复用该键返回 409。网络重试使用原键；下一次有意操作换新键。
- 动作还须带 `hand_no` 和 `turn`。窗口竞争时先提交者成功，后者 `409 stale_turn`，应重新获取快照。看牌不推进 turn，但推进 revision；已看牌重复意图不会再次收费。
- `table_id`、`player` 为不透明字符串。座位 `seat` 从 0 起（炸金花 0..4；德州 0..5）。显示名不等于内部会员 ID；不给前端 Emby 用户 ID、Telegram ID、别人的可用余额。
- 桌参数创建时冻结，不受随后后台修改影响。总开关关闭后不允许新开/新入桌，但已在桌会员仍能完成对局、离桌并收回资金。
- POST 成功除开桌/快速开始外统一 `{ "table": <Snapshot> }`。离桌返回已脱敏的桌快照（本人 seat 变 null；延后离桌时 leave_after=true）。

### 深链

游戏页 `startapp=games`；指定桌 `startapp=gc_<table_id>`，参数只含 ASCII 字母数字和下划线。Mini App 从 `Telegram.WebApp.initDataUnsafe.start_param`（或页面 `tgWebAppStartParam`）读取，用 `GET /tables/{table_id}` 预览，再显式 `POST .../seat` 入座；**打开链接不扣分、不自动入座**。若设置的 miniapp_url 是普通 HTTPS 页面，页面链接追加 `tgWebAppStartParam`；Telegram Main Mini App 链接追加 `startapp`。响应的 `deep_link` 是可直接使用的完整链接，不自行拼 Bot 名。

## 2. 大厅、场次、余额

### GET `/lobby`

请求：无 body。响应示例（games/tiers/tables 数组省略同类项）：

```json
{
  "enabled": true,
  "server_time": 1792939300,
  "games": [
    {"id":"zjh","name":"炸金花","enabled":true,"available":true,"coming_soon":false,"min_players":2,"max_players":5,"seated":3},
    {"id":"holdem","name":"德州扑克","enabled":false,"available":true,"coming_soon":false,"min_players":2,"max_players":6,"seated":0},
    {"id":"niuniu","name":"牛牛","enabled":false,"available":false,"coming_soon":true,"min_players":0,"max_players":0,"seated":0}
  ],
  "wallet":{"points":3200,"frozen":50,"beans":10000,"beans_day":"2026-10-25","daily_beans":10000},
  "active_tables":[{"table_id":"abc123","game":"zjh","tier":"novice","currency":"points"}]
}
```

`seated` 为真人在桌人数（不含机器人）；`enabled` 为后台总开关与单游戏开关的组合，`available` 表示本版本引擎已接入。总开关默认关闭。

### GET `/{game}/tiers`

请求例：`/zjh/tiers`。响应：

```json
{"game":"zjh","tiers":[
  {"id":"novice","name":"新手场","currency":"points","ante":1,"cap":50,"min_balance":50,"seated":2,"waiting_tables":[
    {"table_id":"abc123","game":"zjh","tier":"novice","host_name":"小明","players":2,"max_players":5,"phase":"waiting","deep_link":"https://t.me/example_bot?startapp=gc_abc123"}
  ]}
]}
```

炸金花六场 `practice/novice/normal/pro/master/supreme`；`cap:null` 表示至尊不封顶，`min_balance` 是准备所需可用余额，至尊默认 2500。入座不冻结。

德州同六场，场次字段替换为：

```json
{"id":"novice","name":"新手场","currency":"points","small_blind":1,"big_blind":2,"buy_min":40,"buy_max":200,"min_balance":40,"seated":0,"waiting_tables":[]}
```

`buy_max:null` 不限；默认积分场带入20–100个大盲（至尊至少20个大盲、无上限），后台可调整各场参数。练习场固定带入 2000 欢乐豆。`waiting_tables` 仅公开积分等待桌，不列出他人的私人练习桌。

### GET `/me`

响应：`{"active_tables":[{"table_id":"abc123","game":"zjh","tier":"novice","currency":"points"}],"wallet":<Wallet>,"server_time":1792939300}`。用于恢复原桌；同用户最多一张积分桌，另可一张个人练习桌。

### GET `/wallet`

响应：`{"points":3200,"frozen":50,"beans":10000,"beans_day":"2026-10-25","daily_beans":10000}`。
`points` 是可用积分；`frozen` 仅游戏中心冻结额（不含其他群游戏）。德州手中冻结额保留开手数值，直到本手结算再按净输赢转移；不要用 frozen 当作当前可下注筹码，应看座位 stack。每日北京时间首次查询/使用练习余额时补足到 daily_beans，不降低超额余额，不可兑换积分。

## 3. 开桌和桌操作

### POST `/tables`

```json
{"game":"zjh","tier":"novice","request_id":"create_12345678"}
```

德州：`{"game":"holdem","tier":"novice","buy_in":100,"request_id":"create_12345678"}`。
响应 `{"table":<Snapshot>,"created":true}`。创建者自动入座成为房主；炸金花需自行准备/点开始。练习桌自动补机器人（明示 bot=true），德州补到6人，因此满足满桌自动首手条件，开桌响应即可能为 playing。德州积分桌必须给 buy_in；练习可省略，默认2000。开桌群播报后台异步，不阻塞开桌。

### POST `/quick-start`

请求与开桌一致。优先入座同场次有空位的等待桌；德州还会匹配有空位的 between/playing 桌（下一手参与），找不到则新建；响应 `{"table":<Snapshot>,"created":false}`（新建为 true）。已在同一场次原桌则回原桌，不重复冻结；已有其他积分桌返回 `409 already_seated`。

### GET `/tables/{table_id}`

响应 `{"table":<Snapshot>}`。可看公开积分桌，包括深链进桌；他人的练习桌返回 404。旁观视图没有私牌、比牌详情、私有提示。

### POST `/tables/{table_id}/seat`

炸金花：`{"seat":2,"request_id":"seat_12345678"}`；不指定 seat 则自动选空位。
德州：`{"seat":2,"buy_in":100,"request_id":"seat_12345678"}`。
成功返回统一桌响应。进行中入座从下一手参与，不打断当前行动截止时间；炸金花新入座取消全员准备倒计时，德州不重置已有4秒连续发牌倒计时。德州带入冻结在同一个经济写事务内。同用户已在此桌则返回原座位，不再次扣带入；离开后再入座会重新冻结选择的 buy_in（内部复用 escrow，不复用旧 player 身份）。

### POST `/tables/{table_id}/leave`

`{"request_id":"leave_12345678"}`。等待中立即离座；炸金花活跃手牌视为弃牌并立即结算该手应得部分后离座（若仍有其他玩家未结束，本人标记 leave_after=true，结算后移除）；德州本手参与者（包括已弃牌/全下者）标记 leave_after=true，延后到手结束结算离桌；未全下者轮到时约1.5秒自动弃牌，不再额外投入，已全下者仍保留分池资格。期间不能补码/手动动作/取消托管。私有 notice 区分待自动弃牌、已经弃牌和已全下仍参与本手结算，不将全下误提示为弃牌。非参与/已结算座位立即离开。房主移交最早入座的其他会员；无真人时散桌。返回统一桌响应。

等待离线离座无需新增接口：仅 `phase=waiting` 时，每个在座真人连续 180 秒无本桌 SSE 连接、
也无本桌会员 HTTP 请求，就由服务端 tick 按上述离桌路径处理。覆盖炸金花等待开局、德州首手前及
人数不足暂停；`playing/between` 不适用。退回剩余筹码/冻结额、房主继任和开局倒计时与主动离桌一致，
没有真人则散桌（包括练习主人离线），不发 Telegram 消息。

- 本人的本桌快照、操作（含重试）及 stream-token 请求会刷新在线时间；其他人旁观、本人访问大厅、
  钱包或另一桌不会替本桌座位续期。只记录在座者，不保存旁观者的长期在线记录。
- 在线时间与 SSE 连接计数仅在内存；启动时旧座位统一获得完整 180 秒宽限，不从旧 `last_activity`
  推算离线。其他持久化截止时间不重置。
- 移除后通过已有响应体现：`seats` 不再含该座位、`me.player/seat=null`，`/me.active_tables` 不再含该桌；
  真正离座时桌 revision 更新并正常通知 SSE。仅在线续期不更新 revision，也不新增在线时间字段。

### POST `/tables/{table_id}/ready`

`{"ready":true,"request_id":"ready_12345678"}`；取消 `ready:false`。
炸金花及棋类等待阶段（棋类细节见专节）；炸金花余额不足封顶不允许准备。全员准备且至少两人自动 5 秒倒计时，有人入座/取消则取消。每手结束需要重新准备（托管玩家自动准备，但达到连续托管手数上限自动离座）。

### POST `/tables/{table_id}/start`

`{"request_id":"start_12345678"}`。仅房主，且等待阶段。
炸金花房主自动算准备，已准备且余额足够者至少两人才能开，其他人在本手旁观。德州此接口仅用于首手，至少两名有筹码非暂离玩家；满六人自动首手，后续约 4 秒连续发牌，无需也不能再次 start。可参与者不足两人时 waiting 暂停，补码/恢复/新入座使人数足够后自动进入4秒倒计时。筹码为0者不发牌、不扣盲，提示补码或离桌。

### POST `/tables/{table_id}/auto`

`{"enabled":true,"request_id":"auto_12345678"}`。仅炸金花/德州，随时切换托管；false 取消。开启后到本人时 1–2 秒自动行动，普通行动超时自动开启。取消后不会重置原始 30 秒截止时间。

炸金花托管说明：第一次先看牌；对子或更好就跟注，单张弃牌，不加注不主动比牌；跟不起时对子或更好孤注一掷，否则弃牌。最多连续 3 手自动准备（后台可调），达到上限离座。
德州托管说明：免费就过牌；成牌一对或更好（翻前口袋对子或两张都是 A/K/Q），且跟注不超过底池一半才跟；跟注即全下需两对或更好；否则弃牌，不主动下注/加注/全下。

### POST `/tables/{table_id}/away`（德州）

`{"enabled":true,"request_id":"away_12345678"}`。暂离从下一手开始生效，不扣盲；本手行动/计时仍继续。false 恢复下一手，并重新起算连续托管手数/时长（auto 开关不变，手动接管仍用 `/auto enabled:false`）。重复相同 enabled 不重置计时。连续托管 10 手或 10 分钟自动暂离；暂离生效后 10 分钟自动离桌退回筹码；阈值使用桌创建时后台参数。返回统一桌响应。练习场真人暂离或筹码为0时整桌暂停，不跑纯机器人对局。

### POST `/tables/{table_id}/top-up`（德州）

`{"amount":100,"request_id":"topup_12345678"}`。amount 为增加的筹码，正整数，只能 waiting/between（没有进行中手牌）时补码；补后不超过最高带入且可用余额足够。至尊无最高限制，练习补后最多2000。不重置已有发牌倒计时；同 request_id 重试不会再次扣款。返回统一桌响应。会员积分流水只记带入、补码、离桌结算，不按每手记输赢。

## 4. 游戏动作

### POST `/tables/{table_id}/actions`

```json
{"hand_no":1,"turn":3,"action":"compare","target":"p_efgh","request_id":"action_12345678"}
```

返回 `{"table":<Snapshot>}`。服务端校验轮次、当前操作者和合法动作，客户端必须按快照 `legal_actions` 展示按钮而非自行计算扣款。

炸金花动作：除 `look` 外均只有当前行动者可执行。**本手在局且未弃牌的玩家随时可看自己的牌**；`legal_actions` 对非当前行动者也可返回 `{"look":{}}`，看过后不再列出 look（重复提交看牌仍成功且不收费）。

| action | 参数 | 含义 |
|---|---|---|
| look | 无 | 在局未弃牌者随时可看自己的三张牌；重复看牌幂等，不推进 turn、不更换当前行动者、不改变任何行动截止时间，只推进 revision |
| fold | 无 | 弃牌 |
| call | 无 | 当前底注档；看牌后双倍 |
| raise | 无 | 将当前档提高一档并跟注，最高底注×5 |
| compare | target=另一在局 player | 第 2 轮起且已看牌，付跟注两倍；平手发起方输；详情只给双方 |
| all_in | 无 | 仅剩余额度小于应跟额时孤注一掷，投入全部剩余额度并统一摊牌 |

10 轮后统一摊牌。孤注一掷及 10 轮摊牌在局赢家平分整个底池，非德州边池机制。离座的弃牌不要求轮到本人，由 `/leave` 表达。235 开关仅指杂色235；两人比牌只克豹子，多人统一摊牌如同时存在豹子则杂色235按特殊最高牌参与（无豹子时仍为单张），消除三种牌型循环克制的歧义。

德州动作示例：

```json
{"hand_no":4,"turn":12,"action":"raise","amount":120,"request_id":"raise_12345678"}
```

`fold/check/call/bet/raise/all_in`；`bet/raise` 的 amount 是**本轮下注加到多少**（不是增量，不是整手投入），其他动作不传 amount。合法区间、重开加注权依据引擎 `legal_actions()`；不能把不足完整加注的全下当作重开加注。

## 5. Snapshot（HTTP 与 SSE 完全一致）

```json
{
  "id":"abc123","game":"zjh","tier":"novice","tier_name":"新手场","currency":"points",
  "phase":"playing","revision":8,"hand_no":1,"host":"p_abcd","max_players":5,
  "server_time":1792939300,"created_at":1792939200,"last_activity":1792939290,
  "countdown_at":null,"turn_deadline":1792939320,"auto_at":null,"idle_deadline":null,
  "deep_link":"https://t.me/example_bot?startapp=gc_abc123",
  "rules":{"ante":1,"cap":50,"min_balance":50,"step_seconds":30,"allow_235":false,"auto_hands":3},
  "seats":[
    {"seat":0,"player":"p_abcd","name":"小明","avatar":"/api/app/avatar/9000?s=<signature>","bot":false,"ready":false,"auto":false,"auto_hands":0,"away":false,"leave_after":false},
    {"seat":2,"player":"p_efgh","name":"小雨","avatar":"/api/app/avatar/9001?s=<signature>","bot":false,"ready":false,"auto":true,"auto_hands":1,"away":false,"leave_after":false}
  ],
  "me":{"player":"p_abcd","seat":0,"is_host":true,"notice":"","wallet":{"points":3200,"frozen":50,"beans":10000,"beans_day":"2026-10-25","daily_beans":10000}},
  "hand":{
    "number":1,"turn":3,"round":2,"current_player":"p_abcd","bet":1,"pot":4,
    "commitment":"<64 lowercase hex>","finished":false,"reason":null,
    "players":[
      {"player":"p_abcd","seat":0,"seen":true,"folded":false,"invested":2,"remaining":48,"cards":[12,25,7],"hand_name":"对子"},
      {"player":"p_efgh","seat":2,"seen":false,"folded":false,"invested":2,"remaining":48,"cards":null,"hand_name":null}
    ],
    "legal_actions":{"fold":{},"call":{"amount":2},"raise":{"amount":4},"compare":{"amount":4,"targets":["p_efgh"]}},
    "comparisons":[],"result":null
  },
  "last_hand":null
}
```

- `phase`：`waiting/playing/closed`，德州另有 `between`（两手间4秒）。`seats` 只包含已坐位置，不是定长数组。未入座 `me.player/seat=null`，仍可看公开桌；`me.wallet` 永远只有本人余额。
- 两款游戏的公开 `seats[].avatar` 为追加字符串字段：真人返回 `miniapp.avatar_url(绑定 Telegram ID)`
  生成的 `/api/app/avatar/{tg}?s=...`；机器人、无绑定或会员记录已不存在时为 `''`。前端可直接用于图片，
  为空或加载失败时显示名字首字。只追加头像 URL，不额外公开会员 ID 或独立 Telegram ID 字段。
  快照通过一次批量读取绑定后签名，不把签名 URL 写进 `state_json`；旧座位直接兼容，重启后的新签名也可用。
- `revision` 单调递增，任何桌变化都会更新；按 revision 去重 SSE。`hand_no` 桌内递增，动作 turn 在每手从 1 起递增。
- `countdown_at` 炸金花全员准备开局时刻/德州下一手开局时刻；`turn_deadline` 原始行动截止；`auto_at` 托管行动时刻；`idle_deadline` 等待散桌时刻。无计时器为 null。房主等待 3 分钟无操作自动转给最早入座的其他玩家。
- `hand` 当前进行中手牌；等待时 null，上一手放 `last_hand`，结构相同且 `finished:true`。结算的 result 为 `{"winners":["p_abcd"],"awards":{"p_abcd":4,"p_efgh":0},"net":{"p_abcd":2,"p_efgh":-2}}`，所有净额之和为 0。
- 自己未看牌时也不下发 cards；其他人的牌仅正式统一摊牌的在局玩家公开。弃牌和比牌输掉者不在快照公开其牌。
- 比牌双方的 `comparisons` 包含 `{"turn":3,"actor":"p_abcd","target":"p_efgh","loser":"p_efgh","cards":{"p_abcd":[12,25,7],"p_efgh":[0,16,32]}}`，第三人该数组无此项；不得把私人动作响应广播给整桌。
- `notice` 仅本人的超时/余额不足等提示；超时文字为“已超时，进入托管；点「取消托管」接管”。

### 开局状态（追加字段，炸金花与德州共用）

HTTP 与 SSE 的 Snapshot 新增 `start_status`；旧字段保持不变。例如，房主视角下两人入座、
但另一人未准备：

```json
{"start_status":{"min_players":2,"seated_players":2,"eligible_players":1,
 "can_start":false,"reason":"waiting_ready","message":"请等待其他玩家准备；房主点开始时自动算准备"}}
```

- `seated_players`：未申请离桌的在座人数，包含练习机器人；不能用它计算空座，空座仍按 `seats`。
- `eligible_players`：等待/两手间阶段按服务端实际条件计算的可参与人数。炸金花包含房主自动准备，
  并检查会员资格及本场所需可用余额；德州按有筹码且未暂离判断。进行中/散桌时为 0。
  不返回他人的余额或不合格者身份。开局事务仍会重新检查，快照不是资金预留。
- `can_start`：**当前查看者**此刻能否调用 `/start`，只有满足开局条件的房主为 true。
  德州已开过首手后恒为 false，不应再次显示手动开始按钮。
- `reason`：`waiting_players` 人数不足；`waiting_ready` 等待准备；`ineligible_players` 实际合格人数不足；
  `waiting_host` 人数已够、等待房主；`ready` 本人可以开始；`playing` 手中；`closed` 已散桌；
  `automatic` 德州后续自动发牌/人数不足暂停（具体说明见 `message`）。
- `message`：可直接展示的中文说明。前端应优先使用 `can_start/message`，不要凭他人的 `ready`
  标记推测其当前余额。`countdown_at`、`idle_deadline` 继续提供自动开局和散桌时间。
- 余额可能因桌外消费发生变化而不改变桌 `revision`。开始失败或主动刷新时应接受最新 HTTP
  快照，即使 revision 相同；SSE 仍按桌 revision 推送，不额外广播钱包变化。
- 炸金花准备不足额时，`insufficient_funds` 的消息现在说明具体预算/币种；至尊说明入场最低积分及
  “每手冻结全部可用积分”，不再误称有封顶。成功重新准备、恢复资金后开局会清除过期余额提示。

### 德州 Snapshot 与 hand 结构

通用桌壳不变，另有 `button`（尚未开手为 null，否则上次/当前按钮座位号）。多人转为单挑时，先按上一手的大盲位置顺时针确定新的大盲，另一人是按钮/小盲，避免同一人连续两手大盲。

德州 Snapshot 和场次 `waiting_tables` 摘要均增加 `start_status`，原有字段不变：

```json
{"eligible_players":2,"min_players":2,"can_start":true,"reason":"waiting_for_host"}
```

- `eligible_players` 使用服务端参与条件计算（有筹码、非暂离、非延后离桌；练习场无可参与真人则为 0）。仅在 waiting/between 阶段用于开局提示；playing 时筹码尚未结算，不能据此预测下一手人数。
- `can_start` 表示本桌满足首手手动开局条件，不代表查看者权限；按钮还须结合 `me.is_host`。后续手数即使人数足够也为 false，不能再次 POST start。
- `reason`：`waiting_for_players` 首手等人；`waiting_for_host` 人数足够、等待房主开始；`paused_for_players` 连续发牌因人数不足暂停；`practice_player_unavailable` 练习桌真人暂离或无筹码；`next_hand` 等待下一手（倒计时仍取 `countdown_at`）；`closed` 已散桌；进行中为 null。
- 此字段由现存桌状态计算，不写入 `state_json`，旧牌桌无需迁移。不要用总座位数代替可参与人数，也不要把暂停后的恢复提示为“房主开始”。

德州 `ready` 不参与开局判定，不显示准备按钮；seats 增加以下字段：

```json
{"seat":0,"player":"p_abcd","name":"小明","avatar":"/api/app/avatar/9000?s=<signature>","bot":false,"ready":false,
 "auto":false,"auto_hands":0,"auto_since":null,
 "away":false,"away_since":null,"leave_after":false,"stack":99}
```

- `stack` 是公开的当前桌上可下注筹码，手中与 engine.seats 对应玩家 stack 相同，**不包含已下注金额**；手结束为结算后筹码；不是可用积分余额。
- `auto_since` 为本段托管起点，未托管/机器人为 null。`away` 表示下一手是否跳过；本手参与者打开暂离后立即为 true，但 `away_since` 要本手结束才写入；非本手参与者立即生效。`away_since + rules.away_seconds` 为自动离桌时刻。
- `rules` 含实际完整场次快照，例如：`{"id":"novice","name":"新手场","currency":"points","small_blind":1,"big_blind":2,"buy_min":40,"buy_max":200,"min_balance":40,"step_seconds":30,"auto_hands":10,"auto_seconds":600,"away_seconds":600}`。
- 两手间例：`{"phase":"between","hand_no":4,"button":0,"countdown_at":1792939304,"turn_deadline":null,"auto_at":null,"idle_deadline":null,"hand":null,"last_hand":<已完成hand>}`（此例省略通用字段）。暂停则 phase=waiting、countdown_at=null、idle_deadline 为10分钟无操作散桌时刻。

`hand.engine` 严格使用引擎 **`view(state, viewer)`**，不发送原始 state。以下为实际字段（引擎没有 turn，外壳为每次动作维护 turn）：

```json
{
  "number":4,"turn":1,"current_player":"p_abcd","commitment":"<hex>","finished":false,
  "engine":{
    "street":"preflop","board":[],"button":0,"small_blind":1,"big_blind":2,
    "small_blind_seat":0,"big_blind_seat":2,
    "seats":[
      {"seat":0,"player":"p_abcd","stack":99,"bet":1,"committed":1,"shown":false,"status":"active","hole":[1,3]},
      {"seat":2,"player":"p_efgh","stack":98,"bet":2,"committed":2,"shown":false,"status":"active"}
    ],
    "pots":[{"amount":2,"eligible":["p_abcd","p_efgh"],"winners":[]},{"amount":1,"eligible":["p_efgh"],"winners":[]}],
    "pot":3,"current_bet":2,"min_raise":2,"current_player":"p_abcd",
    "legal_actions":{"fold":true,"check":false,"call":1,"min_raise_to":4,"max_raise_to":100,"all_in":100},
    "finished":false,"uncalled":{},"results":null
  },
  "legal_actions":{"fold":true,"check":false,"call":1,"min_raise_to":4,"max_raise_to":100,"all_in":100},
  "result":null
}
```

- `street` 为 preflop/flop/turn/river，手结束仍保留最后街道，用 finished 区分结束。`board` 公共牌，`button`/盲注座位均为座位号。
- `engine.seats` 是本手参与者；`bet` 本街已投，`committed` 本手已投，`stack` 剩余筹码，`status` 为 active/folded/all_in。手牌字段名 **hole**，只在本人或实际亮牌时存在，**隐藏时省略 hole 字段**，不是发两张假牌；0..51 编码不变。
- `legal_actions` 德州采用引擎原结构，与炸金花不同。非当前玩家/结束为 null。fold/check 为布尔，call 为本次实际需付额（不足时截为剩余筹码，0 不应显示跟注）；min_raise_to/max_raise_to 是本街加到的总额，null 不允许加注；两者相等可能是未达到完整最小加注的短筹码全下。all_in 为全下后的本街总额，0 表示禁止全下。current_bet=0 时用 bet，否则用 raise。
- `pots` 为主池/边池，amount 池额，eligible 有资格者，winners 结算前为空。`uncalled` 为 player 到未跟注退回额的映射。
- 外层 result 等于 engine.results，结算后结构为 `{"players":{"p_abcd":{"stack":102,"net":2,"won":4,"hand":"一对","best5":[0,13,2,3,4],"shown":true}},"pots":[{"amount":4,"eligible":["p_abcd","p_efgh"],"winners":["p_abcd"]}],"uncalled":{}}`（玩家示例省略其他人）。hand 为中文牌型；best5 最好五张；未亮牌者 hand/best5=null，shown=false。净额之和为 0。
- 只按 engine/legal_actions 允许项渲染；不可应注的未跟注筹码会自动退回，可能出现无人能再跟进而 all_in=0 的合法局面，此时只能 call/fold（或 check）。

## 棋类（象棋、五子棋）

本节为棋类前后端合同（首版 2026-10-26）。游戏 ID 为 `xiangqi`、`gomoku`；均为两人桌，沿用上述开桌、快速开始、入座、准备、房主开始、离桌、HTTP 快照及 SSE 接口。棋盘公开，积分桌允许会员旁观；练习桌仍仅主人可见。`phase` 为 `waiting/playing/closed`，结束后 `hand=null`，完整结束局面放入 `last_hand`，双方须重新准备，房主可以自动算准备。练习机器人自动准备。离开进行中的对局视为认输并立即结算。

### 场次、开桌和通用字段

六场 `practice/novice/normal/pro/master/supreme`；场次和 `rules` 含 `stake`（每人每局押注）、`min_balance`，不使用 ante、cap、buy_in。默认练习押 100 欢乐豆，五档积分押 10/50/200/500/2000，赢家拿两份，和棋或作废原额退回，无抽水。开局才冻结。场次摘要示例：

```json
{"id":"novice","name":"新手场","currency":"points","stake":10,"min_balance":10,"seated":0,"waiting_tables":[]}
```

```json
{"game":"gomoku","tier":"practice","robot_level":"easy","request_id":"create_board_123"}
```

`robot_level` 仅练习场可传，`easy/normal/hard` 分别为入门/普通/困难，省略为 `normal`；积分场只有真人。`/quick-start` body 相同。`/seat` body 为 `{"seat":1,"request_id":"seat_board_123"}`（seat 可省略）；`/ready`、`/start`、`/leave` 与通用接口相同。`/auto`、`/away`、`/top-up` 不适用棋类。`max_players=2`，座位为 0、1；`start_status` 与炸金花的查看者相关结构相同，但两人都必须满足条件。`auto_at=null`，机器人思考/排队仍为 `playing` 且 `current_player` 是机器人。两种引擎合计最多同时运行 2 个进程；桌层排队加思考预算 1.5 秒，过期用合法回退，不新增前端状态字段。

`rules` 完整示例（象棋默认 `total_seconds=600, step_seconds=60`；五子棋为 300、30；时长均可配置，开桌时冻结）：

```json
{"id":"practice","name":"练习场","currency":"beans","stake":100,"min_balance":100,
 "total_seconds":300,"step_seconds":30,"offline_step_seconds":120,
 "draw_cooldown_plies":3,"auto_play":false,"robot_level":"normal"}
```

`seats[]` 保留公共字段，并追加 `color`（未首次开局前为 null；象棋 red/black，五子棋 black/white）。每局 `hand.players` 保存当局颜色，红/黑先行，同桌第一局随机，以后交换。`rules.auto_play=false` 是隐藏托管入口的依据。

### hand / last_hand 的完整棋类结构

以下是五子棋进行中的 `hand` 示例，其余顶层 Snapshot 字段沿用通用结构：

```json
{
  "number":1,"turn":2,"current_player":"p_white","finished":false,
  "players":[{"player":"p_black","seat":0,"color":"black"},{"player":"p_white","seat":1,"color":"white"}],
  "board":{"size":15,"moves":[[7,7]]},
  "moves":[{"ply":1,"player":"p_black","color":"black","move":[7,7]}],
  "last_move":{"ply":1,"player":"p_black","color":"black","move":[7,7]},
  "clocks":{"remaining":{"p_black":298.5,"p_white":299.2},"running":"p_white","turn_started_at":1792939300,"turn_deadline":1792939330},
  "in_check":null,"draw_offer":null,
  "legal_actions":{"move":{},"resign":{},"offer_draw":{}},
  "result":null
}
```

- 五子棋 `board.moves` 是实际落子顺序，黑白交替，坐标 `[x,y]` 均为 0..14，左上角为 `[0,0]`，x 向右、y 向下；15×15 自由规则，任意方向五子或更多胜，下满和棋。`moves` 是带玩家、颜色的同一历史；`last_move` 是其最后一项，无落子时 null。
- `turn` 是动作版本，不是落子数；认输、求和、拒绝、悔棋等也推进，客户端一律使用最新快照。`ply` 是棋谱内从 1 起的半回合序号。
- `clocks.remaining` 为截至外层 `server_time` 双方剩余总秒数（非负小数）；`running` 是正在扣时的 player，结束为 null；`turn_started_at` 是本步起点，结束为 null。`clocks.turn_deadline` 与外层 `turn_deadline` 一致，为总时长和本步上限中较早的绝对 Unix 截止时间，结束为 null。客户端自行逐秒显示，不要求每秒 SSE。
- 当前玩家本桌无有效 SSE 且无近期 HTTP 活动时，本步上限放宽到 120 秒，总时长仍扣除，回来可继续，已获宽限不收回。在线使用共享的本桌 presence 记录（HTTP 在线窗口 30 秒）；放宽后的截止时间持久化，重启不重置。开始时没有走过第一步且先手到期则 `aborted` 退回；以后到期 `timeout` 判负。求和及回应不暂停、不重置棋钟。
- `draw_offer` 是 `null` 或 `{"player":"p_black","ply":8}`；对方落子视为拒绝。相同玩家再次求和须距离上次至少 3 次落子（悔棋不回退冷却计数）。机器人会拒绝求和。
- `legal_actions` 是**查看者**能执行的动作对象；旁观者/结束为 `{}`，非当前玩家仍可认输、求和或回应求和。无资格的键直接省略，不以空值代表禁止。练习场只有轮到真人、且自己的上一步及机器人应着都存在时有 `undo`；撤回这两步，不退还已经用掉的总时间。
- `result` 未结束为 null，结束为 `{"winner":"p_black","reason":"five","net":{"p_black":10,"p_white":-10}}`。winner 是 player 或 null（和棋/作废），net 含双方且总和为 0。和棋或作废双方 net 都是 0。结束后 current_player=null，legal_actions={}，棋盘、棋谱、颜色和停止后的剩余棋钟保留。

象棋 `hand` 的公共字段完全相同，差别如下（示例为红方刚开局、红方查看）：

```json
{
  "number":1,"turn":1,"current_player":"p_red","finished":false,
  "players":[{"player":"p_red","seat":0,"color":"red"},{"player":"p_black","seat":1,"color":"black"}],
  "board":{"fen":"rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w"},
  "moves":[],"last_move":null,
  "clocks":{"remaining":{"p_red":600,"p_black":600},"running":"p_red","turn_started_at":1792939300,"turn_deadline":1792939360},
  "in_check":false,"draw_offer":null,
  "legal_moves":["h2e2","h2f2"],
  "legal_actions":{"move":{},"resign":{},"offer_draw":{}},"result":null
}
```

示例 `legal_moves` 仅展示两项，实际响应是完整合法着法数组。着法字符串如 `h2e2`，列 a..i、行 0..9，**红方底线是第 0 行**；FEN 自上而下从黑方底线（第 9 行）排列。`moves[].move` 为同格式字符串，`board.fen` 是当前局面，规范输出为**两字段 FEN**（棋子布局、`w/b` 行棋方），前端不要依赖末尾四个计数字段；引擎也能读取六字段 FEN。`in_check` 为当前走棋方是否被将军。**仅进行中且当前走棋方本人拿到 `legal_moves` 字段；对手、观众、结束局面直接省略此字段**，不可由共享快照缓存泄漏。

结束原因：象棋 `checkmate/stalemate/perpetual_check/repetition/move_limit/insufficient/timeout/resign/draw_agreed/aborted`；五子棋 `five/board_full/timeout/resign/draw_agreed/aborted`。

### 动作请求与错误

统一 POST `/tables/{table_id}/actions`，都必须有 `request_id/hand_no/turn`，成功返回 `{"table":<Snapshot>}`：

```json
{"request_id":"move_board_123","hand_no":1,"turn":2,"action":"move","move":[7,8]}
```

| action | 额外请求字段 | 条件 |
|---|---|---|
| move | 象棋 `"move":"h2e2"`；五子棋 `"move":[7,8]` | 只允许当前玩家的合法着法 |
| resign | 无 | 双方进行中随时可认输 |
| offer_draw | 无 | 无待处理求和且本人不在冷却期 |
| accept_draw | 无 | 仅收到对方求和者，立即议和结算 |
| reject_draw | 无 | 仅收到对方求和者，继续原有棋钟 |
| undo | 无 | 仅练习场真人、本人回合且可撤回完整的两步 |

无额外参数的完整请求示例：`{"request_id":"draw_board_123","hand_no":1,"turn":2,"action":"offer_draw"}`，其余动作仅替换 action。

复用通用错误格式和鉴权/幂等错误。棋类新增或明确：`400 invalid`（坐标/着法格式/难度/棋类不支持的桌操作）、`400 illegal_action`（非本人回合、非法落子、不能悔棋或无对方求和）、`409 draw_cooldown`（三次落子内再次求和）、`409 stale_turn`（局号或版本不符，包括已结束）、`409 not_ready`（另一人尚未准备或双方余额不足）、`400 insufficient_funds`（准备余额不足）。期限到达后的请求先结算，再返回 stale_turn，不会用迟到动作延长棋钟。棋类不使用洗牌复核接口，调用返回 `400 invalid`。

## 6. 复核

会员界面不调用此接口，也不展示复核入口；接口保留，供排查争议时核对牌序。

### GET `/tables/{table_id}/hands/{hand_no}/verify`

正在进行：`{"table_id":"abc123","hand_no":1,"commitment":"<hex>","settled":false}`。
结算后：

```json
{"table_id":"abc123","hand_no":1,"commitment":"<hex>","settled":true,
 "deck":[12,24,34],"deck_codes":"As,Kh,Tc,...","salt":"<hex>",
 "deal_order":["p_abcd","p_efgh"],"result":{"winners":["p_abcd"],"awards":{"p_abcd":4,"p_efgh":0},"net":{"p_abcd":2,"p_efgh":-2}}}
```

上述 deck/deck_codes 仅为排版缩写；真实响应是完整 52 张、不带省略号/空白的 ASCII 牌码，例如 `As,Kd,Tc,...`，点数 2..9/T/J/Q/K/A、花色 s/h/c/d；验证 **SHA256(UTF-8(deck_codes + "|" + salt))**。炸金花按 deal_order 顺序每人连续取三张。**德州**：seat 号递增为顺时针，deal_order 从按钮左边第一个参与座位开始，绕一圈至按钮；按此顺序逐人发一张，共两圈（两人局同样先给非按钮者）。deck[0] 是第一张；N 人时玩家 i 的底牌为 deck[i]、deck[N+i]。之后从 deck[2N] 烧1张，翻牌取随后的3张；再烧1张，转牌取1张；再烧1张，河牌取1张，即公共牌索引为 2N+1/2N+2/2N+3/2N+5/2N+7。无人跟进而结束时不补发未进行的街道。结算前绝不返回 deck/salt。按照规格，结算后公开整副牌序，技术上可推导已弃牌的牌；普通桌快照仍遵守不公开弃牌的显示规则。

## 7. SSE

### POST `/tables/{table_id}/stream-token`

请求 `{}`（仅签发，不是游戏动作，无需 request_id），带 initData。

```json
{"token":"<signed token>","expires_at":1792939420,"url":"/api/app/games/tables/abc123/events?token=<signed token>"}
```

token 绑定会员和该桌，签名密钥为进程随机值，120 秒有效，重启失效；仅用于 SSE，不能当动作凭证。不要放 localStorage、日志或转发给别人。应用会从 uvicorn access 日志去掉该 token；部署时代理/CDN 也应关闭本路径查询串日志，避免代理独立记录凭证。到期断开，先用 initData 重取 token 再重连；客户端 `onerror` 关闭旧 EventSource，退避重连。每桌每用户最多 3 条连接，超限 429。

### GET `/tables/{table_id}/events?token=...`

EventSource(url)，无需额外头。响应 Content-Type text/event-stream、Cache-Control no-store、X-Accel-Buffering no。
连接后立刻发该用户快照，之后桌版本变化推送，15 秒无变化 keepalive：

```text
id: 8
event: snapshot
data: {"table": <Snapshot>}

: keepalive

```

连接按牌桌及会员单独计数；只要本人还有一条有效 SSE 连接，等待阶段就不会因 180 秒离线规则被移除。
每次快照和保活都刷新内存在线时间；最后一条连接关闭后，重新起算完整 180 秒无访问期限。
这不会延长行动截止、房主无操作继任或 10 分钟无人操作散桌时间。连接取消、失效或到期均清理在线计数。

`Last-Event-ID` 无需补历史：重连总是给最新全量快照，不播发旧动作。`snapshot` data 与 GET 桌完全相同。连接时验证 token 签名、到期、桌绑定、用户仍存在和练习桌归属。到期/权限失效关闭连接；发卡故障不影响 SSE。仅发送当前查看者视图，无跨用户私牌缓存。

## 8. 错误

与会员面板统一：

```json
{"detail":{"code":"stale_turn","message":"牌局已变化，请刷新后操作"}}
```

| HTTP | code | 含义 |
|---|---|---|
| 401 | unsigned / expired / invalid_token | initData 无效/过期，或 SSE token 无效 |
| 403 | unbound / forbidden | 未绑定，非本人座位/非房主等 |
| 404 | not_found | 桌/手牌不存在或不应可见 |
| 409 | already_seated / table_full / wrong_phase / stale_turn / idempotency_conflict / not_ready | 占桌、满桌、阶段冲突、过期动作、键冲突、人数不足 |
| 400 | invalid / illegal_action / insufficient_funds | 参数错误、非法动作、可用余额不足 |
| 429 | too_many_streams | SSE 连接数上限 |
| 503 | disabled | 会员面板或新开/入桌功能未开放 |

客户端应显示 message，409 后获取最新桌；不得通过重试新 request_id 猜测资金操作是否成功。JSON 解析失败、非对象 body 或路径/查询参数类型错误沿用 FastAPI 的 422 验证响应 `{"detail":[{"type":"...","loc":["body"],"msg":"..."}]}`；客户端显示通用“请求参数无效”。

## 9. Deck 管理端（不是会员 API）

插件 `game_center`，默认总开关/单游戏/播报关闭。棋类追加 `xiangqi_enabled/gomoku_enabled`、`xiangqi_tiers/gomoku_tiers` 和各自 `*_total_seconds/*_step_seconds`；声明场次 JSON、行动超时、托管阈值、235、欢乐豆、授权播报群等 Field；插件 readonly_status 给真人在桌数、今日局数、积分投入流水（炸金花下注 + 今日已结算德州各手 committed 之和，扣除未跟注退回）、今日德州冻结转移额（只计出账一侧）。群播报发送前用 getMe 确认对应 Bot 已配置 Main Mini App；未配置则保留任务重试，不发普通网页假深链。初次发卡结果不明或租约过期进入 unknown，不再盲发；没有确认的 message_id 不能安全编辑/删除，需管理员核实原群卡，状态计数在插件卡可见。
使用现有后台登录鉴权的 `GET /api/game-center/cash?table_id=...&user_id=...&limit=100` 查询（至少一个条件，最多500条），返回 `{"events":[{"id":1,"scope":"gc_zjh","ref":"abc123:1","user_id":"...","operation":"reserve","amount":50,"target":"","created_at":1792939300}]}`；可按桌/用户交叉筛选，仅管理端允许内部会员 ID。
