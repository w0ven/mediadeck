# Vendored browser libraries

## ThumbmarkJS

| 项目 | 值 |
| --- | --- |
| 包 | npm `@thumbmarkjs/thumbmarkjs` |
| 版本 | **1.12.0**（锁定；升级须重新审查下述联网路径并更新 sha256） |
| 上游 | https://github.com/thumbmarkjs/thumbmarkjs （gitHead `7d845343492b5afde9c013cf0cfff1299067d798`） |
| 许可证 | MIT（`package.json` `license: "MIT"`，全文见同目录 `LICENSE`，Copyright (c) 2024 Ilkka） |
| tarball | `https://registry.npmjs.org/@thumbmarkjs/thumbmarkjs/-/thumbmarkjs-1.12.0.tgz` |
| tarball 校验 | npm integrity `sha512-qGIKlSKhsH7CtazU8dhowBDNo1l1DeAr30K785nCtFdzSWam/sybyjiPmmKu/jUjkuUJ8kr8VyRXiWQxQm/81w==`（本地复算一致）；sha256 `7d814967dd27bf154abc157cec586c31e0df69cf1db317a2069f2b7d19d9aeee` |

本目录只复制了 tarball 中的两个文件，内容未作任何修改：

| 文件 | 来源路径 | sha256 |
| --- | --- | --- |
| `thumbmark.umd.js` | `package/dist/thumbmark.umd.js` | `d13e5e67dd63a313a6d40cd925f2506399132a5cff67545da188db1f607a16cb` |
| `LICENSE` | `package/LICENSE` | `8572d5013f16d29f05d8ca1ba07828e551dec02d5aab39a7f620aa83c02e17ba` |

UMD 构建无运行时依赖，加载后只定义全局 `window.ThumbmarkJS`，不执行任何采集。

### 联网审查（1.12.0 源码 `src/`）

库内能发起网络请求的路径只有以下几条，`../ux.js` 的调用方式全部避开：

1. 付费 API（`functions/api.ts`，`api.thumbmarkjs.com`）：仅当传入 `api_key` 或 `simple_request` 时调用。`ux.js` 两者都不传。
2. collect beacon（`utils/collect.ts`，`sendBeacon`/`fetch`）：仅在 API 响应带 `collect` 指令时触发，没有 API 调用就不会发生；`ux.js` 另外显式设置 `collect_beacon: false`。
3. 抽样遥测（`utils/log.ts`）：默认 `logging: true`，按 0.01% 概率向日志端点上报，并会拉取、执行 `experimental.thumbmarkjs.com/experimental.min.js`。`ux.js` 显式设置 **`logging: false`**（短路后也不会读写 `sessionStorage`）。
4. WebRTC 组件（`components/webrtc`，`iceServers: []`）：`ux.js` 通过 `exclude` 不运行它（`resolveClientComponents` 在派发前过滤）；`math`、`permissions`、`plugins`、`speech` 同样不运行。

`backend/tests/test_lottery_device_browser.py` 在 Chromium 中验证：加载与采集全过程没有对这三个本地文件以外的任何请求，也没有 `fetch`/XHR/`sendBeacon`/WebSocket/`RTCPeerConnection` 调用。

### 升级步骤

在独立临时目录 `npm pack @thumbmarkjs/thumbmarkjs@<版本>`，核对 integrity 与许可证，重新审查上述路径和组件输出结构（`../ux.js` 依赖 `canvas`、`webgl`、`audio`、`fonts`、`hardware`、`screen`、`system`、`locales` 的字段名），只复制 `dist/thumbmark.umd.js` 与 `LICENSE`，更新本文件的版本和 sha256，并跑设备校验相关测试。
