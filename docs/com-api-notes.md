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

## 8. `Save()` 的两个真机陷阱（补充第 6 节）

1. **请求路径可能只拿到模板壳**：`Save()` 有时把真实内容写到"去掉扩展名"的
   同级文件（50 KB 模型落在 `a_model`，请求的 `a_model.pdm` 只剩 1.9 KB 模板壳；
   `FileName` 在 `Save()` 后会被规范化成不含扩展名的形式）。另一些会话里 PD
   会正常写回请求路径并额外生成 `.pdb/.cdb` 备份。行为不稳定，所以
   **保存后必须校验请求路径的内容**，必要时把无扩展名文件搬回请求路径——
   移动前必须先 `Close()`（PD 持句柄，否则 WinError 32），移动后再
   `OpenModel(请求路径)` 让绑定保持一致。实现见 `_stray_saved_file()`
   与 `save_model_as()`。
2. **内容复制不带图符号**：复制后必须对每个表/实体与引用/关系重新
   `AttachObject` / `AttachLinkObject`，否则新模型打开是空白图。附加完毕后调
   `AutoLayout`（原始 Invoke、无参）可避免符号重叠。`Model.Diagram.Symbols.Count`
   可用来断言"图不是空的"。

## 9. CDM / LDM（概念层）

PDM 之外的对象容器与语义（真机验证）：

| 概念 | PDM | CDM / LDM |
| --- | --- | --- |
| 实体 | `Tables` | `Entities` |
| 属性 | `Columns` | `Attributes` |
| 主键 | `Keys` + `Key.Primary=True` | `Identifiers` + `Entity.PrimaryIdentifier` |
| 关联 | `References`（ParentTable/ChildTable/Joins） | `Relationships`（Entity1/Entity2） |
| 索引 | `Indexes` | **不存在**（物理概念，应显式拒绝而非静默忽略） |

- **属性没有 `Primary` 语义**：给 CDM 属性设 `Primary=True` 不构成主键。必须
  `Identifiers.CreateNew(509178229)` → `Identifier.Attributes.Add(attr)` →
  `Entity.PrimaryIdentifier = ident`。成员可用 `Attributes.Add/Remove/Clear` 增删。
- **主标识符不能"就地替换"、也不能先置空**：
  - `Entity.PrimaryIdentifier = None` 抛类型不匹配（`_try_set` 会静默吞掉）；
  - 旧标识符仍存在时新建同名标识符 → `That name already exists!`。
  正确顺序：**先 `prev.delete()`**（删除后 `PrimaryIdentifier` 自动变 None），
  再创建新标识符并赋值；赋值后必须回读校验，失败要**报错而不是静默通过**。
- **属性数据类型是 PD 自有词汇**，不是 DBMS 语法：`Characters(n)` /
  `Variable characters(n)` / `Integer` / `Decimal(p,s)` / `Timestamp`。
  直接传 `VARCHAR(20)` 会让 CheckModel 报未知类型。
- **关系基数**：`Entity1ToEntity2RoleCardinality`（E1→E2，即 E2 端重数）与
  `Entity2ToEntity1RoleCardinality`（E2→E1，即 E1 端重数），值为**逗号格式**
  `"0,n"` / `"1,1"`（不是 SQL 的 `0..*`）。角色名 `Entity1ToEntity2RoleName` /
  `Entity2ToEntity1RoleName`；依赖侧 `DependentRole`（`"A"`/`"B"`）。
- **CDM 自身不做外键迁移**：实测 `campus_canteen.cdm` 中每个标识符属性只出现
  一次；父实体的标识符是在**生成 LDM 时**才迁移进 `(x,n)` 端的子实体。
- **原生转换入口**：PD 16.5 **没有** `GenerateLogicalDataModel` /
  `GeneratePhysicalDataModel` 方法，唯一入口是
  `GenerateModel(ObjectSelection, Kind, Target, SaveDependencies)`（dispid 33554819），
  须原始 `InvokeTypes` 调用：

  ```python
  ole.InvokeTypes(33554819, 0, pythoncom.DISPATCH_METHOD, (9, 2),
                  ((9, 49), (3, 49), (8, 49), (11, 49)),
                  None, 目标模型元类ID, "", True)
  ```

  返回的模型**未绑定文件**，落盘要走第 6 节的 ShellNew 绑定流程。
- **链接对象的符号判定**要覆盖关系：CDM 关系**没有 `Joins` 集合**，只按
  `hasattr(obj,'Joins')` 判断会把关系送进 `AttachObject`（对链接对象是静默
  no-op），图上就会丢连线。判定应同时检查 `Entity1`/`Entity2`。

## 10. 工程注意事项（非 COM，但同样真机踩过）

- **`GetAttribute` 对不存在的属性也返回非 None**，所以 `_safe()` 的
  GetAttribute 兜底**不能**用来判断属性/集合是否存在。真机后果：一个 CDM 与
  一个 LDM 都被判成 PDM（`_safe(m,'Tables')` 返回了非 None）。凡"探测存在性 /
  区分种类"的代码一律走纯 `getattr`（实现：`_plain_attr()`）。
- **`ClassKind` 可能是 int，也可能是数字字符串**（真机：原生生成的 LDM 返回
  `'1598421368'`），比较前先 `int(str(ck))` 归一化；且必须在**创建该对象的同一
  COM 套间**内读取——跨线程派发得到的代理会给出不同结果。
- **`ObjectID` 类型随元类变化**：模型是整数，CDM 标识符是 GUID 字符串
  （`'{8CAF64C2-…}'`）。对象身份比较请统一用字符串形式；`int('{...}')` 会抛异常 →
  退化成 `is` 比较 → 恒 False（曾导致主标识符回读 `primary=False`、CDM→LDM
  迁移失效）。
- **COM 代理必须在创建它的套间里释放**：主线程（从未 `CoInitialize`）在解释器
  退出时回收代理会报 `CO_E_NOTINITIALIZED (0x800401f0)` 致命异常。断开连接时
  应在派发线程内清空 `_models`/`_refs`/`_app`（见
  `ComAdapter._release_references()` 与 `ComDispatcher.shutdown()`）。
- **宿主 safe-delete 拦截**：本机 `unlink()` 被 `sitecustomize` 改为走回收站，
  脚本批量删临时文件会抛 `OSError`。清理逻辑要么用唯一目录，要么容忍失败。
