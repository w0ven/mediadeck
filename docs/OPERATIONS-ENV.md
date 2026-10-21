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

## systemd drop-in 示例

```ini
[Service]
Environment="MEDIADECK_EMBY_LOG=/var/lib/emby/logs/embyserver.txt"
Environment='MEDIADECK_PREWARM_MOUNTS={"/mnt/media-a/Media": "/var/cache/rclone/media-a/vfs/Media"}'
```

JSON 值里有双引号，所以整条赋值用单引号包起来。值里不要出现 `%`；必须用时写成 `%%`，这是 systemd 的转义规则。
