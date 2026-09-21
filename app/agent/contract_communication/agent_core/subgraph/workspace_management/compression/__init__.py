"""自动压缩子 Agent 入口，候选由父图继续验收。"""
from .schema import CompressionResult
from .agent import run_workspace_compression_agent

__all__ = ['CompressionResult', 'run_workspace_compression_agent']

from .prompt import (
    WORKSPACE_COMPRESSION_PROMPT_VERSION,
    build_workspace_compression_prompt,
    build_workspace_compression_task_prompt,
)

__all__ += [
    'WORKSPACE_COMPRESSION_PROMPT_VERSION',
    'build_workspace_compression_prompt',
    'build_workspace_compression_task_prompt',
]
