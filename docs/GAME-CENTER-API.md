# 游戏中心 Mini App API

版本：v1，2026-10-25。权威规则见 `GAME-CENTER.md`。根路径 `/api/app/games`。
炸金花 `zjh` 与德州扑克 `holdem` 均已实现，`available:true`；后台总开关及各游戏开关仍默认关闭，关闭时开桌/入桌返回 `503 disabled`。牛牛 `niuniu`、斗地主 `doudizhu`、五子棋 `gomoku`、象棋 `xiangqi` 为 coming_soon。

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

`{"request_id":"leave_12345678"}`。等待中立即离座；炸金花活跃手牌视为弃牌并立即结算该手应得部分后离座（若仍有其他玩家未结束，本人标记 leave_after=true，结算后移除）；德州本手参与者（包括已弃牌/全下者）标记 leave_after=true，延后到手结束结算离桌；未全下者轮到时约1.5秒自动弃牌，不再额外投入，已全下者仍保留分池资格。期间不能补码/手动动作/取消托管。非参与/已结算座位立即离开。房主移交最早入座的其他会员；无真人时散桌。返回统一桌响应。

### POST `/tables/{table_id}/ready`

`{"ready":true,"request_id":"ready_12345678"}`；取消 `ready:false`。
仅炸金花等待阶段；余额不足封顶不允许准备。全员准备且至少两人自动 5 秒倒计时，有人入座/取消则取消。每手结束需要重新准备（托管玩家自动准备，但达到连续托管手数上限自动离座）。

### POST `/tables/{table_id}/start`

`{"request_id":"start_12345678"}`。仅房主，且等待阶段。
炸金花房主自动算准备，已准备且余额足够者至少两人才能开，其他人在本手旁观。德州此接口仅用于首手，至少两名有筹码非暂离玩家；满六人自动首手，后续约 4 秒连续发牌，无需也不能再次 start。可参与者不足两人时 waiting 暂停，补码/恢复/新入座使人数足够后自动进入4秒倒计时。筹码为0者不发牌、不扣盲，提示补码或离桌。

### POST `/tables/{table_id}/auto`

`{"enabled":true,"request_id":"auto_12345678"}`。随时切换托管；false 取消。开启后到本人时 1–2 秒自动行动，普通行动超时自动开启。取消后不会重置原始 30 秒截止时间。

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
    {"seat":0,"player":"p_abcd","name":"小明","bot":false,"ready":false,"auto":false,"auto_hands":0,"away":false,"leave_after":false},
    {"seat":2,"player":"p_efgh","name":"小雨","bot":false,"ready":false,"auto":true,"auto_hands":1,"away":false,"leave_after":false}
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
- `revision` 单调递增，任何桌变化都会更新；按 revision 去重 SSE。`hand_no` 桌内递增，动作 turn 在每手从 1 起递增。
- `countdown_at` 炸金花全员准备开局时刻/德州下一手开局时刻；`turn_deadline` 原始行动截止；`auto_at` 托管行动时刻；`idle_deadline` 等待散桌时刻。无计时器为 null。房主等待 3 分钟无操作自动转给最早入座的其他玩家。
- `hand` 当前进行中手牌；等待时 null，上一手放 `last_hand`，结构相同且 `finished:true`。结算的 result 为 `{"winners":["p_abcd"],"awards":{"p_abcd":4,"p_efgh":0},"net":{"p_abcd":2,"p_efgh":-2}}`，所有净额之和为 0。
- 自己未看牌时也不下发 cards；其他人的牌仅正式统一摊牌的在局玩家公开。弃牌和比牌输掉者不在快照公开其牌。
- 比牌双方的 `comparisons` 包含 `{"turn":3,"actor":"p_abcd","target":"p_efgh","loser":"p_efgh","cards":{"p_abcd":[12,25,7],"p_efgh":[0,16,32]}}`，第三人该数组无此项；不得把私人动作响应广播给整桌。
- `notice` 仅本人的超时/余额不足等提示；超时文字为“已超时，进入托管；点「取消托管」接管”。

### 德州 Snapshot 与 hand 结构

通用桌壳不变，另有 `button`（尚未开手为 null，否则上次/当前按钮座位号）。德州 `ready` 不参与开局判定，不显示准备按钮；seats 增加以下字段：

```json
{"seat":0,"player":"p_abcd","name":"小明","bot":false,"ready":false,
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

## 6. 复核

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

插件 `game_center`，默认总开关/单游戏/播报关闭。声明场次 JSON、行动超时、托管阈值、235、欢乐豆、授权播报群等 Field；插件 readonly_status 给真人在桌数、今日局数、积分投入流水（炸金花下注 + 今日已结算德州各手 committed 之和，扣除未跟注退回）、今日德州冻结转移额（只计出账一侧）。群播报发送前用 getMe 确认对应 Bot 已配置 Main Mini App；未配置则保留任务重试，不发普通网页假深链。初次发卡结果不明或租约过期进入 unknown，不再盲发；没有确认的 message_id 不能安全编辑/删除，需管理员核实原群卡，状态计数在插件卡可见。
使用现有后台登录鉴权的 `GET /api/game-center/cash?table_id=...&user_id=...&limit=100` 查询（至少一个条件，最多500条），返回 `{"events":[{"id":1,"scope":"gc_zjh","ref":"abc123:1","user_id":"...","operation":"reserve","amount":50,"target":"","created_at":1792939300}]}`；可按桌/用户交叉筛选，仅管理端允许内部会员 ID。
