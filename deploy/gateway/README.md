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

三个短剧变量必须全部显式设置为非根目录、无 `.` / `..` / 空路径分段的绝对路径；任一缺失或无效，则本地分支禁用、维持原有 Deck / CMCC 路由。索引文件及具体路径仅保存在部署环境，不进入仓库。索引中 `root` 必须是虚拟前缀下 `hg-<数字>`，`path` 必须是该目录下四段相对媒体路径（含 `Season 01`、` - S01E001.mp4`），对应本机文件 size/mtime 必须匹配。命中时仍需调用者 token 对该 item 的 PlaybackInfo 可见，并核对 `MediaSources` 中同一 `File` 类型、source ID 与路径；通过后才将 GET/HEAD、Range 等转发给本地 Emby。外层网关准入规则必须维持在本服务之前，此分支不能代替外层准入。

未配置或非法的公网 host **不会**从注册表 URL 推断允许主机。部署主机必须显式设置 `CMCC_OPENLIST_PUBLIC_HOST` 才能签发云盘 302。

Emby / OpenList / Deck 仍走 loopback 默认：`127.0.0.1:8096`、`127.0.0.1:5244`、`127.0.0.1:8300`。

## 本轮改动

未变文件不再每次 `json.loads` 注册表。缓存键为 inode + mtime_ns + size：

- 未变：只解析一次
- 原子替换 / 原地修改 / 删除：立即失效
- 并发：锁内只加载一次
- 读失败、JSON 损坏、非 object：清空缓存，不复用旧授权/旧来源

不缓存 OpenList/网盘最终直链。未登记 edition 仍转发 Deck；`/cmcc-source/` 与 edition 302 仍要求能力 hash，302 允许后缀仍为 `.cmecloud.cn` / `.139.com` / `.10086.cn`。

## 独立部署（主控执行）

1. 将本文件随 tag 检出到运行目录。
2. 在服务环境中设置 `CMCC_GATEWAY_REGISTRY` 与 `CMCC_OPENLIST_PUBLIC_HOST`（值不进仓库）。
3. 重启网关服务（loopback 8339）。不要重启 Emby / rclone 挂载 / 播放节点。
4. 校验：`/health` 返回 `ok`；未知 `MediaSourceId` 仍转发 Deck；已登记 CMCC 路径仍要求 `cap`；未设公网 host 时 resolve 502。
5. 回滚：checkout 上一 tag 并再重启同一 unit。

## 测试

```
python3 deploy/gateway/test_registry_cache.py
python3 deploy/gateway/test_duanju_local.py
python3 backend/tests/test_cmcc_gateway_registry.py
```
