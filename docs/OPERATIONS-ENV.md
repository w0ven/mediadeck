# 运维脚本的主机环境变量

`backend/tools/` 下的运维脚本不再内置任何主机布局。路径、挂载点、systemd unit
和 SSH 别名都从环境变量读取；仓库里的默认值只是中性示例，不对应任何真实部署。
真实取值只放在部署主机上（systemd drop-in 或该服务自己的环境文件），不进 git。

未设置或为空时使用默认值。值格式不对时，脚本直接报错，不会悄悄退回默认值；
`mediadeck_cloud_dl.py` 例外：配置错误只让云下载那一节显示不可用，不影响同一快照里的其他来源。

## `prewarm_head.py`

| 变量 | 格式 | 默认值（示例） |
| --- | --- | --- |
| `MEDIADECK_EMBY_LOG` | 路径 | `/var/lib/emby/logs/embyserver.txt` |
| `MEDIADECK_DISPATCH_LOG` | 路径 | `/var/log/nginx/mediadeck-dispatch.log` |
| `MEDIADECK_EMBY_LIBRARY_DB` | 路径 | `/var/lib/emby/data/library.db` |
| `MEDIADECK_EMBY_PATH_PREFIXES` | 逗号分隔的 Emby 路径前缀 | `/media/` |
| `MEDIADECK_PREWARM_MOUNTS` | JSON 对象：挂载点 → 该挂载的 rclone VFS 缓存目录 | 见下 |

`MEDIADECK_PREWARM_MOUNTS` 的键同时也是预热白名单，候选路径按键的顺序生成：

```json
{"/mnt/media-a/Media": "/var/cache/rclone/media-a/vfs/Media",
 "/mnt/media-b/Media": "/var/cache/rclone/media-b/vfs"}
```

`--log`、`--dispatch-log`、`--library-db` 的默认值取自上表。`--settings`
默认是相对脚本的 `backend/data/settings.json`；如果脚本不在 checkout 里运行，就显式传入 `--settings`。

## `collect-status.py`

`MEDIADECK_STATUS_MOUNTS`：JSON 对象，`标签 → [挂载路径, systemd unit]`。

```json
{"media-main": ["/mnt/media-main", "rclone-media-main.service"],
 "media-union": ["/mnt/media-union-example", "media-union-example.service"]}
```

状态目录 `/var/lib/mediadeck` 不变。

## `mediadeck-intake-collector.py`

`MEDIADECK_INTAKE_PATHS`：JSON 对象，按键覆盖默认路径，未给出的键保留默认值。
只接受下列键：`refresh_queue_dir`、`refresh_sent_dir`、`refresh_suppress_file`、
`notify_pending_dir`、`notify_log`、`upload_lane_root`、`staging_dir`、
`local_fallback_dir`、`quarantine_dir`、`upload_state_dir`。

```json
{"staging_dir": "/srv/intake/staging", "notify_log": "/var/log/mediadeck-intake/notify.log"}
```

输出文件 `/var/lib/mediadeck/intake-backend-snapshot.json` 不变。

## `mediadeck_cloud_dl.py`

`MEDIADECK_CLOUD_DL_PATHS`：JSON 对象，按键覆盖默认值。所有值必须是非空字符串。

| 键 | 含义 | 默认值（示例） |
| --- | --- | --- |
| `host` | 后端已有的 SSH 别名，也是快照里的来源 host | `cloud-dl` |
| `state_root` | 远端状态目录（以 `/` 结尾），包含 `ownership.json`、`backlog.json` | `/var/lib/cloud-dl/state/` |
| `owner_mode` | `ownership.json` 里现役 owner 的 `mode` | `primary` |
| `progress_file` | `state_root` 下的进度文件名 | `primary-progress.json` |
| `jobs_dir` | 远端任务目录 | `/var/lib/cloud-dl/jobs` |
| `gd_pending_dir` | 远端待上传事件目录 | `/var/lib/cloud-dl/upload-events/pending` |
| `gd_done_dir` | 远端已上传事件目录 | `/var/lib/cloud-dl/upload-events/done` |

`host` 只能是字母、数字、`.`、`_`、`-`，且不能以 `-` 开头。其余值作为一个 JSON 字面量嵌进远端只读脚本，
再整体做 shell 转义后发送，不会被拼成 shell 或 Python 代码。

## 游戏中心棋类机器人（后端服务）

这四项是**可选**引擎配置，不属于运维脚本的必填参数。未设置、文件缺失、启动/协议失败或超时均回退内置机器人，练习场仍可用；不影响真人积分场。不要把二进制或网络权重提交进仓库。

| 变量 | 格式 | 默认行为 |
| --- | --- | --- |
| `MEDIADECK_PIKAFISH_BIN` | Pikafish 可执行程序的绝对路径 | 未配置则象棋回退机器人 |
| `MEDIADECK_PIKAFISH_NNUE` | 对应 NNUE 权重的绝对路径 | 未配置则象棋回退机器人 |
| `MEDIADECK_RAPFI_BIN` | Rapfi/piskvork 可执行程序的绝对路径 | 未配置则五子棋启发式机器人 |
| `MEDIADECK_RAPFI_DIR` | Rapfi 配置和网络所在工作目录的绝对路径 | 未配置则五子棋启发式机器人 |

Rapfi 目录须包含发行版 `config.toml`、`model210901.bin` 以及 freestyle 网络（本次验证的发行版使用 `mix9svqfreestyle_bsmix.bin.lz4`）。目录及父目录必须可被引擎运行账户遍历，程序可执行、配置及权重可读；应由管理员持有且不允许引擎账户写入。不要开启引擎数据库或自动日志。推荐独立只读目录，不使用应用的数据/密钥目录。

- root 后端启动 Rapfi/Pikafish 前通过 subprocess 的 `user/group/extra_groups` 降为 `nobody:nogroup`，不保留附加组；Rapfi 再由隔离启动器施加资源限制并 exec。非 root 保持当前低权限身份。Pikafish 使用 1 个搜索线程和最多 16 MiB hash，神经网络加载后的进程峰值内存约 313 MiB，Rapfi 约 65 MiB。
- Rapfi 单搜索线程，32 MiB hash，512 MiB 地址空间硬限额、2 秒 CPU 限额、禁 core/文件写入，协议输出上限 64 KiB。整次调用墙钟上限 1.2 秒，结束或超时均强杀进程组并回收；内置回退不启动子进程。
- 已验证的 250615 发行程序还会创建闲置调参线程池，因此 OS 线程总数不等于搜索线程数（本机为 18：主线程、1 个搜索线程、16 个闲置线程）；CPU 采样确认仅 1 个线程执行搜索。该发行程序偶发启动失败时立即回退，不重试扩大单次时间预算。
- 入门/普通/困难的 Rapfi 搜索预算为 60/200/650 毫秒；入门多数回合使用较弱的局部随机策略，普通和困难优先使用引擎。回退先成五、堵对方成五，再看活三/邻近点。
- **全局合计最多 2 个外部引擎进程**：Rapfi 和 Pikafish 共用 `board_engine_pool` 的进程级信号量，不是各自两个；跨牌桌、跨 GameCenter 实例和直接机器人调用都共享。必须在 Popen 前取许可，终止并回收进程后才释放。继续遵守本项目单应用 worker 的部署约束，不支持通过增加 worker 绕过此全局内存预算。
- 机器人由独立线程排队，不在事件循环或 SQLite 事务内等待。桌层从任务入队即开始计 **1.5 秒总预算**，包括执行器排队、全局许可排队和引擎思考；引擎自身更短的上限仍有效（Rapfi 1.2 秒、Pikafish 独立调用 3 秒）。队列超时不启动进程；正在运行的调用到期强杀并回收，转用本地合法走法。tick 在 1.5 秒仍未收到结果也立即回退并取消未执行任务，防止旧任务稍后补启动。
- 机器人回合继续体现在 current_player/棋钟中，正常 0.5 秒 tick 下约 0.5–2 秒落子。排队不暂停或重置棋钟，也不阻塞真人桌；已结束/悔棋局面的旧结果不会落入新局面。
- 象棋同样通过独立工作线程调用 `xiangqi_robot.robot_move`，合法着法仍由本地规则引擎核验。规则引擎属于应用代码，不是这四个可选环境变量；缺少规则模块时大厅 `xiangqi.available=false`，不允许新开象棋桌。

已核对的发行版（安装前用 sha256 校验；GitHub 发行页公布的资产 digest 与下表一致）：

| 引擎 | 来源 | 校验 |
| --- | --- | --- |
| Pikafish 2026-09-06 | `https://github.com/official-pikafish/Pikafish/releases/download/Pikafish-2026-09-06/Pikafish.2026-09-06.7z` | 压缩包 `41952bbfe2520faceb5902c69e6ab4845cc999841d2b49a95cc1be7867a25e5b`；`Pikafish-Linux-x86-64-universal` `727828293830de8082a80b224bf47b32700c2d1a441908ea11b3c9197178c20c`；`pikafish.nnue` `7d13d73569a9b571ba0eb20cf1596247bc2a42738967e61afef6482b231e900e` |
| Rapfi 250615 | `https://github.com/dotb1ue/rapfi/releases/download/250615/Rapfi-engine.7z`（作者原 `dhbloo` 账号改名，仓库编号不变） | 压缩包 `1a3e24024062a153ac079060ee9589a37c6bdd1ecc54fed3908793c519594e05` |

许可证：两个引擎程序都是 GPL-3.0，只作为独立进程在服务器上运行、不分发，不影响本项目代码。Rapfi 网络权重为 CC0。Pikafish 的 `pikafish.nnue` 另有 NNUE-License，**未经许可不得商业使用**；运营方已确认本服务不属于商业用途后才启用。服务性质变化时须重新评估，或换用 CC0 权重的引擎。

示例（路径仅为中性部署布局）：

```ini
[Service]
Environment="MEDIADECK_PIKAFISH_BIN=/srv/board-engines/pikafish/pikafish"
Environment="MEDIADECK_PIKAFISH_NNUE=/srv/board-engines/pikafish/pikafish.nnue"
Environment="MEDIADECK_RAPFI_BIN=/srv/board-engines/rapfi/pbrain-rapfi"
Environment="MEDIADECK_RAPFI_DIR=/srv/board-engines/rapfi"
```

以上配置不会自动开启游戏。仍需在 `game_center` 插件启用对应游戏，并按需要调整场次 JSON 和双方总时长/每步时长；旧桌沿用创建时冻结的参数。

## systemd drop-in 示例

```ini
[Service]
Environment="MEDIADECK_EMBY_LOG=/var/lib/emby/logs/embyserver.txt"
Environment='MEDIADECK_PREWARM_MOUNTS={"/mnt/media-a/Media": "/var/cache/rclone/media-a/vfs/Media"}'
```

JSON 值里有双引号，所以整条赋值用单引号包起来。值里不要出现 `%`；必须用时写成 `%%`，这是 systemd 的转义规则。
