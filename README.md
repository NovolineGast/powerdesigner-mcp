<div align="center">

# PowerDesigner MCP

**Let AI agents operate PowerDesigner like a professional database modeling engineer.**

[![Windows](https://img.shields.io/badge/platform-Windows-blue)](#quick-start)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](#quick-start)
[![PowerDesigner](https://img.shields.io/badge/tested--with-PowerDesigner%2016.5-green)](#verified-environment)
[![MCP](https://img.shields.io/badge/protocol-MCP%20stdio-purple)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

**English** | [中文](#中文文档)

An MCP (Model Context Protocol) server that drives **Sybase PowerDesigner 16.5**
through its official **COM Automation API** — never by touching `.pdm` binaries —
covering the full database course-design loop:

```
requirements → data dictionary → CDM → E-R → LDM → PDM
            → constraints → indexes → DDL (SQL) → model checks → save
```

**Verified end-to-end on a real PowerDesigner 16.5 installation**: model creation,
tables, columns, primary keys (single & composite), foreign keys with automatic
FK-column migration, indexes, native MySQL DDL generation, validation, and
save-as — 42 unit tests + live acceptance tests, all passing.

</div>

---

## Why this exists

AI coding assistants can reason about database design, but they cannot *touch*
PowerDesigner. This MCP server is the missing execution layer:

- **The AI does the design reasoning.** The server does reliable execution.
- **55+ granular tools** (not one giant tool), plus high-level batch operations.
- **Safety first**: every mutating tool supports `dry_run`; batch tools are
  atomic with automatic rollback; file-level transactions restore the exact
  pre-transaction state.
- **Graceful degradation**: without PowerDesigner installed, the server falls
  back to an in-memory mock backend with identical tool semantics — so the
  whole pipeline is testable anywhere.

## Architecture

```
MCP Client (Cursor / Claude Desktop / Claude Code)
        │  MCP stdio (JSON-RPC)
        ▼
FastMCP Server ──── Tools (55+) / Resources / Prompts
        ▼
Services (schema orchestration · validation engine · inspection · transactions)
        ▼
PowerDesignerAdapter (interface)
   ├── ComAdapter   ── single-threaded STA COM dispatcher ── PowerDesigner 16.5 COM
   └── MockAdapter  ── in-memory model store (tests / PD-less development)
```

Adapter isolation means version differences (16.x / 17.x) stay inside the COM
layer; every assumption is verified against the vendor's own constants file,
`.NET` interop metadata, and live experiments — see
[docs/com-api-notes.md](docs/com-api-notes.md).

## Quick start

### Install — one command, no path editing

```powershell
git clone https://github.com/NovolineGast/powerdesigner-mcp.git
cd powerdesigner-mcp
.\install.ps1
```

That is the whole install. There is no path to type and no client JSON to
hand-edit:

1. **The package is installed as a tool** — `uv tool install --editable .` puts
   `powerdesigner-mcp` in an isolated environment with a launcher on `PATH`
   (without uv it falls back to an editable install in `.venv`).
2. **The client config is written for you** — the installer then runs
   `powerdesigner-mcp install`, resolves the launch command for this machine and
   merges it into every MCP client it detects: `%USERPROFILE%\.workbuddy\mcp.json`,
   `%APPDATA%\Claude\claude_desktop_config.json`, `%USERPROFILE%\.cursor\mcp.json`,
   and `claude mcp add` when that CLI is present. A replaced config is backed up
   next to the original as `mcp.json.bak-<timestamp>`.
3. **The result is verified** — a real PowerDesigner COM probe and a stdio smoke
   test run **on the exact command that was just registered**, so the entry is
   proven to launch rather than merely written.

Already installed? Re-register at any time (after moving the project, changing
clients, etc.):

```powershell
powerdesigner-mcp install                 # detected clients
powerdesigner-mcp install --client workbuddy
powerdesigner-mcp install --dry-run       # preview, write nothing
powerdesigner-mcp install --print-only    # print the entry JSON
```

> **Why this needed absolute paths before:** the project used to be installed as
> *dependencies only* into a `.venv`, so every client entry had to carry the
> venv's interpreter path **and** a `PYTHONPATH` pointing at `src`. Installing
> the package itself removes both, and `install` computes whatever remains by
> itself — including the uv tool launcher when the host does not inherit `PATH`.

### Manual / other platforms

```powershell
uv tool install --editable .          # recommended: launcher on PATH
powerdesigner-mcp install             # writes the client configs
powerdesigner-mcp probe               # COM capability check
powerdesigner-mcp serve               # start MCP server (stdio)

# without uv:
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m pd_mcp install
```

### MCP client configuration

You normally never write this by hand — `powerdesigner-mcp install` does it.
Here is what it produces, for reference:

<details open>
<summary><b>Claude Desktop</b> — <code>%APPDATA%\Claude\claude_desktop_config.json</code></summary>

```json
{
  "mcpServers": {
    "powerdesigner": {
      "command": "C:\\Users\\<you>\\.local\\bin\\powerdesigner-mcp.exe",
      "args": ["serve"],
      "env": { "PDMCP_ATTACH_MODE": "auto", "PDMCP_DEFAULT_DBMS": "MySQL 5.0" }
    }
  }
}
```
</details>

<details>
<summary><b>Cursor</b> — <code>%USERPROFILE%\.cursor\mcp.json</code></summary>

Same structure as above, under the `mcpServers` key.
</details>

<details>
<summary><b>Claude Code</b></summary>

```powershell
claude mcp add powerdesigner -- "$env:USERPROFILE\.local\bin\powerdesigner-mcp.exe" serve
```
</details>

<details>
<summary><b>WorkBuddy</b> — <code>%USERPROFILE%\.workbuddy\mcp.json</code></summary>

Same structure as the Claude Desktop example. Then open **Connector management →
Custom connectors (top-right) → Trust** the `powerdesigner` server; new MCP
servers do not activate automatically.

If your client cannot pass `env` at all, point `command` at
`scripts\powerdesigner-mcp.cmd` (portable wrapper, leave `args` empty) — the
installer also accepts an explicit launcher:
`powerdesigner-mcp install --command <path>`.
</details>

## Tool catalog (55+)

| Group | Tools |
|---|---|
| **Model management** | `get_server_info` · `list_open_models` · `open_model` · `create_model` · `save_model` · `save_model_as` · `close_model` · `get_model_info` · `list_packages` · `get_package` |
| **Read / inspect** | `list_tables` · `get_table` · `search_tables` · `list_columns` · `get_column` · `search_columns` · `list_keys` · `get_key` · `list_indexes` · `get_index` · `list_references` · `get_reference` · `list_relationships` · `get_relationship` · `list_domains` · `get_domain` · `model_snapshot` · `inspect_schema` · `compare_model` |
| **Mutations** (all support `dry_run`) | `create/rename/update/delete_table` · `create/rename/update/delete_column` · `create/set/remove_primary_key` (single & composite) · `create/update/delete_reference` (auto FK migration) · `create/update/delete_index` (normal/unique/composite) |
| **High-level automation** | `create_database_schema` (one JSON → whole schema) · `apply_schema_patch` (16 structured ops, atomic) · `design_from_spec` (materialize a PDM/CDM/LDM design) |
| **Validation loop** | `validate_model` (structural checks) · `check_database_design` (14 machine-checkable rule types, e.g. TABLE_HAS_PK, COLUMN_HAS_COMMENT, naming patterns) |
| **DDL & conversion** | `generate_ddl` (PowerDesigner **native** generation) · `convert_cdm_to_ldm` · `convert_cdm_to_pdm` · `convert_ldm_to_pdm` |
| **Transactions** | `begin_transaction` · `commit_transaction` · `rollback_transaction` · `list_model_backups` · `rollback_model` |

**Resources**: `powerdesigner://models`, `powerdesigner://model/{id}`,
`.../tables`, `.../table/{code}`, `.../relationships`, `.../indexes`
**Prompts**: `database_design_workflow` (full course-design loop), `schema_review`

## The course-design workflow

```
1. get_server_info                       # verify connection
2. design_from_spec(kind=CDM)            # AI designs entities/relations → materialized
3. convert_cdm_to_ldm                    # resolve M:N
4. convert_cdm_to_pdm(dbms="MySQL 5.0")  # physical model
5. create_reference / create_index       # FKs & indexes (dry_run first)
6. validate_model                        # fix every error
7. check_database_design(rules)          # your course naming/comment rules
8. fix → re-validate                     # closed loop
9. generate_ddl                          # native SQL
10. save_model                           # save .pdm
```

## Safety model

| Mechanism | Behavior |
|---|---|
| `dry_run: true` | Returns the exact execution plan, touches nothing |
| `apply_schema_patch` / `create_database_schema` | Atomic: any failure rolls the model back |
| `begin_transaction` | Saves + backs up the model file |
| `rollback_transaction` | Restores the exact pre-transaction file and reopens |
| No file backup + destructive op | Explicit error — never fails silently |

## Configuration

| Env var | Default | Description |
|---|---|---|
| `PDMCP_ADAPTER` | `auto` | `com` / `mock` / `auto` |
| `PDMCP_ATTACH_MODE` | `auto` | `auto` / `attach` (ROT) / `launch` (start pdshell) / `new` |
| `PDMCP_PD_EXE` | auto-detect | Path to `pdshell16.exe` |
| `PDMCP_VISIBLE` | `false` | Show PowerDesigner window when we launch it |
| `PDMCP_DEFAULT_DBMS` | PD default | e.g. `MySQL 5.0` for new PDMs |
| `PDMCP_CALL_TIMEOUT` | `300` | Per COM-call timeout (seconds) |

Full list in [`src/pd_mcp/config.py`](src/pd_mcp/config.py). A JSON config file
(`pdmcp.json`) is also supported.

## Development & testing

```powershell
.venv\Scripts\python.exe -m pytest tests -m "not live"    # 42 unit/smoke tests (mock backend)
$env:PDMCP_LIVE = "1"
.venv\Scripts\python.exe -m pytest tests -m live          # live acceptance (real PowerDesigner)
.venv\Scripts\python.exe -m pd_mcp probe                  # standalone COM capability probe
```

The mock backend mirrors PowerDesigner semantics (PK via `Primary` flags,
reference FK migration, index column binding) so the full pipeline — including
schema orchestration, validation, transactions and DDL — is tested without a
PowerDesigner license.

## Verified environment & honest limitations

- **Verified**: PowerDesigner **16.5.0.3982** on Windows 10/11, 64-bit Python
  3.13. COM facts verified against the vendor's `VBScriptConstants.vbs`,
  `Interop.*.dll` metadata, official C# sample and live probes — see
  [docs/com-api-notes.md](docs/com-api-notes.md).
- `CheckModel()` returns `None` on 16.5 (results go to PD's Result List window);
  machine-readable findings come from `validate_model` / `check_database_design`.
- PowerDesigner has **no SaveAs**: saving an unsaved model to a new path is
  implemented via ShellNew-template file binding + content copy (returns a
  `copied` summary). Diagram symbols are re-attached and auto-laid-out as part
  of the copy, and the saved file is verified to hold the model rather than the
  ShellNew stub (PD 16.5 sometimes writes to an extension-less sibling).
- CDM/LDM are first-class: entities/attributes, identifiers (primaries), and
  conceptual relationships with per-end cardinalities and a dependent side.
  Indexes are a physical concept, so index tools reject a CDM/LDM explicitly.
- CDM inheritance is not exposed yet; the adapter interface makes adding it
  straightforward.
- Generating for a DBMS different from the model's DBMS returns a clear error —
  use `create_model(dbms=...)` / `ChangeDBMS` first.

## License

[MIT](LICENSE)

---

<div align="center">

<a name="中文文档"></a>

# PowerDesigner MCP（中文文档）

**让 AI Agent 像专业数据库建模工程师一样操作 PowerDesigner。**

[English](#powerdesigner-mcp) | 中文

一个 MCP（Model Context Protocol）服务器，通过 PowerDesigner 的**官方 COM
Automation API**（绝不直接修改 `.pdm` 二进制）驱动 **Sybase PowerDesigner 16.5**，
覆盖数据库课程设计完整闭环：

```
需求分析 → 数据字典 → CDM → E-R 模型 → LDM → PDM
        → 完整性约束 → 索引 → DDL(SQL) → 模型检查 → 保存模型
```

**已在真实 PowerDesigner 16.5 环境端到端验收**：建模、表、列、主键（单列/联合）、
外键（自动迁移 FK 列）、索引、原生 MySQL DDL 生成、模型校验、另存为——
42 项单元测试 + 真机验收测试全部通过。

</div>

### 中文目录

- [为什么需要它](#为什么需要它-1)
- [架构](#架构-1)
- [快速开始](#快速开始-1)
- [工具目录](#工具目录-1)
- [课程设计工作流](#课程设计工作流-1)
- [安全模型](#安全模型-1)
- [配置](#配置-1)
- [开发与测试](#开发与测试-1)
- [验证环境与诚实声明](#验证环境与诚实声明-1)

### 为什么需要它

AI 助手擅长数据库设计推理，却无法"触碰" PowerDesigner。本 MCP Server 是缺失的
执行层：

- **AI 负责设计推理，服务器负责可靠执行**；
- **55+ 细粒度工具**（拒绝单一巨型工具）+ 高层批量编排；
- **安全第一**：所有修改工具支持 `dry_run`；批量操作原子执行、失败自动回滚；
  文件级事务可精确还原事务前状态；
- **优雅降级**：未安装 PowerDesigner时自动切换语义一致的内存 Mock 后端，
  整条流水线在任何机器上均可测试。

### 架构

```
MCP 客户端（Cursor / Claude Desktop / Claude Code）
        │  MCP stdio（JSON-RPC）
        ▼
FastMCP Server ──── Tools（55+）/ Resources / Prompts
        ▼
Services（schema 编排 · 校验规则引擎 · 检查 · 事务）
        ▼
PowerDesignerAdapter（抽象接口）
   ├── ComAdapter   ── 单线程 STA COM 调度 ── PowerDesigner 16.5 COM
   └── MockAdapter  ── 内存模型（测试 / 无 PD 环境）
```

Adapter 隔离使版本差异（16.x/17.x）被限制在 COM 层内；所有 API 假设均经厂商
常量文件、.NET Interop 元数据与真机实验验证——见
[docs/com-api-notes.md](docs/com-api-notes.md)。

### 快速开始

```powershell
git clone https://github.com/NovolineGast/powerdesigner-mcp.git
cd powerdesigner-mcp
.\install.ps1        # 一条命令：装包 + 注册客户端 + 真机 COM 探测 + 冒烟测试
```

**不需要手写任何路径**，安装脚本自己完成三件事：

1. **把包装成工具**（`uv tool install --editable .`）：独立环境 + PATH 上的
   启动器；没有 uv 时退回项目 `.venv` 可编辑安装。
2. **自动写客户端配置**：随后执行 `powerdesigner-mcp install`，自行解析本机
   启动命令，并合并进它探测到的所有客户端配置（WorkBuddy / Claude Desktop /
   Cursor，以及存在 `claude` CLI 时的 `claude mcp add`）；被替换的配置会备份为
   `mcp.json.bak-<时间戳>`。
3. **验证配置真能启动**：真机 COM 探测 + 用**刚刚注册的那条命令**跑 stdio 冒烟
   测试，而不是只把 JSON 写进去。

已装过？随时重新注册（搬目录、换客户端之后）：

```powershell
powerdesigner-mcp install                 # 自动探测客户端
powerdesigner-mcp install --client workbuddy
powerdesigner-mcp install --dry-run       # 只预览，不写文件
powerdesigner-mcp install --print-only    # 打印配置 JSON
```

> **以前为什么要手写路径**：项目过去只把**依赖**装进 `.venv`，包本身没装，
> 于是每个客户端条目都必须带 venv 解释器路径 **加** 指向 `src` 的 `PYTHONPATH`。
> 现在把包装成工具后两者都不需要，剩下的路径由 `install` 自己算——包括宿主不
> 继承 `PATH` 时改走 uv 工具启动器。

手动安装 / 其他平台：

```powershell
uv tool install --editable .
powerdesigner-mcp install
powerdesigner-mcp probe     # COM 能力探测
powerdesigner-mcp serve     # 启动 MCP Server（stdio）

# 不用 uv 时：
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m pd_mcp install
```

客户端配置样例（正常无需手写，由 `powerdesigner-mcp install` 生成）见上文英文部分。

### 工具目录

| 分组 | 工具 |
|---|---|
| **模型管理** | `get_server_info` · `list_open_models` · `open_model` · `create_model` · `save_model` · `save_model_as` · `close_model` · `get_model_info` · `list_packages` · `get_package` |
| **读取/检查** | `list_tables` · `get_table` · `search_tables` · `list_columns` · `get_column` · `search_columns` · `list_keys` · `get_key` · `list_indexes` · `get_index` · `list_references` · `get_reference` · `list_relationships` · `get_relationship` · `list_domains` · `get_domain` · `model_snapshot` · `inspect_schema` · `compare_model` |
| **修改类**（全部支持 `dry_run`） | `create/rename/update/delete_table` · `create/rename/update/delete_column` · `create/set/remove_primary_key`（单列/联合）· `create/update/delete_reference`（自动迁移 FK 列）· `create/update/delete_index`（普通/唯一/联合） |
| **高层自动化** | `create_database_schema`（一份 JSON 建成整库）· `apply_schema_patch`（16 种结构化操作，原子执行）· `design_from_spec`（物化 PDM/CDM/LDM 设计） |
| **校验闭环** | `validate_model`（结构检查）· `check_database_design`（14 种可机检规则类型） |
| **DDL 与转换** | `generate_ddl`（PowerDesigner **原生**生成）· `convert_cdm_to_ldm` · `convert_cdm_to_pdm` · `convert_ldm_to_pdm` |
| **事务** | `begin_transaction` · `commit_transaction` · `rollback_transaction` · `list_model_backups` · `rollback_model` |

**Resources**：`powerdesigner://models`、`powerdesigner://model/{id}` 及 tables /
table / relationships / indexes 子资源　**Prompts**：`database_design_workflow`（课程设计全流程）、`schema_review`

### 课程设计工作流

```
1. get_server_info                       # 确认连接
2. design_from_spec(kind=CDM)            # AI 设计实体/联系 → 一次性物化
3. convert_cdm_to_ldm                    # 概念 → 逻辑（拆解 M:N）
4. convert_cdm_to_pdm(dbms="MySQL 5.0")  # 逻辑 → 物理
5. create_reference / create_index       # 补外键与索引（先 dry_run）
6. validate_model                        # 修复全部 error
7. check_database_design(rules)          # 课程规范（命名/注释/PK/必填列）
8. 修复 → 复检                           # 闭环
9. generate_ddl                          # 生成 SQL
10. save_model                           # 保存 .pdm
```

### 安全模型

| 机制 | 行为 |
|---|---|
| `dry_run: true` | 只返回执行计划，不触碰模型 |
| `apply_schema_patch` / `create_database_schema` | 原子执行：任一步失败自动回滚 |
| `begin_transaction` | 先保存并备份模型文件 |
| `rollback_transaction` | 精确还原事务前文件并重新打开 |
| 无文件备份 + 破坏性操作 | 明确报错——绝不静默失败 |

### 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PDMCP_ADAPTER` | `auto` | `com` / `mock` / `auto` |
| `PDMCP_ATTACH_MODE` | `auto` | `auto` / `attach`（ROT 附加）/ `launch`（自启动 PD）/ `new` |
| `PDMCP_PD_EXE` | 自动检测 | `pdshell16.exe` 路径 |
| `PDMCP_VISIBLE` | `false` | 自启动时是否显示 PD 窗口 |
| `PDMCP_DEFAULT_DBMS` | PD 默认 | 新建 PDM 的 DBMS，如 `MySQL 5.0` |
| `PDMCP_CALL_TIMEOUT` | `300` | 单次 COM 调用超时（秒） |

完整清单见 [`src/pd_mcp/config.py`](src/pd_mcp/config.py)；也支持 JSON 配置文件（`pdmcp.json`）。

### 开发与测试

```powershell
.venv\Scripts\python.exe -m pytest tests -m "not live"    # 42 项单测/冒烟（Mock 后端）
$env:PDMCP_LIVE = "1"
.venv\Scripts\python.exe -m pytest tests -m live          # 真机验收
.venv\Scripts\python.exe -m pd_mcp probe                  # 独立 COM 能力探测
```

Mock 后端复刻 PowerDesigner 语义（列 Primary 主键、引用 FK 迁移、索引列绑定），
因此 schema 编排、校验、事务与 DDL 全流程在无 PD 许可证的机器上同样可测。

### 验证环境与诚实声明

- **验证环境**：Windows 10/11 + PowerDesigner **16.5.0.3982** + 64 位 Python 3.13。
  COM 事实经厂商 `VBScriptConstants.vbs`、`Interop.*.dll` 元数据、官方 C# 样例
  与真机实验交叉验证——见 [docs/com-api-notes.md](docs/com-api-notes.md)。
- 16.5 的 `CheckModel()` 返回 `None`（结果进 PD Result List 窗口）；机器可读的
  检查结果由 `validate_model` / `check_database_design` 提供。
- PowerDesigner **没有 SaveAs**：未保存模型另存通过 ShellNew 模板文件绑定 +
  内容复制实现（返回 `copied` 统计）。复制过程会重新挂载图符号并自动布局，
  且会校验请求路径确实拿到模型内容而非模板壳（PD 16.5 有时会把内容写到
  去掉扩展名的同级文件）。
- CDM/LDM 为一等公民：实体/属性、标识符（主标识）、概念联系（两端基数 +
  依赖侧）。索引属物理概念，索引类工具对 CDM/LDM 显式报错。
- CDM 继承（Inheritance）暂未暴露；Adapter 接口使补充实现非常直接。
- 生成与模型 DBMS 不符的 SQL 会返回明确错误——请先 `create_model(dbms=...)`
  或 ChangeDBMS。

### 许可证

[MIT](LICENSE)
