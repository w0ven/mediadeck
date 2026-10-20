# CMCC/GD 8339 播放网关

版本化源码，不是热补丁。本目录随仓库发布；运行时配置只通过环境变量进入，仓库不包含真实域名或生产路径。

## 仓库路径

- `deploy/gateway/gateway.py` — 运行文件（原文件名 `gateway.py`）
- `deploy/gateway/test_registry_cache.py` — 与网关同目录的定向测试
- `backend/tests/test_cmcc_gateway_registry.py` — pytest 入口，导入上述网关

不要提交 `sources.json`、OpenList token、Emby key 或主机专属路径。默认注册表是与本文件同目录的 `sources.json`；测试会改写 `gateway.REG`。

## 环境变量

| 变量 | 作用 | 未配置时 |
|---|---|---|
| `CMCC_GATEWAY_REGISTRY` | 注册表 JSON 绝对路径 | `Path(__file__).with_name('sources.json')` |
| `CMCC_OPENLIST_PUBLIC_HOST` | 允许的 OpenList 公网 host（仅 host，无 scheme/path） | `example.invalid`，`resolve` **拒绝** 任何来源 URL |
| `CMCC_DUANJU_LOCAL_REGISTRY` | 短剧本地播放索引 JSON 的绝对文件路径 | 未设置则不启用本地分支 |
| `CMCC_DUANJU_EMBY_ROOT` | 索引内 Emby 虚拟文件路径的绝对目录前缀 | 未设置则不启用本地分支 |
| `CMCC_DUANJU_STORAGE_ROOT` | 对应的本机媒体文件绝对目录前缀（用于文件校验） | 未设置则不启用本地分支 |

三个短剧变量必须全部显式设置为非根目录、无 `.` / `..` / 空路径分段的绝对路径；任一缺失或无效，则本地分支禁用、维持原有 Deck / CMCC 路由。索引文件及具体路径仅保存在部署环境，不进入仓库。索引中 `root` 必须是虚拟前缀下 `hg-<数字>`，`path` 必须是该目录下四段相对媒体路径（含 `Season 01`、` - S01E001.mp4`），对应本机文件 size/mtime 必须匹配。命中时仍需调用者 token 对该 item 的 PlaybackInfo 可见，并核对 `MediaSources` 中同一 `File` 类型、source ID 与路径；通过后才将 GET/HEAD、Range 等转发给本地 Emby。本分支现在在取 PlaybackInfo/转发字节前主动调用 Deck 的完整播放准入；不再依赖普通 stream/original 位置的外层 nginx 子请求。迁移顺序见下文。

未配置或非法的公网 host **不会**从注册表 URL 推断允许主机。部署主机必须显式设置 `CMCC_OPENLIST_PUBLIC_HOST` 才能签发云盘 302。

Emby / OpenList / Deck 仍走 loopback 默认：`127.0.0.1:8096`、`127.0.0.1:5244`、`127.0.0.1:8300`。

## 注册表缓存

未变文件不再每次 `json.loads` 注册表。缓存键为 inode + mtime_ns + size：

- 未变：只解析一次
- 原子替换 / 原地修改 / 删除：立即失效
- 并发：锁内只加载一次
- 读失败、JSON 损坏、非 object：清空缓存，不复用旧授权/旧来源

不缓存 OpenList/网盘最终直链。未登记 edition 仍转发 Deck；`/cmcc-source/` 要求既有能力 hash，已登记 edition 的初始 stream/original 则要求完整播放准入及该调用者的 edition 可见性。302 允许后缀仍为 `.cmecloud.cn` / `.139.com` / `.10086.cn`。

## 唯一播放准入执行点

| 路径/分支 | 准入所有者 | 后续校验/行为 |
|---|---|---|
| 未知 edition、非静态请求、未命中本地索引：`forward` | 8300 原 stream handler 的 `_admit_playback` | gateway 不先调准入，原方法/URI/身份完整转发；8300 直连同样检查 |
| 已登记 CMCC 静态 stream/original | gateway 调 8300 `/api/playback/admit` | 成功后仍用调用者 token 检查精确 edition，再 resolve |
| 已核实本地 File stream/original | gateway 调同一准入 | 成功后仍核实 File/source/path，原 GET/HEAD/Range 转发 Emby |
| 原生 PlaybackInfo、生命周期 | 原 Deck 专用 handler | 不改变 reserve=False、原子 play 绑定或停止处理 |
| 普通 HLS/其他直通 Emby 播放 | nginx `auth_request /_deck_admission` | 此 gate **必须保留**；字节不经 Deck |
| 白名单 HLS、非静态、文件等 | 原 whitelist 专用 handler/nginx gate | **必须保留** md_route/个人身份/会员/签名校验 |
| `/cmcc-source/<name>?cap=...` | 原有 bearer capability 校验 | 已签发 source URL，不是 stream-start API；保留原合约，白名单该 location 的 `_wl_admission` 仍保留 |
| `/health` | 无播放权限 | 不读注册表，不签发/转发媒体 |

所有完整准入都仍调用现有 token、item、web、client、同播及设备规则。没有授权 TTL 缓存，没有“已准入”头或按席位免授权。gateway 对每个自签发请求只接受 Deck **204**，401/403 原样拒绝，其余状态/网络异常按 503 关闭；不跟随重定向、不回退 origin。forward 的 Deck 故障同样不会回退 Emby。

`X-Original-URI/Method` 只由 gateway 根据实际请求覆盖，客户端同名头不参与决定；重复身份头原样送给 Deck 拒绝歧义。不是信任客户端断言。白名单直发仍凭已有注册入口凭据选择受限模式，但个人身份、会员状态等每次重查。Deck 同步补充严格原始 URI/重复 query 检查，不能因移除 nginx 子请求而丢失该边界。

## 协调发布/回滚（需单独授权，走仓库 PR/release）

不要直接复制工作树热补丁、reset 运行目录或单独先发布 nginx。仅将本修复的限定差异通过分支/PR、CI、审查合并及新 release/tag 发布；不要夹带运行目录其他未提交文件。这里不新增运行环境变量，也不改变注册表/schema。沿用已有私有配置，不输出凭据。

1. **先 Deck 后端**：部署同一 release 的 per-user 锁与 whitelist 原始 URI 校验，保持原 nginx 全部 auth_request、原 gateway；按已批准流程切换单 worker 后端，确认旧进程代际已退出。原流程仍安全，只是还未去重。
2. **再 gateway**：发布匹配版本至全部 gateway 实例，仍保持原 nginx auth_request。新旧 gateway 共存阶段，旧 nginx 仍守住自签发分支；forward 仍由 Deck 再检查。等所有旧 gateway 进程/在途请求完成，验证版本与故障拒绝行为。此阶段新 gateway 的自签发会暂时有两次完整准入，是有意的安全过渡。
3. **最后 nginx**：仅移除转发 8339 的普通 stream/original location 的旧 `auth_request /_deck_admission;`，以及白名单 `location @wl_direct` 的旧 `auth_request /_wl_admission;`。不改 proxy_pass、入口身份头、原始 URI、fallback、日志等现有设置。新安装可使用 `deploy/nginx/gateway-stream.conf`，放在 `playback-admission.conf` 前；按注释定义 upstream。白名单按 `whitelist-origin.conf.template` 的限定差异更新。先审核配置、`nginx -t`，授权后 reload。新旧 nginx workers 共存时，旧配置多查一次、新配置只查一次，都有完整准入。
4. **验收**：先 staging 跑集成测试，确认 GET/HEAD 的 forward 完整准入/Sessions 从原先 2 次降为 1 次，自签发分支从过渡期的 2 次降回 1 次；直连8300/8339、撤销/封禁/超限/超时不签发，白名单/HLS/原生路径不变。生产验证需另外授权；可先被动比对 admission 延迟、403/503、上游负载。不需要重启 Emby、挂载或播放节点。
5. **回滚严格逆序**：先恢复两个 nginx 外层 gate，检查并经授权 reload；**等待所有原 gate-free nginx workers/keepalive 请求完全排空**，确认新请求确经外层 gate，再回滚 gateway，最后 Deck 后端。不能仅重启旧 gateway 而保留无外层 gate 的 nginx；那会使旧自签发分支无完整准入。保留现有数据库与租约，不整树回滚或覆盖其他未提交工作。

允许的混合状态：旧 nginx + 旧 gateway + 新 Deck；旧 nginx + 新 gateway + 新 Deck；新旧 nginx workers + 全量新 gateway/新 Deck。**禁止新 nginx + 任一旧 gateway，或在白名单直发去掉外层 gate 后回退 Deck URI 校验。** 不以健康检查 `ok` 代替版本/准入行为确认。

## 测试

```
python3 deploy/gateway/test_registry_cache.py
python3 deploy/gateway/test_duanju_local.py
python3 backend/tests/test_cmcc_gateway_registry.py
# 项目测试环境中，从 backend/ 执行（mock + 临时数据库/回环服务）
python -m pytest tests/test_gateway_admission_integration.py tests/test_whitelist_nginx.py
```
