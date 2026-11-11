# dsh-wake-relay · 外部唤醒

**English: [README.en.md](./README.en.md)**

让**任意外部进程**唤醒一个 DeepSeek Harness 会话：构建监听、任务收尾、CI 回调、抓取完成、备份完成、手机推送转发、你自己的 watcher——任何能发一个 HTTP POST 的东西。

一句话说清它和同类插件的分工：**别人解决「agent 睡着了怎么叫醒它」，这个解决「叫醒它的那根线断了怎么办」。**

---

## 为什么

DSH 的原生 `schedule` 和市面上的唤醒插件都把计时器/探针放在**宿主进程里**：宿主退出，探测停、投递停，那段时间里发生又消失的变化永久丢失。

这个插件反过来——**判断在宿主外面，投递在宿主里面**：

```
外部进程（你自己的脚本 / 自带 watcher）
  └─ 每轮探测，条件成立 → POST 127.0.0.1/wake ──┐
                                                 │  宿主在线：立即注入并 flush
                                                 │  宿主离线：连接失败，事件留在你的队列里
                                                 └→ 宿主回来：同一个 event_id 补投
```

关键是这条约定：**HTTP 200 = 已投递并 flush；503 = 什么都没记录，可以用同一个 `event_id` 重试。** 投递从不"大概成功"，所以重试权可以完整地交给调用方，去重和幂等由插件这一侧兜住。

代价是：你得有一个常驻的外部进程（自带 `watcher/` 里的规则引擎，或者你自己的脚本）。

---

## 与同类项目的差异

| | 本插件 | 宿主内探针类（dsh-sentinel 等） |
|---|---|---|
| 探针在哪跑 | 外部进程 | 宿主进程内 |
| 宿主退出期间 | **照常采样，事件排队等重试** | 不采样，窗口内变化丢失 |
| 判断能力 | 任意命令退出码 + JSON 字段断言 | 固定几类 sensor，多数只判「变了没」 |
| 失败语义 | 503 + 不记账，重试幂等 | at-least-once / 由插件内部重排 |
| UI | 无（`GET /status` + 日志） | dock 面板 / 侧栏 dashboard |
| 多实例 | 撞端口自动退到随机端口 | lease 选主 |
| Windows 进程探针 | 原生 `OpenProcess` | 多为 `pgrep`（Windows 上不可用） |

UI 是有意的取舍：这个插件的读者是**脚本**，不是人的眼睛。

---

## 安装

**桌面版的 `desktop` profile 由 Electron 独占，命令行装不了**——`dsh plugin --profile desktop add …` 会直接报错：

```
error: profile "desktop" is managed exclusively by the Electron application
```

所以分两种路径。

**① 让 AI 装（桌面版 / Web 版都适用，推荐）**

把这段粘给你的 DSH，它会自己装好并验证：

````text
帮我装 dsh-wake-relay —— npm 上的 DSH 外部唤醒桥。照它 README 的「安装」那节来，别自己发明步骤。

装好只回三行：profile 名 / 实际端口 / GET /health 的返回。

判据只有一条：$DSH_HOME/wake-bridge.log 里多了一行 listening 才算装上。install_bundle 返回 applied 不算——它没加载也这么说。
````

**② 命令行（`dsh web` 这类非 Electron 管理的 profile）**

```bash
dsh plugin --profile web add dsh-wake-relay
```

从本地目录装：

```bash
git clone https://github.com/tendayst/dsh-wake-relay
dsh plugin --profile web add ./dsh-wake-relay
```

装完通常**即时生效**（`plugin_manager` 会动态 apply，不用重启）。判断标准只有一条：`wake-bridge.log` 里有没有新的 `listening` 行。没有就重启一次 DSH。

> **不要同时装第二个唤醒插件。** 所有实现都写同一个 `$DSH_HOME/wake-endpoint.json`。两个同时跑会抢端口（后启动的那个退到随机端口），而停止时写的 `port: null` 会让另一个的发现文件失效——0.1.2 起本插件只在文件仍指向自己时才清空，但端口之争依然存在。

### 两个装的时候才会遇到的陷阱

**① bundle 的 entry id 必须全局唯一。** 启用/禁用状态是按 entry id 持久化的，写在 profile 的 `cordis.patch.yml` 里：

```yaml
- id: wake-bridge
  disabled: true
```

如果另一个 bundle 的 patch 也 `insert` 同名 id，它会被这条 disabled **静默吞掉**——不报错、不告警，只在日志里留个空。判断装没装上的唯一可靠方法是看运行时产物：`wake-endpoint.json` 里有没有新端口、`wake-bridge.log` 有没有新的 `listening` 行、`wake-bridge-secret.json` 有没有生成。`install_bundle` 的返回永远是 `applied`，它不代表加载成功。

**② `sessionId` 留空是合法配置，但装完不能直接 curl。**留空时请求必须自带 `session`，否则返回 `503 no_session_bound`。启动日志会明说：

```
listening on 127.0.0.1:45871 — NO default session bound: every /wake request must carry "session"
```

要么在 `cordis.patch.yml` 里配死 `sessionId`，要么每次请求都带 `session`。

> 本地目录安装的另一个已知坑：`install_bundle` 传目录落的是 `pnpm link:`，插件从 workspace 真实路径解析不到宿主包 → `ERR_MODULE_NOT_FOUND`。修法是在包目录里建 junction：
> ```
> mklink /J "…\dsh-wake-relay\node_modules\@deepseek-ai\dsh-llm" "C:\Users\<你>\.dsh\profiles\node_modules\@deepseek-ai\dsh-llm"
> ```
> 安装失败后 `install_bundle` 只会报 `already-installed`，要 `set_plugin` 把 `include:<id>` 关掉再打开才能强制重新 import。

---

## 配置

写进 profile 的 `cordis.patch.yml`（bundle 行的 `config` 段），或本包的 `cordis.patch.yml`：

| 字段 | 默认 | 说明 |
|---|---:|---|
| `port` | 45871 | 监听端口，只绑 `127.0.0.1`；被占用时自动退到随机端口并重写 endpoint 文件 |
| `token` | 空 | 空 = 首次启动生成并持久化到 `$DSH_HOME/wake-bridge-secret.json`（0600） |
| `sessionId` | 空 | 默认目标会话。空 = 只接受带显式 `session` 的请求 |
| `cooldownMs` | 90000 | **per-reason** 最小间隔 |
| `reasonWindowMs` / `reasonMaxHits` | 300000 / 2 | 同一 reason 在这个窗口内最多触发几次 |
| `globalBurstMs` | 3000 | 任意两次唤醒的最小间隔，只压同时爆发 |
| `flushTimeoutMs` | 10000 | 单次投递的上限，超时回 503 |
| `dedupeWindowMs` | 604800000 | `event_id` 去重窗口（7 天） |
| `template` | 空 | 消息模板，支持 `{{detail}}` `{{reason}}` `{{data.k}}` 等 |
| `footnote` | 空 | 追加在消息末尾的一行 |
| `watcherStatePath` | 空 | 外部 watcher 的 state 文件，`GET /status` 会折叠它 |

模板变量：`{{event_id}} {{kind}} {{reason}} {{detail}} {{context_path}} {{session}} {{time}}`，以及任意 `{{data.<路径>}}`。未知变量原样保留。

---

## API

```
GET  /health                 公开（无 token）→ {ok, sessionId, inflight, seen, lastWakeAt}
GET  /status                 (token) → 计数器 + reason 命中窗口 + 外部 watcher 的队列摘要
POST /wake     (x-wake-token) → {accepted, delivered, messageId, sessionId}
POST /bind     (x-wake-token) → {accepted, sessionId}   换默认绑定（不持久，重启回配置）
```

`POST /wake` 请求体：

```json
{
  "event_id": "build-2026-11-12-7f3a",
  "kind": "signal",
  "reason": "BUILD_DONE",
  "detail": "17 个用例通过，0 失败",
  "data": { "任何": "结构化字段，模板里用 {{data.任何}} 取" },
  "context_path": "C:\\path\\to\\state.json",
  "session": "session-…（可选，覆盖默认绑定）",
  "text": "（可选）整条消息，替代模板，2000 字以内"
}
```

| 状态 | 含义 | 调用方 |
|---|---|---|
| 200 | 已投递并 flush | 完成 |
| 429 `duplicate` / `cooldown` / `burst` | 按策略丢弃 | **不要重试** |
| 503 `delivery_failed` / `session_not_found` / `no_session_bound` | 什么都没记录 | 用**同一个 event_id** 重试 |
| 401 / 400 | token 错 / 请求格式错 | 修调用方 |

> 中文 body 必须按 UTF-8 字节发。PowerShell 用 `[System.Text.Encoding]::UTF8.GetBytes($body)`，否则 `detail` 会变成一串 `?`。

### Node 客户端

```js
import { wake } from "dsh-wake-relay/client";

const r = await wake({ event_id: `job-${Date.now()}`, reason: "JOB_DONE", detail: "…" });
if (r.retryable) setTimeout(() => wake(sameEvent), 30_000); // 503 → 值得重试
```

### 任何语言

```bash
EP=$(cat ~/.dsh/wake-endpoint.json)   # {port, token, sessionId}
curl -s -X POST "http://127.0.0.1:$PORT/wake" \
  -H "x-wake-token: $TOKEN" -H "content-type: application/json; charset=utf-8" \
  --data-binary @event.json
```

---

## 自带的规则引擎（可选，Python）

`watcher/` 是一个常驻进程 + 一张规则表，替你把「每个场景写一个轮询脚本」收成「加一条规则」：

```bash
python watcher/waiter.py --rules watcher/rules.example.json --list
python watcher/waiter.py --rules my-rules.json --once --dry-run   # 只判断不发
pythonw watcher/waiter.py --rules my-rules.json                   # 常驻（改规则不用重启）
```

| 探针 | 成立条件 |
|---|---|
| `file_exists` / `file_contains` | 路径存在 / 内容匹配正则 |
| `pid` | 进程在/不在（Windows 原生 `OpenProcess`，*nix `kill(pid,0)`） |
| `port` | 端口开/关 |
| `http_json` | 取 JSON 字段后比较（`== != > >= < <= contains matches`） |
| `command` | 任意命令退出码符合；stdout 进 `{detail}` |

边沿触发（只在假→真那一瞬发一次）、`rearm_s` 防抖、`once` 一次性、持久化重试队列 + `ttl_s`、`session: latest` 自动选磁盘上最新的普通会话。规则表热重载。

状态目录：`$DSH_HOME/wake-bridge-watcher`（`waiter-state.json` / `waiter.log` / `waiter.pid`）。

---

## 运行时文件（`$DSH_HOME`）

| 文件 | 内容 |
|---|---|
| `wake-endpoint.json` | `{port, token, sessionId, pid, startedAt}`，脚本自动发现，不用手抄 |
| `wake-bridge-secret.json` | 自动生成的 token（0600） |
| `wake-bridge-events.jsonl` | 已接受的 fire 记录（append-only，7 天窗口，超限自动 compact） |
| `wake-bridge.log` | accept / reject / delivered / failed |
| `wake-bridge-watcher/` | 外部 watcher 的边沿状态 + 重试队列 |

---

## 已知边界

- **宿主必须在线才能投递**。宿主退出期间外部 watcher 只会攒事件（`ttl_s` 内），不会"预投"到磁盘再回放。
- **`/bind` 不持久**；重启回配置里的 `sessionId`。
- **token 是明文**（`wake-endpoint.json`）。只绑 `127.0.0.1`，别做端口转发；多用户机器上把这个文件权限收一收。
- **一次唤醒 = 一次完整推理 + 一条永久消息**。高频条件请写 state 文件，别叫醒；真要叫，一次把上下文带全（`state_path`）。
- **`session: latest` 是猜测**：它挑磁盘上最新的普通会话。长任务/无人值守必须显式钉 `session`，否则唤醒可能落进临时对话。
- 显式 `session` 解析失败返回 503，**绝不静默改投**。

---

## 自测

```bash
node selftest.mjs           # 21 条：投递、幂等、限流、重试、模板、跨重启去重、撞端口回退
python watcher/selftest.py  # 11 组：探针、边沿、重试队列、会话路由、规则校验（离线，不发真唤醒）
```

---

## License

MIT。
