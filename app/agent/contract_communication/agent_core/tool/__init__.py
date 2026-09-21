"""主助手工具包：按能力组织实现，通过包入口统一导出。"""

from .workspace import (
    ExplorationResult,
    WorkspaceAddArguments,
    WorkspaceDeleteArguments,
    WorkspaceEdit,
    WorkspacePath,
    WorkspaceReplaceArguments,
    build_workspace_tools,
    execute_workspace_tool,
    parse_workspace_tool_arguments,
    prepare_workspace_edit,
)

__all__ = [
    "ExplorationResult",
    "WorkspaceAddArguments",
    "WorkspaceDeleteArguments",
    "WorkspaceEdit",
    "WorkspacePath",
    "WorkspaceReplaceArguments",
    "build_workspace_tools",
    "execute_workspace_tool",
    "parse_workspace_tool_arguments",
    "prepare_workspace_edit",
]

from .interaction import (
    EmitProgressArguments, FinishTaskArguments, UserOutput, UserOutputReceipt,
    UserOutputRejected, build_interaction_tools, build_interaction_handlers,
    parse_interaction_tool_arguments,
)

__all__ += ["EmitProgressArguments", "FinishTaskArguments", "UserOutput", "UserOutputReceipt",
            "UserOutputRejected", "build_interaction_tools", "build_interaction_handlers",
            "parse_interaction_tool_arguments"]

from .registry import RegisteredTool, ToolRegistry, build_agent_tool_registry

__all__ += ["RegisteredTool", "ToolRegistry", "build_agent_tool_registry"]

from app.schema.agent_tool_content import OrdinaryToolContent, FoldableToolContent, ToolPageReference

__all__ += ["OrdinaryToolContent", "FoldableToolContent", "ToolPageReference"]

from .progress import ToolProgress

__all__ += ["ToolProgress"]

from .file_viewer import FileDescriptor, FileModel, FileModelCache, FileModelKey, FileReader, FileResolver, FileViewer, read_contract_file, read_session_file

__all__ += ["FileDescriptor", "FileModel", "FileModelCache", "FileModelKey", "FileReader", "FileResolver", "FileViewer", "read_contract_file", "read_session_file"]

from .file_viewer import CachedFileInfo, FileCacheBusyError, FileCacheFullError

__all__ += ["CachedFileInfo", "FileCacheBusyError", "FileCacheFullError"]

from .file_viewer import FileViewError

__all__ += ["FileViewError"]

from .file_viewer import ViewSessionFileArguments, ViewContractFileArguments

__all__ += ["ViewSessionFileArguments", "ViewContractFileArguments"]

from .file_viewer import build_file_view_tools

__all__ += ["build_file_view_tools"]

from .external_expert import (
    AskExternalExpertArguments, ContinueExternalExpertArguments, build_external_expert_tools,
    build_external_expert_system_prompt, render_external_expert_reply,
)

__all__ += ["AskExternalExpertArguments", "ContinueExternalExpertArguments", "build_external_expert_tools",
            "build_external_expert_system_prompt", "render_external_expert_reply"]

from .external_expert import ExternalExpertSessionPool
from .external_expert import build_external_expert_registrations

__all__ += ["ExternalExpertSessionPool", "build_external_expert_registrations"]
