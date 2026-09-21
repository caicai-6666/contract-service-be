"""主助手上下文展示组件；与自动摘要子 Agent 的输入展示分别维护。"""
from .summary import SUMMARY_RENDER_VERSION, render_summary_section

__all__ = ['SUMMARY_RENDER_VERSION', 'render_summary_section']

from .workspace import WORKSPACE_SECTION_RENDER_VERSION, render_workspace_section

__all__ += ['WORKSPACE_SECTION_RENDER_VERSION', 'render_workspace_section']

from .task import (
    TASK_RENDER_VERSION, TaskAttachment, TaskUserInput, TaskTraceEntry,
    TaskFinalOutput, TaskRenderInput, render_task_section,
)

__all__ += ['TASK_RENDER_VERSION', 'TaskAttachment', 'TaskUserInput', 'TaskTraceEntry',
            'TaskFinalOutput', 'TaskRenderInput', 'render_task_section']

from .task import render_task_input

__all__.append('render_task_input')

from .page import PAGE_DISPLAY_RENDER_VERSION, render_tool_page_messages

__all__ += ['PAGE_DISPLAY_RENDER_VERSION', 'render_tool_page_messages']
