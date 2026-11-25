---
name: wake-relay
description: 让外部进程在条件成立时叫醒当前会话（dsh-wake-relay，本机 loopback 端口）。触发场景：用户说「帮我盯着 X，有变化通知我」「等某个任务/回复/部署完了叫我」「后台监控某个 API 或仓库」「别挂着等，先干别的」「宿主重启了也别丢」；或你需要把控制权交回给人、但要求某个外部事件发生后继续。不适用于纯时间提醒（用 DSH 原生 schedule）和价格触发下单（用交易所条件单）。
---

# wake-relay：外部事件叫醒会话

外部进程探测条件，条件成立就投递一条消息进会话，起一轮 turn。**判断在宿主外面，投递在宿主里面** —— 这是它和 `schedule` 的唯一区别，也是最容易搞错的地方。

> 安装后把本文件复制到 `~/.dsh/skills/wake-relay/SKILL.md`，会话就能在遇到「等外部事件」时自己发现这个能力，不需要用户每次口述。

## 先判断该不该用它

| 需求 | 用什么 |
|---|---|
| 3 小时后 / 每天 9 点提醒 | **schedule**。持久化、停机补发、不占进程 |
| 价格穿过某位就下单/止损 | **交易所条件单**。延迟和可靠性都差三个数量级 |
| 等一个外部条件（回复、部署、任务完成、文件出现、端口变化） | **wake-relay** ✅ |
| 条件成立后需要判断再决定做什么 | **wake-relay**：规则只报「条件成立」，结论留给会话 |

核心差别在**宿主关着的那段时间**：`schedule` 到点注入，宿主没开着就丢了；wake-relay 的探针在外部进程里，照常采样，事件排队等宿主回来再投（`ttl_s` 内）。

## 三条接入路径

### ① 已经有外部进程 → 直接 POST

```bash
EP=$(cat ~/.dsh/wake-endpoint.json)          # {"port":…, "token":…, "sessionId":…}
curl -s -X POST "http://127.0.0.1:$PORT/wake" \
  -H "x-wake-token: $TOKEN" \
  -H "content-type: application/json; charset=utf-8" \
  --data-binary '{"event_id":"deploy-done-2026","reason":"DEPLOY_DONE","detail":"…","session":"<目标会话>"}'
```

中文必须走 UTF-8 字节。PowerShell 用 `[System.Text.Encoding]::UTF8.GetBytes($body)`，否则 `detail` 会变成一串 `?`。

### ② 需要轮询某个条件 → 往规则表加一条

规则表：`<repo>/watcher/rules.json`，**改完热重载，不重启**。

```json
{
  "id": "rbp-watch",
  "enabled": true,
  "interval_s": 300,
  "ttl_s": 259200,
  "probe": { "type": "http_json", "url": "https://api.example.com/status",
             "path": "state", "op": "==", "value": "done" },
  "fire": { "reason": "RBP_DONE", "kind": "info", "detail": "state 变成 done" },
  "session": "session-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
}
```

探针类型：`file_exists` / `file_contains` / `pid` / `port` / `http_json` / `command`（退出码 0 = 成立，stdout 进 `{detail}`）。

**`session` 必须显式写**，漏了会直接 raise（热重载保留旧表）。想投给「最近活跃的会话」写 `"latest"` —— 那代表你明确选了它。

### ③ 用客户端库

```python
from wakeclient import wake          # watcher/wakeclient.py
wake("BUILD_DONE", detail="17 passed", event_id="build-7f3a", session="<会话 id>")
```

```js
import { wake } from "dsh-wake-relay/client";
const r = await wake({ event_id: "job-1", reason: "JOB_DONE", detail: "…" });
if (r.retryable) setTimeout(() => wake(same), 30_000);
```

## 状态码就是全部契约

| 码 | 含义 | 调用方 |
|---|---|---|
| **200** | 已投递**并** flush（会话真收到了） | 完成 |
| **429** | 按策略丢弃（duplicate / cooldown / burst） | **别重试** |
| **503** | **什么都没记**（没去重、没限流、没日志之外的痕迹） | 用**同一个 `event_id`** 重试 |

503 的「什么都没记」是设计的支点：正因为它不留痕，重试才是纯安全操作，最多成功一次。这条语义不能改。

## 落完地怎么验

1. `wake-bridge.log` 里出现 `delivered … messageId=…` 才算通
2. `GET http://127.0.0.1:<port>/health` → `{"ok":true}`
3. `GET /status`（带 `x-wake-token`）→ 能看到规则引擎的 `rules/armed/pending`

## 排查顺序（都是真踩过的）

- **工具列表里找不到它不是故障。** 这个插件不注册任何 agent tool，只有 HTTP 端点。判断死活只看 `/health` 和日志，别看工具表。
- **`install_bundle` 返回 `applied` 不算装上。** 没加载也这么说。判据只有「日志里多了一行 `listening`」。
- **`desktop` profile 不能用 CLI 装**（`dsh plugin --profile desktop add` 会报 Electron 独占）。桌面版只能走插件管理器或 UI。
- **别同时装第二个唤醒插件。** 它们抢同一个 `wake-endpoint.json` 和同一个端口。
- **长等待必须显式写 `ttl_s`。** 宿主关着时事件只进 pending 队列，`defaults.ttl_s` 是 3600（1 小时）。超过窗口事件被丢弃，**而探针的基线已经走过那条变化，之后不会再触发——永久漏掉**。跨夜/跨周末写 `"ttl_s": 259200`（72 小时）。
- **探针静默失效比丢事件更严重。** `command` 探针的退出码 1 同时表示「没变化」和「探测失败」。自己写探针时，失败要留个可被下次运行发现的痕迹，否则监控瞎掉却没人知道。
- **`wakeclient` 每次投递前重读 endpoint 文件**，所以换端口/换插件不用改调用方。
- **规则引擎是独立进程**，宿主重启不影响它；但它自己需要开机自启，否则重启后监控就断了。

## 事实速查

| 项 | 值 |
|---|---|
| 端点 | `http://127.0.0.1:45871`（端口被占会退到随机端口，看 endpoint 文件） |
| 发现文件 | `$DSH_HOME/wake-endpoint.json` → `{port, token, sessionId, pid}` |
| 日志 | `$DSH_HOME/wake-bridge.log` |
| 规则表 | `<repo>/watcher/rules.json` |
| 规则引擎 | `<repo>/watcher/waiter.py`（常驻，热重载规则） |
| 客户端 | `<repo>/watcher/wakeclient.py`、`dsh-wake-relay/client`（Node） |
| 源码 | `github.com/tendayst/dsh-wake-relay`（npm `dsh-wake-relay`，MIT） |
