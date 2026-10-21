# 字节流量统计退役

本版本不再统计字节流量。源码变化不等于已停止现网代理；部署、重启、nginx/nft/生产配置修改必须另行批准。

## 行为

- 不再按播放码率估算流量，不累计用户用量、节点日志账本或内核计量账本；不再按月清零、重置额度、赠送流量或按额度耗尽封禁。
- 删除 Bot/管理员账号、等级、统计、节点、实时播放中的流量用量/实测速率展示及相关操作。组/等级旧配额字段留作兼容档案，不参与权益。旧 `both` 仍保留时间到期语义，旧 `traffic` 不凭旧到期列新增时间限制。
- `UsageSampler` **继续运行**：进度核验、观影时长、签到资格与积分成长、奖励结算、播放记录和并发安全不变。新播放记录的旧 `bytes` 列仅填兼容零值，API 不把它展示成一次实测；历史非零值不清空。
- `stream_rate_proxy.py` 不变：签名中的有效速率预算、账号级共享限速、Range/HEAD、回源故障语义继续保留。限速器为传输节奏使用的瞬时字节预算不是累计用量，也不会上报。
- `loadprobe.py` 只提供连接容量、媒体可用性及签名校验；不读网卡 TX/socket byte counters，不尾随 nginx speed 日志。调度按并发容量及健康/亲和策略，不再按观测出口 Mbps 决策。

## 协议与安全

- 旧 `POST /api/edge/{node}/report`：保持节点鉴权，HTTP 200 + `retired:true, accepted:false`，不解析/存储日志，不推进游标。
- 旧 `POST /api/edge/{node}/measured`：保持节点鉴权与节点名校验，HTTP 200 + `retired:true, accepted:false`，回显 `ack.boot_id/seq` 防止旧端因协议错误无限重试；不记 envelope、watermark、usage、credit 或 policy ack。合法 JSON 是旧协议前提。
- `/cursors` 返回退役空列表，不清除历史游标。`/api/metering` 返回退役状态，不返回当前计量；重新启用、重归属及计量代理下载返回 410。流量重置/赠送不会写库。
- 新 `GET /api/edge/{node}/policy` 使用同一节点凭据，返回权威访问拒绝集合，仅含手动禁用、待开通、时间到期，不含流量耗尽。保留旧 `/measured` 响应中的同一访问策略供过渡客户端使用。
- `meterd.py` 保留部署名称/端口，但已是**纯访问策略守卫**。保留签名之后的注册、同一连接身份混用拒绝、禁用断流及 nginx deny map；只持久化安全身份与拒绝状态。它不调用 nft、不写流量 DB、不上报连接字节。
- 第一次升级可用旧 `--persist` 路径**只读**导入尚未关闭的连接身份；旧 DB/待发送队列原样留存。新的安全状态写入独立 `access-policy.json`。旧 `--enable-nft` 参数只接受、不启用计数。
- 私有签名配置中的旧 `metering` 键兼容保留，仅表示签名通过之后是否访问策略守卫，不再启用统计。不能为了停流量而关闭 HMAC 校验、访问拒绝集合、限速或同播检查。
- 旧 `edgereport.py` 启动器不再读日志/凭据或发网络请求。`--once` 立即退出；旧 Restart=always 服务启动时保持空闲以免重启风暴，应在批准部署时停用该服务。

## 历史保存

不新增清空迁移，不删历史流量表、记录或日志。旧 `measured_*`、`meter_*`、`edge_*` 表保持不变，旧布局的计量 watermark/policy-ack 重排迁移也停止调用。月度归零任务移除。含历史 bytes 的 `usage_daily` / `play_events` 暂不按保留期删除；独立观影证据/审计仍按原有保留规则处理。

## 经批准部署时必须完成

1. 备份代码与生产配置，在当前实际 HEAD/未提交覆盖之上合并此改动，不能覆盖并行会话成果。保留历史数据库/计量 spool/日志文件。
2. 升级面板，确认旧上报不写库且保留观影采样和奖励任务。停止/禁用各节点旧 `mediadeck-edgereport.service` 及另行设置的同类 timer/cron。
3. **替换而不是简单停掉**旧 `mediadeck-meterd.service` 为本版本守卫，升级全部 `loadprobe.py` 实例（包括单独的旧域名签名 verifier）。新服务仍需节点凭据与签名配置，不能通过删凭据停报。
4. 切换过程中保留旧 deny map，直到成功获取新的权威访问策略。确认旧额度耗尽拒绝已解除，而手动禁用/待开通/到期拒绝仍有效；只针对可确认的旧自动耗尽策略做 Emby 对账，不批量解除人工封禁。
5. 从实际 nginx 媒体 location 移除 speed access_log、announce mirror；从节点、透明入口/源站及 dispatch 日志格式移除字节字段。保留必要非字节安全日志、签名、deny map、限速中继、连接限制和回源/Range 行为；先语法检查再经批准 reload。
6. 停止旧 meterd 后，单独核对并移除其专用 `inet mediadeck_meter` **运行时计数表**。仅换 Python 不会停止已存在的内核计数规则；不能删除其他 nft/防火墙表。该操作不是删除历史数据库，仍需生产变更授权。
7. 有远程透明代理/节点时必须同步处理其代理脚本和字节日志，不能仅更新面板后宣布全链路停止。检查上报请求停止、历史计量行不增长、内核计数表消失、观影/奖励继续、有效/篡改/封禁链接和并发/共享限速正常。

## 隔离验证

`tests/test_traffic_retirement.py` 是退役契约回归；配合更新后的 `test_nginx_meter_auth.py`、`test_signing_v2_nginx.py`（真实 TLS/nginx、独立 netns）、`test_aggregate_rate_release.py`、观影/奖励/签到/并发系列及 Chromium 会员页面测试。

历史计量/速率算法的旧测试代表已删除功能，不是本版本仍应提供的计费契约；本次只运行交付报告中列明的针对性集合，不声称全仓测试通过。
