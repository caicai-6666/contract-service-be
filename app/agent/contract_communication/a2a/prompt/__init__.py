"""A2A 稳定系统提示词组合。"""
from ...agent_core.prompt import (AGENT_CORE_WORKSPACE_PROMPT, AGENT_CORE_FIFO_PROMPT,
    build_agent_core_interaction_prompt)
from .role import A2A_ROLE
from .interaction import A2A_OUTPUT_RULES


def build_system_prompt(template):
    return '\n\n'.join((A2A_ROLE,
        build_agent_core_interaction_prompt(tool_call_template=template, output_rules=A2A_OUTPUT_RULES),
        AGENT_CORE_WORKSPACE_PROMPT, AGENT_CORE_FIFO_PROMPT))
