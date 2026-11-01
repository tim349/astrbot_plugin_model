# astrbot_plugin_gunpla

AstrBot 4.x 插件：查询**拼装模型新品/预购**与**日/英/韩说明书链接**（寿屋、高达、机娘）。
数据源：Dalong + 寿屋台湾。纯标准库，无第三方依赖。

注意：本插件由ai生成，仅用于学习交流，使用该skill造成的不良影响有使用者承担

## 为什么做成插件（而不是只放 Skill）

Skill 是提示词，模型可以不读；插件注册的是 **LLM Tool**，和「网页搜索」同级。
本插件的工具描述里明确写了「优先于通用联网搜索」，模型在 function calling 阶段才会选中它。

## 安装

```bash
# 目录名必须是 astrbot_plugin_gunpla
cd AstrBot/data/plugins
# 把本目录整个复制进来（或 git clone）
ls astrbot_plugin_gunpla/
#   main.py  metadata.yaml  core/  tests/  README.md
```

复制后在 WebUI「插件」页点刷新/重载，看到「拼装模型说明书」即加载成功。

- 要求 `astrbot_version >= 4.16,<5`（已在 metadata 声明；4.29.3 满足）。
- 依赖：无（`requirements.txt` 不需要）。
- Windows / Docker 均可；脚本用 `sys.executable` 运行，不需要额外配置 Python 路径。

## 提供的工具（LLM 自动调用）


| 工具名                       | 用途                       | 参数                                           |
| ---------------------------- | -------------------------- | ---------------------------------------------- |
| `gunpla_manual_search`       | 按名称查说明书             | `keyword`：中文俗称/日文名/英文名/Dalong 编号  |
| `gunpla_news`                | 新品与预购资讯             | `keyword`（可选）、`source`（all/koto/dalong） |
| `gunpla_manual_by_dalong_id` | 已知 Dalong 编号直接出链接 | `dalong_id`：`mdv271` 或完整链接               |

## 提供的指令（不经 LLM，方便测试）

```
/gunpla 雪兔
/gunpla mdv271
/gunpla_news 独角兽
```

## 行为约定

- **30 秒硬性超时**：查询超过 30 秒或脚本报错，统一返回
  `没有找到对应的内容，可能网站还没有搜录，或者更换描述后重试`。
- 工具找不到结果时返回同一句话，并在描述里提醒模型「不要反复换关键词重试」。
- 说明书链接固定为 `_i_`（日/韩/英），评测页为 `_p_`；两种都给，不猜图片地址。

## 配置项（WebUI 插件配置页）

`_conf_schema.json` 提供以下可调项，保存后写入 `data/config/astrbot_plugin_gunpla_config.json`：


| 配置                  | 类型   | 默认     | 说明                                                 |
| --------------------- | ------ | -------- | ---------------------------------------------------- |
| `timeout_seconds`     | int    | 30       | 单次查询超时，范围 10–120；调大不会提高命中率       |
| `max_results`         | int    | 5        | 说明书候选条数，范围 1–10                           |
| `news_limit`          | int    | 8        | 新品/预购条数，范围 1–20                            |
| `default_news_source` | string | `all`    | `all` / `koto` / `dalong`，news 未指定 source 时使用 |
| `enable_commands`     | bool   | true     | 是否响应`/gunpla`、`/gunpla_news` 指令               |
| `not_found_message`   | text   | 内置文案 | 查不到时的统一回复，留空用默认                       |

越界值会被夹到合法区间（例如 `timeout_seconds: 1` → 10），不会因为配置写错而崩溃。

## 让它更「优先」触发

插件已经是工具，模型按描述选择。如果仍被网页搜索抢走，可以再加一层：

1. **人格/系统提示**中加路由规则：
   ```
   遇到拼装模型、高达、钢普拉、寿屋、机娘、说明书、预购、新品等问题，
   必须先调用 gunpla_manual_search / gunpla_news，不要先联网搜索；
   只有这些工具返回「没有找到对应的内容…」后才可以换搜索。
   ```
2. 或在 WebUI 里给通用搜索工具加前置过滤：命中「说明书 / 高达 / 寿屋 / 机娘」时改走本插件。

## 排查：工具被调用了但没结果

日志里如果出现这一行：

```
gunpla_manual_search 没有返回值，或者已将结果直接发送给用户。
```

说明 **llm_tool 用了 `yield event.plain_result(...)`**。AstrBot 4.29 的 agent runner
只把 `return` 的值当作 tool result；async generator 会被判定为「无返回值」，
模型拿不到链接，就会转去 `astrbot_execute_shell` 瞎试路径、猜编号。
本插件 v1.0.1 起已改为 `return` 字符串，工具函数**不要**再改回 `yield`。

其它可对照的现象：


| 现象                                                            | 含义                                     |
| --------------------------------------------------------------- | ---------------------------------------- |
| 日志有`使用工具：gunpla_manual_search` 但无 `[gunpla] tool=...` | 装的是旧版本（无日志），需要覆盖更新     |
| 日志完全没有`gunpla_*`                                          | 插件未启用 / 被其它插件接管了消息        |
| 模型用`astrbot_execute_shell` 跑 `gunpla.py`                    | 工具没给结果，模型在走歪路；不是插件在跑 |
| 回复里出现「没有收录」而非「没有搜录」                          | 模型自己写的，不是插件返回值             |

更新后重载插件，正常应看到：

```
[gunpla] tool=gunpla_manual_search keyword='篝火真里亞'
Tool `gunpla_manual_search` Result: 【说明书 1】篝火 真里亞【戦闘装束】…
```

## 结构

```
main.py                插件入口：3 个 llm_tool + 2 个指令 + 配置读取
metadata.yaml          插件元数据（市场展示、版本范围）
_conf_schema.json      WebUI 可配置项（超时/条数/数据源/指令开关/兜底文案）
core/scripts/*.py      查询实现（与 gunpla-model-info skill 同一套代码）
core/data/*.json       术语表（中文俗称 → 日/英/韩名）与繁简表
tests/test_plugin_smoke.py  无需 AstrBot 的冒烟测试
```

`core/` 是从 skill 复制的独立副本。改术语表请两边同步，或只维护一处后重新复制：

```bash
cp -r ../gunpla-model-info/scripts/* core/scripts/
cp -r ../gunpla-model-info/data/* core/data/
```

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：插件可导入、3 个工具/2 个指令已注册、子进程与进程内两条执行路径、
超时与空结果都返回统一话术。

## 实现要点

- 优先**子进程**执行 `core/scripts/gunpla.py`，与 AstrBot 环境隔离；
  若宿主禁止创建管道（部分 Windows 服务、受限沙箱会 `PermissionError`），
  自动退回**进程内调用**，功能不受影响。
- 所有网络异常、解析异常都收敛成同一句话，不向用户抛栈。
- 不写 `data/` 持久化数据（只读查询 + 本地缓存目录 `~/.cache/gunpla-skill`）。
