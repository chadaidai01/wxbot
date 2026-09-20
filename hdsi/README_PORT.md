# HDS-Interlude Python 移植（hdsi/）

本目录是 [Gitee: MomoiCore/hds-interlude](https://gitee.com/MomoiCore/hds-interlude)
**master（1.0.1-beta6-rebuild）** 的一比一 Python 移植，接入本项目（WeChatBot_WXAUTO_SE）。

- 上游是 Koishi / TypeScript 插件（36 个 src 文件、约 1.68 万行）；
- 本包逐模块对应上游 `src/**`（文件名 snake_case），运行逻辑、提示词、阈值、状态机逐字复刻；
- 上游 `ctx.database/session/bot/http/puppeteer` 分别映射到 `hdsi/store.py`、
  `hdsi/platform/session.py`、`hdsi/platform/wxbot.py`、`requests`、可选 Puppeteer 能力；
- 上游 `async/await` 统一改为同步实现，并发语义用线程 + 每故事锁复刻。

> 参考源码保存在工作区 `.hdsi_reference/src/**`（上游同版本，只读对照用）。

## 文件映射

| 上游 | 本包 |
| --- | --- |
| `types.ts` | `types.py` |
| `time.ts` | `time_utils.py`（避免与标准库 `time` 冲突） |
| `logging.ts` / `meta.ts` | `logging.py` / `meta.py` |
| `story-state.ts` / `turn-persistence.ts` | `story_state.py` / `turn_persistence.py` |
| `database.ts` | `database.py`（表结构）+ `store.py`（SQLite 实现） |
| `model-routing.ts` / `narrator.ts` | `model_routing.py` / `narrator*.py`（拆 4 个文件） |
| `alter.ts` / `agency.ts` / `schedule-preplan.ts` / `urge.ts` | 同名 snake_case |
| `group-willingness.ts` / `qq-face.ts` / `delivery.ts` | 同名 snake_case |
| `service.ts` | `service_*.py`（按职责拆 mixin）+ `service_helpers.py` + `service_types.py` |
| `index.ts` | `config_model.py`（配置默认值）+ `commands.py`（管理命令）+ `index.py`（运行时引导） |
| `desktop-bridge.ts` | `desktop_bridge.py`（无桌面端时降级为空操作） |
| `script/*.ts`（17 个） | `script/*.py` |

`service.ts` 的拆分（原文件 8524 行）见 `SERVICE_PORTING_SPEC.md`：
`service_base`（字段/构造/配置/日志/数据库）、`service_story`（故事与参与者/管理/purge）、
`service_inbound`（私聊/群聊入口与缓冲）、`service_media`（贴纸/图片/音频/向量/投递前处理）、
`service_narrative`（advance/decide/时间导演）、`service_decision`（决策落库/Alter/记忆）、
`service_schedule`（到期意图/投递/自动推进调度）、`service_memory`（压缩/日程预排/状态补丁）。

## 平台适配（与上游的差异）

1. **微信而非 OneBot**：`hdsi/platform/wxbot.py` 实现 `PlatformAdapter`，
   发送走 `bot.py` 的 `_send_msg/_send_file`（`wx_op_lock` 串行），媒体读取走本地文件/本地库；
   QQ 原生表情、消息 reaction、Puppeteer 网页观察在微信侧默认不可用，按上游降级分支执行。
2. **同步执行**：上游 Promise → 同步函数；`ctx.setTimeout/setInterval` → `RuntimeContext` 的线程定时器。
3. **每角色主剧本**：上游一个机器人账号一部主剧本；本项目一个聊天窗口对应一份角色 Prompt，
   相同 Prompt 的聊天共享同一部主剧本（session.selfId 追加角色哈希做隔离），其余语义不变。
4. **群聊**：`LISTEN_LIST` 中开启剧场的聊天会成为上游 `onebot.groupChats` 规则，`responseMode='always'`
   （原来的"未@就忽略（节省token）"门控已按需求删除）；群聊意愿层仅在显式开启时生效。
5. **Desktop bridge**：本项目没有桌面端 IPC，`install_desktop_bridge(service, transport=None)`
   直接返回不上报；等待 typ-0 渠道时调用 `set_desktop_delivery_handler` 由平台层接管。
6. **管理命令**：`hdsi/commands.py` 保留全部 `interlude.*` 命令的解析与文案；
   在微信里可通过 bot.py 的指令入口调用（如需接入请在 bot.py 增加分发）。

## 数据存储

- SQLite：`Theater/hdsi.sqlite3`（表名与上游 `src/database.ts` 完全一致，共 13 张表）。
- 时间字段读出为 UTC aware `datetime`；JSON 字段与上游 camelCase 完全一致。
- 旧的 `Theater/<聊天名>.json`（theater.py 时代）不再写入；如需迁移请单独处理（两者数据模型不同）。

## 网页端管理（config_editor）

`python config_editor.py` 的「剧场系统」与「剧场记忆」两个页面已按新引擎重写：

- **配置界面**：按「引擎与模型 / 节奏与推进 / 叙事与记忆 / 情绪 Alter / 主动联系 Agency /
  日程 Preplan / 群聊」分组，每个开关都标注了映射到的上游配置段；
  旧版遗留且被引擎忽略的字段（群聊回复概率、主动冒泡间隔、状态摘要刷新间隔）只以隐藏字段保留，不再出现在表单里。
- **剧场记忆编辑器**：直接读写 `Theater/hdsi.sqlite3`，包含
  设定（Canon）/ 演化层（settingOverlay + 快照）/ 剧本（分页+类型过滤+清空）/
  参与者（称呼、背景、关系、未决线索、关系备注、关系演化）/
  场景与弧线（引子/摘要修正、历史查看）/ 记忆（interlude_memory 增删归档）/
  长期事实（scope/importance/confidence/unresolved、忘记/恢复）/
  意图（到期时间、取消/完成）/ 状态（continuity、automation 重置、Alter、Agency、在场表、手头小事、未决线索）/
  日程（regimes/exceptions JSON、请求重建、清空）/ 运行信息（计数、设定补丁驳回、场景帧、导出、删除剧场）共 11 个标签页。
- **旧版 JSON 记忆**：首次打开记忆页时，`Theater/*.json` 会被整体移动到
  `Theater/_legacy_json_backup/`（不删除），此后编辑器与引擎都只使用 SQLite。
- 后端 API 前缀为 `/api/hdsi/*`（stories / story / script / memories / facts / intents / participant / patches），
  与 bot 进程共用同一个 SQLite 文件（WAL 模式，可同时运行）。

## 自检

```bash
# 1) 全模块编译/导入
python -m compileall -q hdsi
python -c "import hdsi.index"

# 2) 端到端冒烟（不需要微信/真实模型）
python hdsi/tests/smoke_runtime.py

# 3) 上游测试移植（若已生成）
python -m unittest discover -s hdsi/tests -p "test_*.py" -v
```

## 已知取舍

- 上游对畸形输入会抛异常的位置，Python 移植多数按"宽容降级"处理（已在各文件注释标注）。
- `undefined` 与 `null` 在 Python 里都是 `None`：字段读取统一 `.get()`，行为一致但序列化文本可能有 `null`/缺键差异。
- UTF-16 与码点差异：含 emoji 等非 BMP 字符时，按字符数截断的位置可能与 JS 相差 1-2 个字符。
- 桌面端专用方法（snapshot/timeline/purge-range/cursor-set）已移植为 `service_desktop.py`，
  但没有桌面 IPC 时不会被调用。
- 旧的 `Theater/*.json` 数据会在首次打开记忆页时归档到 `Theater/_legacy_json_backup/`，需要人工决定是否删除。
