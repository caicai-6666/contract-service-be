"""探索模式的稳定系统提示词组合，保持用户交互规则。"""
from ...agent_core.prompt import (AGENT_CORE_ROLE_PROMPT, AGENT_CORE_WORKSPACE_PROMPT,
    AGENT_CORE_FIFO_PROMPT, build_agent_core_interaction_prompt)
from .interaction import EXPLORATION_OUTPUT_RULES


def build_system_prompt(template):
    return '\n\n'.join((AGENT_CORE_ROLE_PROMPT,
        build_agent_core_interaction_prompt(tool_call_template=template, output_rules=EXPLORATION_OUTPUT_RULES),
        AGENT_CORE_WORKSPACE_PROMPT, AGENT_CORE_FIFO_PROMPT))
