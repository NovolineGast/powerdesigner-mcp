"""MCP Prompts - ready-made workflows for database course design."""

from __future__ import annotations

from mcp.server.fastmcp.prompts import base as prompt_base


def register(mcp, backend) -> None:
    @mcp.prompt(name="database_design_workflow",
                description=("数据库课程设计全流程工作流：需求 → CDM → LDM → PDM → "
                             "约束/索引 → 校验 → DDL。参数：需求描述与模型文件路径（可选）。"))
    def database_design_workflow(requirement: str, model_path: str = "") -> list:
        open_step = (f"先用 open_model 打开已有模型 {model_path}，"
                     if model_path else "先确认 PowerDesigner 已连接（get_server_info），"
                     "如需新建模型用 create_model，")
        text = f"""你是数据库建模工程师，负责把下面的课程设计需求落成完整的 PowerDesigner 模型。

【需求】
{requirement}

请严格按以下闭环执行（可配合 powerdesigner MCP 的工具）：

1. 连接与环境
   - {open_step}用 get_server_info 确认连接。
2. 概念设计（CDM）
   - 从需求中识别实体、属性、标识符、联系（1:1 / 1:N / M:N）。
   - 用 design_from_spec(kind=CDM) 一次性物化，或逐个 create_table/create_reference。
3. 逻辑设计（LDM）
   - convert_cdm_to_ldm；解决 M:N 联系（拆出关联实体并迁移双方主键）。
4. 物理设计（PDM）
   - convert_cdm_to_pdm（选择 DBMS，如 MySQL 5.0）；核对数据类型与命名（snake_case）。
5. 完整性约束与索引
   - 检查每张表主键（create_primary_key）、外键（create_reference，自动迁移 FK 列）、
     为高频查询字段建索引（create_index，命名 idx_表_列）。
6. 规范检查（闭环）
   - validate_model 修复全部 error；
   - check_database_design 传入课程要求的规则（如：所有表必须有主键/注释、
     表名 snake_case、外键命名 fk_），按 violations 逐条修复后复检。
7. 交付
   - generate_ddl 生成 SQL；save_model 保存模型；
   - 输出统计（表/列/主键/外键/索引数量）与检查结果汇总。

每一步之后都先 dry_run 或 validate，再实际执行。"""
        return [prompt_base.Message(role="user", content=prompt_base.TextContent(type="text", text=text))]

    @mcp.prompt(name="schema_review",
                description="对现有模型做一次评审：结构校验 + 自定义规范规则 + 修复建议。")
    def schema_review(model_id: str, rules: str = "") -> list:
        text = f"""请对模型 {model_id} 做完整评审：

1. inspect_schema(mode='detail') 通读结构。
2. validate_model 列出全部 error/warning。
3. 若给出规则，则 check_database_design(rules={rules or '[]'}) 执行规范检查。
4. 对每条问题给出：位置、原因、修复方案（具体到工具调用与参数）。
5. 修复后复检，输出最终统计。"""
        return [prompt_base.Message(role="user", content=prompt_base.TextContent(type="text", text=text))]
