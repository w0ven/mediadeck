# 会员面板（Telegram Mini App）

会员在 Bot 私聊首页点「📱 会员面板」打开。面板只是 Bot 已有功能的另一种界面：身份、续期、兑换、求片、称号和邀请都复用 Bot 背后的同一套服务和防重复扣除逻辑，不另立规则。

## 组成

- 页面：`GET /app`，静态资源在 `/app/assets/`（`backend/app/static/miniapp/`）。
- 接口：`/api/app/*`。每个请求都在 `X-Telegram-Init-Data` 头里带 Telegram WebApp `initData`，后端用 Bot Token 校验签名（HMAC-SHA256，`WebAppData` 派生密钥），签名超过 24 小时视为过期。只信任签名覆盖的 Telegram 用户 ID，再按绑定关系找到账号；未绑定返回 403。
- 海报：影片榜海报通过 `/api/app/poster/{id}?s=<签名>` 转发，签名为进程内随机密钥，只能取榜单里出现过的条目。

## 与 Bot 的对应

| 面板操作 | 复用的逻辑 |
| --- | --- |
| 续期 30 天 | `activity_renew_intents` 确认意图 + `LevelService.renew`，同一意图重复确认只扣一次 |
| 自动保号开关 | `LevelService.set_auto`，带绑定版本，换绑后旧页面不能改 |
| 商城兑换 | `ShopService.redeem`，客户端生成一次性 `request_id`，并核对商品规格未变 |
| 使用道具 / 创建称号 | `InventoryService.use`，之后同步成员权益 |
| 佩戴称号 | `TitleService.wear`，需要已配置称号群（与 Bot 相同） |
| 求片 | `RequestService.create`，`create_key` 防重复；同一需求已存在时自动改为免费关注 |
| 生成邀请码 | `RegistrationService.spend_quota_for_invite` |

注册、换绑、修改密码、设备管理、删号、签到命令、群内玩法和管理员功能留在 Bot 里，面板只提示回到 Bot。

排行榜昵称规则与 Bot 观影榜一致：先用 TG 昵称，其次 @用户名，从不显示 Emby 账号名。

## 上线配置

1. 给面板一个独立的 HTTPS 域名，反向代理到后端，并**只放行** `/app`、`/app/*` 和 `/api/app/*`；管理后台路径不要经这个域名暴露。
2. Deck「机器人 → 首页外观」里填写「会员面板地址」，例如 `https://app.example.com/app`。留空即隐藏 Bot 按钮，并让 `/api/app/*` 返回 503。
3. 入口是私聊消息里的 `web_app` 按钮，不需要在 BotFather 注册 Mini App；群里不会出现这个按钮（Telegram 不允许）。
