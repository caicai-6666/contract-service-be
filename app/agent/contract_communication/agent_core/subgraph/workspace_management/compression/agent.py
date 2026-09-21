"""压缩子 Agent 的调用边界；多轮工具循环已接入，不另建 LangGraph。"""
from collections.abc import Awaitable, Callable

from app.schema.communication_workspace import WorkspacePayload
from ..schema import CompressionRequest
from .schema import CompressionResult

CompressionRunner = Callable[[CompressionRequest], Awaitable[WorkspacePayload]]


async def run_compression_tool_loop(request: CompressionRequest, **kwargs) -> WorkspacePayload:
    """延迟加载实际多轮工具循环，保持外围校验入口独立。"""
    from .runtime import run_compression_tool_loop as run_loop
    return await run_loop(request, **kwargs)


def _get(content, path):
    if not path.startswith('/') or any(not part for part in path.split('/')[1:]):
        raise ValueError('保护路径无效')
    for key in path.split('/')[1:]:
        content = content[key]
    return content


def validate_compression_candidate(request: CompressionRequest, raw_candidate) -> WorkspacePayload:
    """每次工具修改和最终返回共用结构、保护项及已有结论状态校验。"""
    candidate = WorkspacePayload.model_validate(raw_candidate).model_copy(deep=True)
    content = candidate.model_dump()
    for path, value in request.protected_values.items():
        if _get(content, path) != value:
            raise ValueError('压缩改变了保护目标或引用')
    if request.reserved_path:
        parent_path, key = request.reserved_path.rsplit('/', 1)
        if key in _get(content, parent_path):
            raise ValueError('压缩占用了新增 ID')
    for key, value in candidate.known_information.items():
        original = request.workspace.known_information.get(key)
        if original and value.status != original.status:
            raise ValueError('压缩不能改变信息确认状态')
    for key, value in candidate.explored_directions.items():
        original = request.workspace.explored_directions.get(key)
        if original is None or value.outcome != original.outcome:
            raise ValueError('压缩不能创建探索成果或改变探索结果')
    return candidate


async def run_workspace_compression_agent(
    request: CompressionRequest | dict, *, compressor: CompressionRunner | None = None,
    counter=None, audit=None,
) -> CompressionResult:
    """校验输入、执行有界子 Agent、验收候选；私有审计与外部反馈分开。"""
    audit = audit if audit is not None else []
    def error(message):
        return CompressionResult(status='error', error_feedback=message, private_audit=audit)
    try:
        request = CompressionRequest.model_validate(request).model_copy(deep=True)
        original = request.workspace.model_dump()
        for path, value in request.protected_values.items():
            if _get(original, path) != value:
                raise ValueError('保护值与原工作区不一致')
        if request.target_tokens != (request.token_budget * 7 - 1) // 10:
            raise ValueError('压缩目标与预算不一致')
    except (ValueError, TypeError, KeyError):
        return error('自动压缩输入或保护约束无效；本次操作未执行。')
    try:
        if compressor is None:
            raw_candidate = await run_compression_tool_loop(request.model_copy(deep=True), counter=counter, audit=audit)
        else:
            raw_candidate = await compressor(request.model_copy(deep=True))
        candidate = validate_compression_candidate(request, raw_candidate)
    except Exception as exc:
        audit.append({'event': 'agent_failed', 'error_type': type(exc).__name__})
        return error('自动压缩执行或验收失败；本次操作未执行，原工作区保持不变。')
    return CompressionResult(status='success', workspace=candidate, private_audit=audit)
