# PowerDesigner 16.5 COM Automation API — 验证记录

> 本文记录在 **Sybase PowerDesigner 16.5.0.3982**（D:\powerdesigner，COM 服务器
> `pdshell16.exe`，CLSID `{B6988F4F-A312-45D8-9BFF-29ABF70844E1}`）上**实际验证过**
> 的自动化接口事实。信息来源：
> 1. 官方 `Ole Automation\VBScriptConstants.vbs`（元类 ID 常量）
> 2. 官方 C# 示例 `Ole Automation\CSharp Samples\Sample1\Form1.cs`
> 3. `Interop.PdCommon.dll` / `Interop.PdPDM.dll` 元数据（成员名）
> 4. 本机真机 POC 运行（`pd_mcp probe` / `.probe/exp_round*.py`）
> 5. Sybase 官方文档《Generating a Database (Scripting)》(infocenter.sybase.com)

## 1. 连接

- ProgID：`PowerDesigner.Application`（16.5 变体：`PowerDesigner.Application.16.5`）
- 本机注册在 32 位视图（WOW6432Node），但 **64 位 Python `Dispatch()` 可直接
  CoCreateInstance 成功**（out-of-process LocalServer32）。
- 附加到运行实例：`pythoncom.GetActiveObject(progid)`。
- `Application` 无 `Quit` 方法 —— 关闭用 `PostMessage(MainWindowHandle, WM_CLOSE)`。
- `app.InteractiveMode = False` 抑制模态对话框（真机验证有效；
  设为 1 反而可能弹窗导致 COM 调用挂起）。
- `app.Version` → "16.5.0.3982"；`app.HomeDirectory` → 安装目录；
  `app.BeginTransaction/EndTransaction/CancelTransaction` 真机可用。

## 2. 模型

- 创建：`app.CreateModel(modelClass, template, 0)`
  - PDM 模型类 ID：`-840675807`（CDM `509178224`，LDM `1598421368`，
    来源 VBScriptConstants.vbs）
  - `template = "|Diagram=PhysicalDiagram"`（属性串格式，来自官方 C# 样例）
  - **`template` 也可以是 .pdm 文件路径**：新模型直接绑定该文件（SaveAs 替代方案的基石）
- `model.ChangeDBMS("MySQL 5.0")` → DBMS 变为 `MYSQL50`（真机验证）
- 默认 DBMS：新 PDM 为 `SYASA10`（SQL Anywhere 10）
- `model.Save()`：有文件时写盘；**未保存模型静默返回（不弹窗、不落盘）**
- `model.FileName`：未保存时为空串；**FileName 属性只读**（SetAttribute 被拒）
- `model.CheckModel()` 返回 `None` —— 检查结果进 PD 的 Result List 窗口
- `model.GetPackageOptions()` 生成选项对象（见 §5 DDL）

## 3. 表 / 列 / 主键（官方样例逐字验证）

```python
tbl = model.CreateObject(940288576, "", -1, False)   # PdPDM_Table
tbl.SetNameAndCode("ExpUser", "exp_user")
col = tbl.Columns.CreateNew(940288577)               # PdPDM_Column
col.SetNameAndCode("UserId", "user_id")
col.DataType = "INT"; col.Length = 30
col.Mandatory = True
col.Primary = True          # ← 官方样例的主键创建方式
diagram.AttachObject(tbl)   # 图形符号
```

- Key 对象（940288580）有 `Primary` 可读写、`Columns` 关联集合，可显式命名 PK。
- 列还有 `DefaultValue / Comment / Description / Domain(let/set)`。

## 4. 引用 / 索引

- `References.CreateNew(1852601152)`；`ref.ParentTable = t1; ref.ChildTable = t2`
- **`ref.ParentKey = parentPK`** 赋值后 PD 自动生成 Joins 并迁移 FK 列
  （真机验证：1:N 自动建立 join）。
- 手动 join：`ref.Joins.CreateNew(1852601153)` →
  `join.ParentTableColumn / join.ChildTableColumn`（可 let/set，已验证）
- Reference 另有 `Mandatory / ParentRole / ChildRole / MinimumCardinality /
  MaximumCardinality / UpdateConstraint / DeleteConstraint / ForeignKeyConstraintName`、
  方法 `UpdateReferenceJoins / UpdateImplementation`
- 索引：`table.Indexes.CreateNew(940288578)`；`idx.Unique = True`；
  **列集合名为 `IndexColumns`**（不是 Columns），`ic.Column = col`
  （IndexColumn 940288579，可 let/set Column）。

## 5. DDL 生成（GenerateDatabase）

官方文档流程（sybase infocenter 16.5）：

```python
opts = model.GetPackageOptions()
opts.GenerateODBC = False                # 强制 SQL 脚本而非 ODBC
opts.GenerationPathName = r"C:\out" + "\\"   # ⚠ 必须带尾部反斜杠！
opts.GenerationScriptName = "script.sql"
model.GenerateDatabase()                  # 无参调用
```

**坑 1**：`GenerationPathName` 不带尾部 `\` 时与脚本名直接拼接
（实测生成 `logs` + `exp4.sql` = `logsexp4.sql`）。
**坑 2**：pywin32 动态绑定无法为 `GenerateDatabase` 的可选对象参数构造
默认值（报 "The Python instance can not be converted to a COM object"），
必须用原始 IDispatch Invoke：

```python
ole = model._oleobj_
dispid = ole.GetIDsOfNames(0, "GenerateDatabase")
ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, False)   # 不取返回值
```

真机输出验证（MySQL 5.0）：`drop table if exists exp_user; create table
exp_user ( user_id INT not null, primary key (user_id) );`

## 6. 保存/另存为

- 无 `SaveAs`（BaseModel/Model 均无；`hasattr(model,'SaveAs') == False`）
- 另存为方案（真机验证）：复制
  `<HomeDirectory>\ShellNew\pdmodel16.pdm|.cdm|.ldm` → 目标路径 →
  `CreateModel(kind, 目标路径)`（文件绑定）→ 复制内容 → `Save()`

## 7. 通用访问机制

- 集合：`.Count`、`.Item(i)`（0 基）、`.CreateNew(classId)`、`.Add(obj)`
- 对象：`GetAttribute(name)/SetAttribute(name,value)` 可访问任意元类属性；
  失败属性名会出现在 com_error 文本中（如 "attribute Filename is read-only"）
- `SetNameAndCode(name, code)` 后 Code 可能被命名规范重推导，需回读校验
- 模型种类识别：`model.ClassKind`（数值=元类 ID）；回退：探测 `Tables`/`Entities`
