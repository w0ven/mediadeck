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

| 修改密码 | Bot 改密的同一套步骤（设置 Emby 密码、解除登录锁定、使旧面板登录失效、审计）；随机密码在浏览器端生成，响应丢失时用户仍知道自己提交的密码 |
| 移除设备 | 只能移除 Emby 设备记录里最后使用者是本人的设备；删除设备记录会吊销该客户端的登录，其他设备不受影响，之后可用密码重新登录 |

注册、换绑、删号、签到命令、群内玩法和管理员功能留在 Bot 里。

## 设备与推荐播放器

- 「我的设备」列出 Emby 当前保留登录的设备（`/Devices`，按 `LastUserId` 过滤，全局缓存 20 秒），不再向会员展示「设备分组」统计；后台的设备分组统计保持不变。
- 推荐播放器在 Deck「机器人 → 播放线路 → 推荐播放器」配置，每行 `平台 | 名称 | HTTPS 下载链接 | 说明`，留空则面板不显示该区块。
- Bot 的「我的设备」显示同一份列表，并提供直达面板设备页的按钮（`/app?page=devices`）。

## 头像

成员头像经 `/api/app/avatar/{tg_id}?s=<签名>` 转发：用 Bot API 取 Telegram 头像，缩成 160px JPEG，在内存中缓存 6 小时（取不到时缓存 30 分钟）。签名为进程内随机密钥，只发给面板里实际出现的成员；取不到头像时显示首字母。

排行榜昵称规则与 Bot 观影榜一致：先用 TG 昵称，其次 @用户名，从不显示 Emby 账号名。

## 上线配置

1. 给面板一个独立的 HTTPS 域名，反向代理到后端，并**只放行** `/app`、`/app/*` 和 `/api/app/*`；管理后台路径不要经这个域名暴露。
2. Deck「机器人 → 首页外观」里填写「会员面板地址」，例如 `https://app.example.com/app`。留空即隐藏 Bot 按钮，并让 `/api/app/*` 返回 503。
3. 入口是私聊消息里的 `web_app` 按钮，不需要在 BotFather 注册 Mini App；群里不会出现这个按钮（Telegram 不允许）。
