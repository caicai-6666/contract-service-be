"""主助手工具调用组合入口；子 Agent 内部工具不经过这里。"""
from collections.abc import Mapping

from .tool.executor import ToolExecutor, ToolHandler
from .tool.interaction import UserOutputPublisher
from .tool.registry import build_agent_tool_registry
from .tool.progress import ToolProgressPublisher
from .tool.workspace_executor import WorkspaceCommit, WorkspaceReader, WorkspaceToolHandler
from .subgraph.fifo_management.workflow import build_fifo_management_subgraph
from .subgraph.fifo_management.token_count import count_fifo_task_tokens
from .subgraph.fifo_management.compression import summarize_fifo_placeholder, count_fifo_summary_tokens
from .subgraph.workspace_management.workflow import build_workspace_management_subgraph
from .subgraph.workspace_management.token_count import count_workspace_tokens


def build_tool_management_subgraph(
    *, fifo_token_budget: int, workspace_token_budget: int,
    read_workspace: WorkspaceReader, commit_workspace: WorkspaceCommit,
    handlers: Mapping[str, ToolHandler] | None = None, additional_tools=(),
    publish_output: UserOutputPublisher | None = None,
    publish_progress: ToolProgressPublisher | None = None,
    fifo_counter=count_fifo_task_tokens, workspace_counter=count_workspace_tokens,
    fifo_compressor=summarize_fifo_placeholder, workspace_compressor=None,
    summary_counter=count_fifo_summary_tokens, executor: ToolExecutor | None = None, summary_commit=None,
):
    """装配 FIFO 外层和工作区内层。自定义 executor 时必须自行注册工作区适配。"""
    if executor is not None and (handlers is not None or publish_output is not None or publish_progress is not None or additional_tools):
        raise ValueError('executor 不可与 handlers、additional_tools、publish_output 或 publish_progress 同时指定')
    if executor is None:
        workspace_graph = build_workspace_management_subgraph(
            token_budget=workspace_token_budget, counter=workspace_counter, compressor=workspace_compressor)
        workspace_handler = WorkspaceToolHandler(
            graph=workspace_graph, read_workspace=read_workspace, commit=commit_workspace)
        registered = build_agent_tool_registry(workspace_handler=workspace_handler,
            publish_output=publish_output, additional_tools=additional_tools)
        registered.require_supported_content()
        registry = registered.handlers(publish_progress=publish_progress)
        # 仅保留独立图旧调用方的原始 handler 适配；正式模型工具必须完整注册。
        for name, handler in (handlers or {}).items():
            if name in registry:
                raise ValueError(f'已注册工具不可被覆盖：{name}')
            registry[name] = handler
        executor = ToolExecutor(registry)
    return build_fifo_management_subgraph(
        token_budget=fifo_token_budget, counter=fifo_counter, executor=executor,
        compressor=fifo_compressor, summary_counter=summary_counter, summary_commit=summary_commit)
