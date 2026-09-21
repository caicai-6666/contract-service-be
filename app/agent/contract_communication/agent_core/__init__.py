"""合同沟通主助手：提供角色与交互规则初稿，双层工具管理已装配，主模型循环与上下文组装尚未接入。"""

from .prompt import (
    SYSTEM_GUIDENCE_TEMPLATE,
    SYSTEM_GUIDENCE_TEMPLATE_VERSION,
    SystemGuidenceKind,
    build_system_guidence_message,
    AGENT_CORE_INTERACTION_PROMPT_TEMPLATE,
    AGENT_CORE_INTERACTION_PROMPT_VERSION,
    build_agent_core_interaction_prompt,
    AGENT_CORE_ROLE_PROMPT,
    AGENT_CORE_ROLE_PROMPT_VERSION,
    AGENT_CORE_WORKSPACE_PROMPT,
    AGENT_CORE_WORKSPACE_PROMPT_VERSION,
)

from .tool import build_workspace_tools, execute_workspace_tool

from .prompt.fifo import AGENT_CORE_FIFO_PROMPT, AGENT_CORE_FIFO_PROMPT_VERSION

__all__ = [
    "AGENT_CORE_FIFO_PROMPT",
    "AGENT_CORE_FIFO_PROMPT_VERSION",
    "build_workspace_tools",
    "execute_workspace_tool",
    "AGENT_CORE_ROLE_PROMPT",
    "AGENT_CORE_ROLE_PROMPT_VERSION",
    "AGENT_CORE_WORKSPACE_PROMPT",
    "AGENT_CORE_WORKSPACE_PROMPT_VERSION",
    "AGENT_CORE_INTERACTION_PROMPT_TEMPLATE",
    "AGENT_CORE_INTERACTION_PROMPT_VERSION",
    "build_agent_core_interaction_prompt",
    "SYSTEM_GUIDENCE_TEMPLATE",
    "SYSTEM_GUIDENCE_TEMPLATE_VERSION",
    "SystemGuidenceKind",
    "build_system_guidence_message",
]

from .tool_management import build_tool_management_subgraph

__all__.append("build_tool_management_subgraph")
