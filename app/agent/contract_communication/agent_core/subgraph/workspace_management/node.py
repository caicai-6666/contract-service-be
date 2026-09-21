"""预演、容量分支、压缩子 Agent 调用与统一返回；所有编辑都只作用于副本。"""
from collections.abc import Awaitable, Callable
from copy import deepcopy
from inspect import isawaitable

from pydantic import ValidationError
from app.schema.communication_workspace import WorkspacePayload, WorkspaceSnapshot
from ...prompt.guidance import build_system_guidence_message
from ...tool.workspace import prepare_workspace_edit
from .schema import WorkspaceManagementRequest, WorkspaceManagementResult, CompressionRequest
from .compression import run_workspace_compression_agent

TokenCounter = Callable[[WorkspacePayload], int | Awaitable[int]]
WorkspaceCompressor = Callable[[CompressionRequest], Awaitable[WorkspacePayload]]


async def _count(workspace, counter: TokenCounter | None):
    if counter is None:
        raise RuntimeError('未配置工作区 token 计数器')
    count = counter(workspace.model_copy(deep=True))
    if isawaitable(count):
        count = await count
    if type(count) is not int or count < 0:
        raise ValueError('token 计数必须为非负整数')
    return count


def _get(payload, path):
    value = payload
    for key in path.split('/')[1:]:
        value = value[key]
    return value


def _parent(payload, path):
    parts = path.split('/')[1:]
    parent = payload
    for key in parts[:-1]:
        parent = parent[key]
    return parent, parts[-1]


async def estimate_modified_tokens(state, *, counter=None):
    """节点 1：完整校验并预演操作，冻结新增 ID，计算预计修改后的容量。"""
    try:
        request = WorkspaceManagementRequest.model_validate({
            'workspace': state['workspace'], 'operation': state['operation'],
        })
        original = request.workspace.model_copy(deep=True)
        preview = prepare_workspace_edit(WorkspaceSnapshot(payload=original, revision=0, updated_at=0),
                                         request.operation.name, request.operation.arguments)
    except (ValueError, TypeError, KeyError) as exc:
        # 不回显无效参数或模型原文，正式循环后续可按字段提炼更细的最小反馈。
        location = ''
        if isinstance(exc, ValidationError) and exc.errors():
            location = '（字段：' + '.'.join(map(str, exc.errors()[0]['loc'])) + '）'
        return {'branch': 'error', 'error': f'工作区操作未被接受{location}，请检查目标位置、参数类型和引用；本次未修改工作区。'}
    try:
        count = await _count(preview.payload, counter)
    except Exception:
        return {'branch': 'error', 'error': '工作区 token 计数不可用，本次操作未执行；请等待系统恢复计数能力。'}
    return {'original': original, 'pending_operation': request.operation, 'preview': preview,
            'projected_tokens': count, 'error': None}


def choose_capacity_branch(state, *, token_budget):
    """节点 2：100% 优先；严格超过 80% 才走提醒分支，避免浮点边界漂移。"""
    if state.get('error'):
        return {'branch': 'error'}
    tokens = state['projected_tokens']
    branch = 'full' if tokens >= token_budget else ('warning' if tokens * 5 > token_budget * 4 else 'normal')
    return {'branch': branch}


def route_capacity(state):
    return state['branch']


def _compression_request(state, token_budget):
    original = state['original'].model_dump()
    preview = state['preview']
    operation = state['pending_operation']
    protected = {}
    reserved = None
    if operation.name == 'workspace_add':
        if preview.removed_path:
            protected[preview.removed_path] = deepcopy(_get(original, preview.removed_path))
        else:
            reserved = preview.path
    else:
        parts = preview.path.split('/')[1:]
        # 修改某个字段时保留其整个条目，防止压缩先改变同一对象的其他语义。
        target = preview.path if parts[0] == 'task_constraints' else '/' + '/'.join(parts[:2])
        protected[target] = deepcopy(_get(original, target))
    # 本次写入的探索结果依赖的信息也必须保留，不能压缩后形成悬空引用。
    if operation.name != 'workspace_delete' and preview.path.startswith('/explored_directions/'):
        direction_id = preview.path.split('/')[2]
        for info_id in preview.payload.explored_directions[direction_id].information_ids:
            path = f'/known_information/{info_id}'
            protected[path] = deepcopy(_get(original, path))
    return CompressionRequest(workspace=state['original'].model_copy(deep=True), operation=operation,
        protected_values=protected, reserved_path=reserved, token_budget=token_budget,
        target_tokens=(token_budget * 7 - 1) // 10)


async def compress_before_modification(state, *, token_budget, compressor=None, counter=None):
    """满容量分支调用压缩子 Agent，成功候选交给后续应用节点。"""
    try:
        request = _compression_request(state, token_budget)
        result = await run_workspace_compression_agent(request, compressor=compressor, counter=counter)
    except Exception:
        return {'error': '自动压缩子 Agent 执行失败；本次操作未执行，原工作区保持不变。'}
    if result.status == 'error':
        return {'error': result.error_feedback, 'compression_audit': result.private_audit}
    try:
        # 70% 验收只计算压缩副本，给随后应用原操作预留空间。
        compressed_tokens = await _count(result.workspace, counter)
        if compressed_tokens > request.target_tokens:
            return {'error': '自动压缩副本未低于 70%；本次操作未执行，原工作区保持不变。', 'compression_audit': result.private_audit}
    except Exception:
        return {'error': '自动压缩后的 token 计数不可用；本次操作未执行。', 'compression_audit': result.private_audit}
    return {'compression_request': request, 'compressed': result.workspace.model_copy(deep=True), 'compression_audit': result.private_audit}


async def apply_after_compression(state, *, token_budget, counter=None):
    """将预演操作的既定修改应用到压缩候选，不重新生成 ID，也不原地覆盖输入。"""
    if state.get('error'):
        return {}
    try:
        preview = state['preview']
        candidate = state['compressed'].model_dump()
        if preview.removed_path:
            parent, key = _parent(candidate, preview.removed_path)
            del parent[key]
        if state['pending_operation'].name != 'workspace_delete':
            parent, key = _parent(candidate, preview.path)
            parent[key] = deepcopy(_get(preview.payload.model_dump(), preview.path))
        candidate = WorkspacePayload.model_validate(candidate)
        count = await _count(candidate, counter)
        if count * 5 >= token_budget * 4:
            raise ValueError('压缩后应用操作未达到严格低于 80% 的目标')
    except Exception:
        return {'error': '压缩后无法安全应用本次操作，或最终容量未低于 80%；本次未修改工作区。'}
    return {'candidate': candidate, 'final_tokens': count}


def apply_without_compression(state):
    """未满容量统一应用预演结果，保留 branch 标记供最终节点决定是否提醒。"""
    # LangGraph 合并节点增量状态，未返回的 normal/warning 标记会继续保留。
    return {'candidate': state['preview'].payload.model_copy(deep=True), 'final_tokens': state['projected_tokens']}


def build_final_result(state, *, token_budget):
    """按真实分支统一返回：错误不携带候选，成功提示供外部提交后写入任务轨迹。"""
    if state.get('error'):
        result = WorkspaceManagementResult(status='error', error_feedback=state['error'])
    else:
        branch, count = state['branch'], state['final_tokens']
        usage = f'{count}/{token_budget} token（{count / token_budget:.1%}）'
        guidance = None
        if branch == 'warning':
            guidance = build_system_guidence_message(kind='context_capacity',
                reason=f'本次工作区操作已生成有效结果，使用率为 {usage}，超过 80% 提醒阈值。',
                required_action='优先使用工作区工具主动整理至低于 80%；整理期间暂停业务探索，根据每次成功工具反馈中的使用量判断是否达标，达标后继续任务。')
        elif branch == 'full':
            guidance = build_system_guidence_message(kind='context_capacity',
                reason=f'预计修改后为 {state["projected_tokens"]}/{token_budget} token，达到 100% 容量上限，系统已触发自动压缩。已保留操作目标，先压缩再应用本次修改；最终为 {usage}，已低于 80%。',
                required_action='自动压缩已达到低于 80% 的整理目标，可根据更新后的工作区继续任务；不要重放本次操作、重复已完成探索或再次触发同一次自动压缩。')
        # 容量事实随普通工具结果返回；仅告警和自动压缩另发系统提示，避免重复占用轨迹。
        # 此处只构造候选反馈，调用方必须在版本校验和提交成功后才交给模型。
        result = WorkspaceManagementResult(status='success', workspace=state['candidate'],
            tool_feedback=f'工作区修改成功。当前使用量：{usage}。', system_guidence=guidance)
    return result.model_dump()
