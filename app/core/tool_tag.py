"""启动期加载工具格式模板，供当前进程共享只读文本。"""

from app.core.config import MLLMSettings

# 不在导入时读取文件；使用 getter 避免 from-import 捕获启动前的 None。
_MLLM_TOOL_TAG_TEMPLATE: str | None = None


def initialize_mllm_tool_tag(settings: MLLMSettings) -> str:
    """由应用启动入口调用；完整校验成功后才发布新的内存快照。"""
    global _MLLM_TOOL_TAG_TEMPLATE
    _MLLM_TOOL_TAG_TEMPLATE = None
    path = settings.tool_tag_path
    try:
        if path.resolve().parent != path.parent.resolve():
            raise ValueError("MLLM 工具调用模板不能链接到模板目录之外")
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"无法读取 MLLM 工具调用模板：{path}") from exc
    if not content.strip():
        raise ValueError(f"MLLM 工具调用模板不能为空：{path}")
    # 只去掉文本文件的一个末尾换行，保持既有提示词正文及内部空白不变。
    _MLLM_TOOL_TAG_TEMPLATE = content.removesuffix("\n")
    return _MLLM_TOOL_TAG_TEMPLATE


def get_mllm_tool_tag() -> str:
    """读取已初始化的全局模板；不隐式读盘，也不回退到硬编码文本。"""
    if _MLLM_TOOL_TAG_TEMPLATE is None:
        raise RuntimeError("MLLM 工具调用模板尚未在应用启动时初始化")
    return _MLLM_TOOL_TAG_TEMPLATE
