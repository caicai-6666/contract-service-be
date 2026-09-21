"""上下文预算契约；只接受程序提供的实际计数和明确预留，不接受模型估算。"""
from pydantic import BaseModel, ConfigDict, Field


class BudgetObject(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)


class ContextBudgetRequest(BudgetObject):
    context_window_tokens: int = Field(gt=0, description='配置的模型最大上下文，包含输入和输出；须与实际部署一致。')
    system_prompt_tokens: int = Field(ge=0, description='完整稳定系统提示词的 token 数，不含工作区、FIFO 或动态 system-guidence。')
    tool_definition_tokens: int = Field(ge=0, description='当前实际注入的全部工具定义开销，不含已计入系统提示词的内容。')
    message_template_tokens: int = Field(ge=0, description='消息角色、分隔符、聊天模板及其他未在内容计数中覆盖的输入开销，不能重复计数。')
    output_reserve_tokens: int = Field(gt=0, description='本次模型生成的最大输出预留，来自实际请求配置。')
    management_reserve_tokens: int = Field(ge=0, description='为整理调用、反馈和计数误差明确预留的余量；不重复包含最大输出预留。')
    summary_tokens: int = Field(ge=0, description='当前唯一累计摘要及其独占包装的 token 数，从轨迹区配额中扣除；无摘要时显式传 0。')


class ContextBudgetResult(BudgetObject):
    policy_version: str = Field(description='预算策略版本，供程序审计和上下文重建追踪。')
    context_window_tokens: int = Field(gt=0, description='计算采用的配置上下文上限。')
    fixed_input_tokens: int = Field(ge=0, description='系统提示词、工具定义与消息模板开销之和。')
    reserved_tokens: int = Field(gt=0, description='输出预留与整理预留之和。')
    dynamic_tokens: int = Field(gt=0, description='扣除固定输入和预留后的动态记忆总预算。')
    workspace_budget_tokens: int = Field(gt=0, description='动态预算的 30%，整数向下取整；工作区子图 token_budget。')
    trajectory_budget_tokens: int = Field(gt=0, description='动态预算扣除工作区配额后的 70% 侧配额，包含累计摘要和 FIFO。')
    summary_tokens: int = Field(ge=0, description='已占用轨迹区的累计摘要 token 数。')
    fifo_budget_tokens: int = Field(gt=0, description='轨迹区配额扣除累计摘要后的 FIFO 真实预算；内部 95% 阈值不在此扣除。')
