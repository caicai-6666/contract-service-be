"""条款候选发现与完整内容提取的提示词入口。"""

from app.agent.contract_extraction.subgraph.clause_extraction.prompt.content import (
    CLAUSE_CONTENT_COMMON_PROMPT_VERSION,
    CLAUSE_CONTENT_TARGET_PROMPT_VERSION,
    CLAUSE_CONTENT_TOOL_PLACEMENT,
    append_clause_content_prefill_task,
    append_clause_content_target,
    build_clause_content_common_messages,
    build_clause_content_messages,
    render_clause_content_catalog,
    render_clause_content_target,
)
from app.agent.contract_extraction.subgraph.clause_extraction.prompt.discovery import (
    CLAUSE_DISCOVERY_PROMPT_VERSION,
    CLAUSE_DISCOVERY_TOOL_PLACEMENT,
    append_clause_discovery_workspace,
    build_clause_discovery_messages,
    build_clause_discovery_task_messages,
    render_clause_discovery_direction,
    render_clause_discovery_workspace,
)

__all__ = [
    "CLAUSE_CONTENT_COMMON_PROMPT_VERSION",
    "CLAUSE_CONTENT_TARGET_PROMPT_VERSION",
    "CLAUSE_CONTENT_TOOL_PLACEMENT",
    "CLAUSE_DISCOVERY_PROMPT_VERSION",
    "CLAUSE_DISCOVERY_TOOL_PLACEMENT",
    "append_clause_content_prefill_task",
    "append_clause_content_target",
    "append_clause_discovery_workspace",
    "build_clause_content_common_messages",
    "build_clause_content_messages",
    "build_clause_discovery_messages",
    "build_clause_discovery_task_messages",
    "render_clause_content_catalog",
    "render_clause_content_target",
    "render_clause_discovery_direction",
    "render_clause_discovery_workspace",
]
