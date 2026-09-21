"""固定 3:7 配额计算，不读取存储、请求模型或更改管理图状态。"""
from app.core.config import MLLMSettings
from .schema import ContextBudgetRequest, ContextBudgetResult

CONTEXT_BUDGET_POLICY_VERSION = 'agent-core-context-budget-v1'


class ContextBudgetError(ValueError):
    """预算不足，调用者需缩减输入或调整配置，不能继续构造负配额请求。"""


def calculate_context_budget(request: ContextBudgetRequest | dict) -> ContextBudgetResult:
    request = ContextBudgetRequest.model_validate(request)
    fixed = request.system_prompt_tokens + request.tool_definition_tokens + request.message_template_tokens
    return calculate_context_budget_from_fixed_input(
        context_window_tokens=request.context_window_tokens, fixed_input_tokens=fixed,
        output_reserve_tokens=request.output_reserve_tokens,
        management_reserve_tokens=request.management_reserve_tokens, summary_tokens=request.summary_tokens)


def calculate_context_budget_from_fixed_input(*, context_window_tokens, fixed_input_tokens,
                                            output_reserve_tokens, management_reserve_tokens, summary_tokens):
    """聊天分词接口已合计系统、工具和模板开销时，直接使用总量，不虚构分项计数。"""
    values = (context_window_tokens, fixed_input_tokens, output_reserve_tokens, management_reserve_tokens, summary_tokens)
    if any(type(n) is not int or n < 0 for n in values) or min(context_window_tokens, output_reserve_tokens) <= 0:
        raise ContextBudgetError('上下文计数与预留参数非法')
    fixed = fixed_input_tokens
    reserved = output_reserve_tokens + management_reserve_tokens
    dynamic = context_window_tokens - fixed - reserved
    if dynamic <= 0:
        raise ContextBudgetError('固定输入与预留已耗尽上下文，无法分配动态记忆预算')
    workspace = dynamic * 3 // 10
    trajectory = dynamic - workspace
    fifo = trajectory - summary_tokens
    # 不通过夹到 1 或临时借用另一区域来掩盖不足，两个子图都需要正预算。
    if workspace <= 0 or fifo <= 0:
        raise ContextBudgetError('工作区或 FIFO 剩余预算不足，不能创建容量管理图')
    return ContextBudgetResult(
        policy_version=CONTEXT_BUDGET_POLICY_VERSION,
        context_window_tokens=context_window_tokens,
        fixed_input_tokens=fixed, reserved_tokens=reserved, dynamic_tokens=dynamic,
        workspace_budget_tokens=workspace, trajectory_budget_tokens=trajectory,
        summary_tokens=summary_tokens, fifo_budget_tokens=fifo,
    )


def calculate_context_budget_from_settings(
    settings: MLLMSettings, *, system_prompt_tokens: int, tool_definition_tokens: int,
    message_template_tokens: int, management_reserve_tokens: int, summary_tokens: int,
) -> ContextBudgetResult:
    """仅复用明确适用的上下文与生成配置，不挪用合同视觉流程的预留参数。

    固定开销尚未完成计数时应等待装配，不用缺省 0 伪装为可用预算。
    若本次请求覆写生成上限，调用方须使用基础契约传实际输出预留。
    """
    return calculate_context_budget(ContextBudgetRequest(
        context_window_tokens=settings.context_window_tokens,
        output_reserve_tokens=settings.generation.max_completion_tokens,
        system_prompt_tokens=system_prompt_tokens, tool_definition_tokens=tool_definition_tokens,
        message_template_tokens=message_template_tokens,
        management_reserve_tokens=management_reserve_tokens, summary_tokens=summary_tokens,
    ))
