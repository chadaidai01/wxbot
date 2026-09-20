# HDSI Python 移植规范（hdsi/）

本包是 Gitee `MomoiCore/hds-interlude` master（1.0.1-beta6-rebuild）的 Python 一比一移植。
参考源码在工作区 `.hdsi_reference/src/**`（与上游逐文件对应）。

## 文件映射

| 上游 | 本包 |
| --- | --- |
| `src/types.ts` | `hdsi/types.py` |
| `src/time.ts` | `hdsi/time_utils.py` |
| `src/logging.ts` | `hdsi/logging.py` |
| `src/meta.ts` | `hdsi/meta.py` |
| `src/story-state.ts` | `hdsi/story_state.py` |
| `src/turn-persistence.ts` | `hdsi/turn_persistence.py` |
| `src/database.ts` | `hdsi/database.py` + `hdsi/store.py` |
| `src/model-routing.ts` | `hdsi/model_routing.py` |
| `src/group-willingness.ts` | `hdsi/group_willingness.py` |
| `src/alter.ts` | `hdsi/alter.py` |
| `src/agency.ts` | `hdsi/agency.py` |
| `src/schedule-preplan.ts` | `hdsi/schedule_preplan.py` |
| `src/urge.ts` | `hdsi/urge.py` |
| `src/delivery.ts` | `hdsi/delivery.py` |
| `src/narrator.ts` | `hdsi/narrator.py` |
| `src/service.ts` | `hdsi/service.py` |
| `src/index.ts` | `hdsi/index.py`（配置模型 + 启动入口） |
| `src/qq-face.ts` | `hdsi/qq_face.py`（微信表情适配） |
| `src/desktop-bridge.ts` | `hdsi/desktop_bridge.py`（可选，无桌面端时降级） |
| `src/script/x.ts` | `hdsi/script/x.py` |

## 硬性约定

1. **同步化**：上游 `async/await`（含 `Promise`）一律改成同步函数/方法。
   并发语义用线程 + 锁复刻：每条故事一把 `threading.RLock`（`hdsi/service.py` 的 `serial()`），
   数据库写串行队列用一把全局写锁；后台任务用 `threading.Thread`。
   返回 Promise 的辅助函数改成直接返回结果。
2. **命名**：类名保持 PascalCase；函数/方法/变量改 snake_case；
   持久化字段名、JSON key、prompt 文本、日志文案保持与上游完全一致（camelCase 原样）。
   上游私有方法 `private fooBar()` → `_foo_bar()`。
3. **数据形状**：一律用 `dict`（`TypedDict` 仅做类型标注，见 `hdsi/types.py`）。
   可选字段用 `None` 或缺省 key，判断时用 `.get()`；展开操作 `{...a, ...b}` → `{**a, **b}`。
   删除 key 用 `del`/重建 dict，不要写 `None` 冒充“没有该字段”，除非上游就是这么传的。
4. **时间**：`Date` → `datetime`（必须有 tzinfo，统一 UTC）。工具在 `hdsi/time_utils.py`：
   `now_utc()`、`parse_time()`、`iso()`、`story_local_time_context()`、`local_clock_minutes()`、
   `calendar_day_key()`、`format_story_display_time()`、`same_timestamp()`。
   持久化成 ISO 字符串的字段（如 `automation.nextAdvanceAt`）保持字符串，与上游一致。
5. **存储**：不 import koishi。数据库访问统一走 `hdsi/store.py` 的
   `store.get(table, query, options)` / `store.create(table, data)` / `store.set(table, query, data)` /
   `store.remove(table, query)`；query 支持等值、`{'$in': [...]}`、`{'$gt': v}`、`{'$gte': v}`、
   `{'$lt': v}`、`{'$lte': v}`；options 支持 `{'limit': n, 'sort': {field: 'desc'}}`。
   行字段与上游 `src/database.ts` 的表结构完全一致；timestamp 字段读出来是 `datetime`。
6. **LLM 调用**：只能依赖 `hdsi/model_routing.py` 暴露的 `ModelRouter` /
   `NarrativeProvider` 接口，禁止直接 import 微信侧代码。
   上游 `ctx.http` → `requests`；`ctx.puppeteer` → 平台层可选能力，不可用时按上游降级分支走。
7. **平台层**：所有微信相关能力（发消息、取图、表情包库、群成员名、回复引用）
   通过 `hdsi/platform/` 的接口注入；`hdsi/` 内不得 import `bot`/`config`/`localdb_listener`。
8. **提示词保真**：narrator/service 中拼 prompt 的字符串一律逐字复制上游（包括中文标点、换行、缩进语义）。
   插值改成 f-string 或 `+`，不得意译、不得简写。
9. **日志保真**：`reportOperation(...)` 的格式串、级别、文案照抄；
   替换 `%s/%d` 为 Python `%` 或 f-string 时保持最终文本一致。
10. **兼容 Python 3.9**：不要用 `X | Y` 注解（除非 `from __future__ import annotations`）、
    不要用 `match`、`dict | dict` 合并运算符；用 `typing.Optional/List/Dict/Tuple/Union`。

## 每个文件的交付要求

- 文件头注释写明上游对应文件与版本。
- 导出与上游 `export` 一一对应（常量、类型、函数、类）。
- 纯函数不得访问全局状态；有状态类与上游构造函数语义一致。
- 完成后用 `python -m py_compile` 自检；如导入了其它 hdsi 模块，确保名字与骨架一致。

## 保留字与命名注意

- TS 里的 `from`（NarrativeRequest.from、TimelinePlanRequest.from、SchedulePreplanRegime.from 等）
  在 Python 字典里 **保持 key = `'from'` 不变**，取用一律 `d['from']`；TypedDict 里用 `from_` 仅作注释，
  不要真的写 `from_` 进数据。
- TS 里的 `type`、`class` 等 key 同理原样保留。
- 与上游同名的导出函数，若与 Python 内置冲突（如 `compile`），加 `_` 后缀并在注释里标注上游名。

## 目录：平台适配

`hdsi/platform/` 不属于上游，是本项目对接微信的适配层：
- `session.py`：InboundSession（群聊/私聊事件、图片、引用、发送者名）
- `wxbot.py`：把 bot.py 的能力（发送、取图、识图、表情库、群成员）封装成 `PlatformAdapter`
- `capabilities.py`：ChatActionCapabilities 的微信实现（引用回复、表情回应降级）

上游 Koishi 概念到本项目的映射（详细见 `hdsi/index.py`）：
`ctx.database`→`store.Database`；`session`→`platform.session.InboundSession`；
`ctx.bots/adapter.sendMessage`→`PlatformAdapter.send_*`；`ctx.http`→`requests`；
`ctx.puppeteer`→`PlatformAdapter.puppeteer`（可为 None，走上游降级分支）。
