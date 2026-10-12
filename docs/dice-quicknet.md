# 骰局 quicknet v1

仅 `dice-size-banker-quicknet-v1` 使用 `dice-draw-quicknet-v1`。旧七池、无庄大小及
`dice-size-banker-v1` 的随机来源、截止时间、轮次、seed算法和超时均不迁移。

## 官方输入与验签

2026-10-13读取：
- https://api.drand.sh/v2/beacons/quicknet/info
- https://api.drand.sh/52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971/public/1000000
- https://github.com/drand/drand/blob/master/crypto/schemes.go ：`NewPedersenBLSUnchainedG1`

两个原始JSON保存在 `backend/tests/fixtures/drand_quicknet_*.json`，离线验签通过。
固定链为 `52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971`；
genesis=1692803367、period=3秒；公钥完整值见上述info fixture和源码常量。
不是旧的G2Basic：scheme=`bls-unchained-g1-rfc9380`，公钥G2（96字节）、签名G1（48字节）。
消息为SHA256(uint64大端轮次)，RFC9380 hash-to-G1，DST为
`BLS_SIG_BLS12381G1_XMD:SHA-256_SSWU_RO_NUL_`。验证解压编码、非无穷点、子群、配对等式及
randomness=SHA256(signature)。固定公钥验证即绑定链；响应若带chain_hash还须匹配。
不接收旧链签名、不切链、不使用本地随机数兜底。沿用已pin的py-ecc==8.0.0，无新增依赖。

## 开局承诺与封盘选轮

开局secret=32个安全随机字节的hex，nonce=12字节随机hex；公布
commitment=SHA256(bytes.fromhex(secret))及选轮规则。开局round_number=0表示尚未选轮，不能开奖。

满池最后一笔下注与封盘共用资金事务；计时局在首次实际执行封盘的事务中取当前时间，
不能使用已过去的deadline选轮。所有下注仍在deadline拒绝，不因worker迟到延长投注。
在同一事务一次固化closed_at、round_number和timeout，后续重放/重启不重选：

```
r = max(1, floor((closed_at + 2 - genesis) / 3) + 2)
publication_time = genesis + (r - 1) * 3
timeout = closed_at + 600
```

选严格晚于closed_at+2秒的第一轮，理论等候为(2,5]秒，另加信标传播、验签和Telegram耗时。
2秒是固定的时钟安全余量；部署前必须确认宿主机时间同步，不能保证任意时钟失准情况下
仍选到未来轮次。操作方/玩家能影响封盘时机，但不能事先知道所选的未来公共随机值。
网络异常只重试同一轮；封盘后600秒仍未固定有效结果则退款，已fixed永不超时重摇/退款。
开局timeout暂存deadline+600（旧NOT NULL列），封盘时才写入生效的closed_at+600。

## seed及骰面

```
seed_bytes = SHA256(ASCII(compact_JSON([
  "dice-draw-quicknet-v1", chain_hash, round_number, secret_hex, nonce, randomness_hex
])))
```

JSON无空格、ASCII转义；seed存hex。counter从0开始，逐块
SHA256(seed_bytes + uint64大端counter)。逐字节拒绝252～255，其余`byte % 6 + 1`，
取前三个骰面。没有连大/连小补偿，不按账本、庄家或输赢调结果。
最终同条图文战报的折叠验真块提供局号、封盘时刻、承诺、揭示种子和所选信标公开URL，
可重新验签、核对承诺及重算骰面。

## 调度、恢复与部署

每局独立任务等待deadline/所选轮次；满池指令唤醒该局，不等全局worker的5秒轮询。
共享worker只负责恢复新局任务，旧局继续原逻辑。每局任务去重，资金幂等及发送outbox租约保留。
验签在线程运行，HTTP不占资金事务；caption编辑独立，不拖延信标唤醒。
失败按1、2、4、8、16、30秒上限退避，持久化到DB，429遵守Retry-After；无有效结果600秒退款。

启动迁移仅向dice_rounds追加closed_at（nullable）、beacon_due和beacon_attempts（默认0），
不改历史rules/数据、不改图片资产/配置默认。部署需常规DB备份、重启加载代码；
回滚到旧程序前须等待所有新版本局及待发送战报完成，不能让不认识新版本的旧程序接管活跃新局。
