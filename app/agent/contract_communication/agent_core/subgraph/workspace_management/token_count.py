"""工作区文本的统一渲染和接口计数；动态上下文应复用同一渲染函数。"""

import json

from app.core.config import MLLMSettings, get_settings
from app.infrastructure.vllm_tokenizer import count_text_tokens
from app.schema.communication_workspace import WorkspacePayload


WORKSPACE_RENDER_VERSION = 'workspace-json-v1'


def render_workspace(workspace: WorkspacePayload) -> str:
    """保留中文及条目顺序，固定两空格缩进；不含消息外壳、版本及更新时间。"""
    return json.dumps(workspace.model_dump(mode='json'), ensure_ascii=False, indent=2, allow_nan=False)


async def count_workspace_tokens(workspace: WorkspacePayload, *, settings: MLLMSettings | None = None) -> int:
    """调用时才读取配置和创建连接，图装配阶段不访问远端服务。"""
    return await count_text_tokens(render_workspace(workspace), settings or get_settings().mllm)
