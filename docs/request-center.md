# 求片中心（本地实现，未部署）

## 产品语义

- 用户确认提交后为 **待处理**（`open`），创建工单与扣一次月额度在同一事务内。
- 上片员点击 **接受请求** 即为终态 **已接受**（`accepted`），之后手动安排下载。没有接单、处理中、二次完成、待上架或已上架。
- **已拒绝**（`rejected`）的理由完全选填；空理由不显示、不生成默认理由。
- 用户仅可在待处理时撤回为 **已取消**（`cancelled`）。询问/回复不改变待处理状态。
- 不自动下载、不检查或追踪入库、不连接 MoviePilot、不增加积分收费。接受通知固定为：
  > 上片员已接受你的请求，将安排下载，请耐心等待下载完成并入库

## Bot

- 用户私聊 `/requests` 或主菜单「求片中心」。上片员私聊 `/uploader`（兼容 `/req`），只需 uploader 角色，不要求 admin；管理员也可处理。
- 已允许交互的用户群中 `/requests` 只提供私聊深链，不发送工单内容。本轮没有启用专用处理群、改动现网配置或新增自动外发目标。
- 中英文片名搜索，电影/剧集、年份筛选与候选分页；TMDB 链接或纯数字编号。纯数字必须选择类型，明确类型永不回退到同编号的其他作品。
- 候选、上片员新请求通知和详情均为真实海报卡；优先中文标题，辅以原名、年份、类型、简短简介及季集/版本等需求。TMDB 为次要详情链接。图片失败文字回退；元数据失败明确「暂未获取片名」，保留类型及编号。
- 电影整片/补版本，剧集全剧/指定季/指定集/补版本。清晰度、字幕、配音、备注默认无要求。确认页展示完整需求及扣次；长内容通过「完整需求 / 详情」查看，不丢失完整工单数据。
- 媒体库仅在选片时做只读、按用户权限范围的辅助查询；可打开现有条目详情，仍允许补集/补版本。查询失败或不完整明确显示未知，不当作没有，不阻止提交。
- 「我的求片」「我的关注」与上片员工作台均有状态/电影剧集筛选、搜索、分页、详情和沟通记录。用户可修改自己的待处理需求、补充/回复和撤回；关注者不能修改或回复他人的工单。
- 上片员可直接接受、拒绝或询问；拒绝支持无理由、快捷理由、自定义理由。内部备注仅上片员/管理员可见，不进入用户通知。
- 管理员可纠正状态、人工退回一次额度，均保留内部操作记录；纠错不自动退款，拒绝/撤回也不自动退款。

## 数据、兼容与并发

`request_schema.py` 在已有数据库上增列/建附属表，不重建或删除历史工单：

| 原状态 | 新状态 | 兼容说明 |
| --- | --- | --- |
| open | open / 待处理 | 保留待处理 |
| claimed | accepted / 已接受 | 原已接单对应新的接受终态 |
| done | accepted / 已接受 | 不推断已下载、入库或上架 |
| rejected | rejected / 已拒绝 | 原理由原样保留 |

- 原状态保存在 `legacy_status`；原用户、备注、处理人、时间、额度记录保留。迁移不扣次、不生成历史通知。迁移可重复执行。
- 去重键为作品 ID + 电影/剧集类型 + 规范化需求（范围、季集、版本、清晰度、字幕、配音）。普通备注是补充沟通，不作为绕过去重的差异。
- 同需求待处理可关注原单，不新建、不扣次；额度用完仍可关注。已接受的相同需求提示等待入库，不再次扣次；可选择不同缺集或版本。
- 月额度沿用原有 UTC 自然月规则。人工退回每工单仅一次，只退原扣次月份，不能把已过月份的次数转入本月。
- SQLite `BEGIN IMMEDIATE` 与条件更新（`open` + `revision`）保证多连接竞争只有一人终结；修改需求/管理员纠错增加 revision，旧按钮不能终结新版本。
- Bot 操作能力绑定随机 token、私聊、消息 ID、账号、允许动作、工单 revision 和期限；输入/卡片持久化。离开/取消草稿撤销隐藏输入与草稿能力，重启可从列表恢复；越权、跨卡、过期、重复操作均重验。

## 通知与恢复

工单变更、沟通消息和通知 outbox 在同一事务提交。投递有租约与发送记录，失败自动重试，也可从工作台/Web 手动重试；只重发未确认的通知，不重扣额度、不重做处理。接受/拒绝/撤回及管理员纠正通知求片人和关注者；询问/回复保留独立工单归属。旧操作卡刷新；历史沟通收据仅有只读详情按钮，不会被刷新抹掉消息。

**Telegram 协议边界：** 已确认成功的投递不会再次发送，并发重试受租约互斥。但 Telegram `sendMessage/sendPhoto` 没有客户端幂等键；如果远端已发送而响应丢失，或进程在发送成功与本地落库之间崩溃，无法承诺绝对 exactly-once，恢复重试可能出现重复通知。工单终态和额度仍严格不重复执行。

## Web/API

Web 求片页面使用相同四状态、需求详情、沟通记录、内部备注、纠错与人工退回额度、通知投递状态/重试。

所有管理接口沿用现有管理员认证，不能通过 payload 冒用处理人：

- `GET /api/requests`：`status / media_type / search / limit / offset`
- `GET /api/requests/stats`
- `GET /api/requests/{id}`：详情、分页事件及通知投递状态
- `POST /api/requests/{id}/accept`、`/reject`：必传当前 `revision`；拒绝的 `note` 可空
- `POST /api/requests/{id}/messages`：`revision / body / internal / key`，消息 key 幂等
- `POST /api/requests/{id}/correct`：`revision / status / reason`
- `POST /api/requests/{id}/refund`：`reason`
- `POST /api/requests/{id}/notifications/retry`

旧 `/claim` 和 `/resolve` 返回 **410**，旧 Bot 按钮明确失效，绝不把旧「接单/已处理」按钮偷偷解释为新操作。

## 本地验证

使用现有 `backend/.venv`，不安装依赖、不修改 `uv.lock`、不连接真实 Telegram/TMDB/媒体库。

- 模拟 Telegram 测试经过实际 Bot handler 与真实 SQLite：搜索/海报/季集版本/完整确认扣次/关注/询问回复/内部备注/修改撤回/多人终结/重启/过期与跨卡/降级/投递重试。
- 旧 schema 迁移夹具检查历史状态、原始数据、额度及零历史通知；跨连接竞争验证唯一创建与一次退款。
- 真实 Chromium 加载真实页面脚本，通过隔离 FastAPI TestClient 操作真实管理 API；外部图片用明确标注的本地海报夹具拦截。验证分页、搜索、季集展示、无理由拒绝、询问仍待处理、内部备注隔离、纠错、接受终态、额度退回、通知重试、桌面及移动端。

```sh
cd backend
MEDIADECK_REQUEST_ARTIFACTS=/opt/openbear/workspace/artifacts/request-center \
  .venv/bin/python -m pytest tests/test_requests.py tests/test_request_bot.py \
  tests/test_tmdb.py tests/test_requests_ui.py -q
```

本轮大范围回归：**1805 passed / 56 skipped**；最终求片/权限/交互/真实浏览器专项 **322 passed**。另有 2 个已知单测 deselected，以及原基线即初始化失败的 `test_audit_web_browser.py` 模块未计入通过数。完整全量尝试及独立 `8dd6e28` 基线复现日志均保留：

- `test_entry_management_browser`：既有夹具未提供 `deckConfirm`。
- `test_member_list_db_work_does_not_stall_other_pages`：既有隔离测试访问已关闭的 app.state 数据库。
- `test_audit_web_browser.py`：既有浏览器夹具初始化时 `state is not defined`。

不修改这些无关基线问题；本轮新增的求片真实浏览器测试独立通过。工件目录：`/opt/openbear/workspace/artifacts/request-center/`，含浏览器检查 JSON、桌面列表、接受详情、移动端详情截图、`simulated-telegram-report.json` 本地模拟全生命周期记录，以及回归/基线日志。
