"""主助手提示词组件；角色定位、系统交互规则与工作区定义独立维护。"""

from .guidance import (
    SYSTEM_GUIDENCE_TEMPLATE,
    SYSTEM_GUIDENCE_TEMPLATE_VERSION,
    SystemGuidenceKind,
    build_system_guidence_message,
)
from .interaction import (
    AGENT_CORE_INTERACTION_PROMPT_TEMPLATE,
    AGENT_CORE_INTERACTION_PROMPT_VERSION,
    build_agent_core_interaction_prompt,
)

from .role import AGENT_CORE_ROLE_PROMPT, AGENT_CORE_ROLE_PROMPT_VERSION
from .workspace import AGENT_CORE_WORKSPACE_PROMPT, AGENT_CORE_WORKSPACE_PROMPT_VERSION

from .fifo import AGENT_CORE_FIFO_PROMPT, AGENT_CORE_FIFO_PROMPT_VERSION

__all__ = [
    "AGENT_CORE_FIFO_PROMPT",
    "AGENT_CORE_FIFO_PROMPT_VERSION",
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
