"""单条款完整直接内容提取的稳定公共任务与动态目标提示词。"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any, Final

import yaml

from app.agent.contract_extraction.context import append_contract_task
from app.agent.contract_extraction.state import ContractPrefillContext
from app.agent.contract_extraction.subgraph.clause_extraction.tool import (
    ClauseCandidateWorkspaceItem,
)
from app.agent.contract_extraction.tool_protocol import TOOL_CALL_XML_INSTRUCTION

CLAUSE_CONTENT_COMMON_PROMPT_VERSION: Final = "clause-content-common-v9"
CLAUSE_CONTENT_TARGET_PROMPT_VERSION: Final = "clause-content-target-v4"
CLAUSE_CONTENT_TOOL_PLACEMENT: Final = "before_task"

CATALOG_BEGIN: Final = "===== 条款候选目录：开始 ====="
CATALOG_END: Final = "===== 条款候选目录：结束 ====="
TARGET_BEGIN: Final = "===== 当前唯一条款：开始 ====="
TARGET_END: Final = "===== 当前唯一条款：结束 ====="

CLAUSE_CATALOG_COMMENTS: Final = """# 程序已经校验并冻结的条款候选目录；这是正文边界和父子关系的权威输入。
# 字段说明：
# candidate_id：程序生成的稳定候选 ID；详情结果通过它关联本目录和 PDF 证据。
# order：候选在整份合同中的原始发现顺序。
# identifier / title_hint：页面可见的原始编号和可空主题提示；不得据此补写正文。
# document_path / level：原合同从最外层到当前候选的完整路径和绝对深度；包含未提取正文的纯标题。
# parent_candidate_id：路径上最近的已记录正文祖先；null 不代表当前候选一定处于原合同顶层。
# evidence.start：属于当前候选的包含式起始物理页码和原文 anchor。
# evidence.end：属于当前候选自身末尾的包含式物理页码和原文 anchor；不得为空。
# 目录只负责定位和排除重叠；原始 PDF 页面仍是正文内容的唯一事实来源。"""

CLAUSE_TARGET_COMMENTS: Final = """# 当前任务只处理一个候选；不得提取、修正或合并其他候选。
# 字段说明：
# candidate：当前唯一候选的完整程序记录；必须同时包含其 start 和 end anchor。"""

_CLAUSE_CONTENT_COMMON_TASK_BASE: Final = """你已获得当前合同的原始 PDF、文档导航结构、分类结果和程序冻结的完整条款候选目录。当前任务是在收到唯一候选后，从原始 PDF 提取该候选的完整直接原文；唯一候选会在独立材料中明确指定。

事实与权限边界：
1. 原始 PDF 页面是正文字符、标点、换行、项目符号和表格内容的唯一事实来源。
2. 条款候选目录是候选身份、层级、父子关系和起止边界的权威来源；不得新增、删除、合并、拆分、重排或修正候选。
3. 文档结构和合同分类只用于导航与理解语境，不能覆盖 PDF 原文或候选边界。
4. 当前没有获得 Core、Attribute 或摘要结果，也不需要这些信息；不得使用文件名、模板、常识或法律知识补全文字。

完整直接内容定义：
1. 叶子候选：从 evidence.start 的包含式 anchor 开始，提取至 evidence.end 的包含式 anchor；start 和 end 都属于当前候选并必须出现在 content 中。
2. 父候选：保留父候选自己的编号、标题、引导语以及不属于任何子孙候选的独立正文；排除目录中全部子孙候选的编号、标题和正文，避免父子结果重复。
3. 父候选的直接正文可能分布在子条款之前或之后。按 PDF 原始阅读顺序保留这些直接片段，片段之间使用一个空行连接，不添加“已省略子条款”等非原文标记。
4. 候选目录只包含已经确认具有自身直接正文的候选；不要用子条款、相邻文字或常识填充当前候选，也不要把目录中的结构关系误当成正文。

原文保真规则：
1. content 必须包含当前候选的起始 anchor，并保持原始编号、标题、字符、金额、日期、大小写、标点、段落顺序和换行。
2. 不摘要、不改写、不翻译、不纠错、不统一术语、不规范化数字或日期，也不添加解释、Markdown 标题符号、引号或代码围栏。
3. 普通段落使用换行保持原排版；项目符号保留页面可见符号。表格关系清晰时使用简洁 Markdown 表格；无法可靠恢复列关系时按页面阅读顺序逐行转写，不能猜测单元格归属。
4. 页面中的表单横线、填写线以及文字下方的下划线样式均只属于版式，不是正文字符：无论横线上方是空白还是已有文字，都不得把横线或其中某一段转写成 `_`。只有原文明确以独立字符形式书写的下划线符号才可保留。例如，页面显示“即人民币〔连续填写线，后段线上印有 55000 元整〕”时，正确正文是“即人民币 55000 元整”，禁止输出“即人民币_55000 元整”。
5. 页眉、页脚、页码、水印、印章和手写签名只有确实属于当前条款正文时才保留；不能把页面装饰或签署资料混入条款。
6. 局部字符因遮挡、扫描质量或裁切确实无法辨认时，在原位置写入“〔无法辨认〕”，并在 reasoning_summary 说明所在物理页和影响范围；禁止猜测缺失文字。不得把整条 content 写成缺失标记或放弃占位值。

工具协议：
1. 每轮必须且只能调用当前提供的 extract_clause_content，禁止输出普通文本或调用其他工具。
2. 程序已经提供候选证据，工具参数不再接收 evidence；不要重复提交页码、anchor、candidate_id、层级或父级。
3. reasoning_summary 位于最终正文之前，只简洁说明如何应用包含式起止锚点、跨页连续性、表格处理和子孙排除，以及是否存在无法辨认内容；不复制完整原文或输出隐式思维草稿。
4. content 是唯一正式正文。工具返回 ok=false 时，只按反馈修正对应位置并重新调用，不改变候选或创造新的输出字段。

提交前检查：
1. content 从当前候选起始 anchor 开始，且没有漏掉候选自己的直接正文。
2. 当前候选自身的结束 anchor 已包含，且没有继续提取相邻候选或非条款区域。
3. 父候选 content 不含任何 descendants 的起始 anchor 或正文；叶子候选没有擅自排除合法内容。
4. 输出只含 PDF 可见原文和必要的局部“〔无法辨认〕”标记，没有摘要、解释、补全或其他候选内容。"""
CLAUSE_CONTENT_COMMON_TASK: Final = (
    f"{_CLAUSE_CONTENT_COMMON_TASK_BASE}\n\n工具调用格式：\n"
    f"{TOOL_CALL_XML_INSTRUCTION}"
)

CLAUSE_CONTENT_PREFILL_TASK: Final = """你已获得当前合同、条款原文提取规则、完整候选目录和 extract_clause_content 工具定义，但尚未指定需要处理的唯一候选。请先读取已有材料，并准备在收到具体候选后提取其完整直接原文；现在不要提取任何条款正文。"""


class _IndentedSafeDumper(yaml.SafeDumper):
    """让 YAML 列表相对父键缩进，保持候选目录易读。"""

    def increase_indent(
        self,
        flow: bool = False,
        indentless: bool = False,
    ) -> None:
        del indentless
        super().increase_indent(flow, False)


def _serialize_yaml(*, comments: str, root_key: str, payload: Any) -> str:
    """生成字段顺序稳定且带结构注释的模型可读 YAML。"""
    serialized = yaml.dump(
        {root_key: payload},
        Dumper=_IndentedSafeDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    ).rstrip()
    return f"{comments.rstrip()}\n{serialized}"


def _validate_catalog(
    candidates: tuple[ClauseCandidateWorkspaceItem, ...],
) -> None:
    """拒绝无法稳定序列化或引用的候选目录。"""
    if not candidates:
        raise ValueError("单条款详情提示词需要非空候选目录")
    expected_orders = tuple(range(1, len(candidates) + 1))
    if tuple(item.order for item in candidates) != expected_orders:
        raise ValueError("条款候选 order 必须从 1 开始连续递增")
    expected_ids = tuple(f"clause-{order:04d}" for order in expected_orders)
    if tuple(item.candidate_id for item in candidates) != expected_ids:
        raise ValueError("条款候选 candidate_id 必须与程序顺序一致")


def _candidate_payload(candidate: ClauseCandidateWorkspaceItem) -> dict[str, Any]:
    """生成不包含运行审计的候选模型投影。"""
    return candidate.model_dump(mode="json")


def render_clause_content_catalog(
    candidates: tuple[ClauseCandidateWorkspaceItem, ...],
) -> str:
    """渲染所有并发详情请求共享的完整候选目录。"""
    _validate_catalog(candidates)
    catalog = _serialize_yaml(
        comments=CLAUSE_CATALOG_COMMENTS,
        root_key="clause_candidates",
        payload=[_candidate_payload(candidate) for candidate in candidates],
    )
    return f"{CATALOG_BEGIN}\n{catalog}\n{CATALOG_END}"


def render_clause_content_target(
    candidate: ClauseCandidateWorkspaceItem,
    candidates: tuple[ClauseCandidateWorkspaceItem, ...],
) -> str:
    """渲染只在单候选并发请求尾部变化的当前目标。"""
    _validate_catalog(candidates)
    items_by_id = {item.candidate_id: item for item in candidates}
    if items_by_id.get(candidate.candidate_id) != candidate:
        raise ValueError(
            f"当前候选 {candidate.candidate_id} 不在候选目录中或内容不一致"
        )
    payload = {"candidate": _candidate_payload(candidate)}
    target = _serialize_yaml(
        comments=CLAUSE_TARGET_COMMENTS,
        root_key="current_clause",
        payload=payload,
    )
    instruction = (
        f"只提取 {candidate.candidate_id} 的完整直接原文。读取原始 PDF 核对内容，"
        "先在 reasoning_summary 中说明边界应用，再调用 extract_clause_content "
        "提交 content。"
    )
    return f"{TARGET_BEGIN}\n{target}\n\n下一步：{instruction}\n{TARGET_END}"


def build_clause_content_common_messages(
    prefill_context: ContractPrefillContext,
    candidates: tuple[ClauseCandidateWorkspaceItem, ...],
) -> list[dict[str, Any]]:
    """在最终合同前缀尾部追加共享任务规则和完整候选目录。"""
    task = f"{CLAUSE_CONTENT_COMMON_TASK}\n\n{render_clause_content_catalog(candidates)}"
    return append_contract_task(
        prefill_context.messages,
        task_suffix=task,
    )


def append_clause_content_target(
    common_messages: Iterable[dict[str, Any]],
    candidate: ClauseCandidateWorkspaceItem,
    candidates: tuple[ClauseCandidateWorkspaceItem, ...],
) -> list[dict[str, Any]]:
    """在稳定公共任务之后以独立 user 消息追加当前唯一候选。"""
    messages = deepcopy(list(common_messages))
    if not messages:
        raise ValueError("单条款详情公共任务消息不能为空")
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": render_clause_content_target(candidate, candidates),
                }
            ],
        }
    )
    return messages


def append_clause_content_prefill_task(
    common_messages: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """追加不属于共享前缀的预热动作消息，供节点二带工具请求。"""
    messages = deepcopy(list(common_messages))
    if not messages:
        raise ValueError("单条款详情公共任务消息不能为空")
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": CLAUSE_CONTENT_PREFILL_TASK,
                }
            ],
        }
    )
    return messages


def build_clause_content_messages(
    prefill_context: ContractPrefillContext,
    candidate: ClauseCandidateWorkspaceItem,
    candidates: tuple[ClauseCandidateWorkspaceItem, ...],
) -> list[dict[str, Any]]:
    """构造“最终前缀 → 共享任务/目录 → 当前候选”的完整初始消息。"""
    return append_clause_content_target(
        build_clause_content_common_messages(prefill_context, candidates),
        candidate,
        candidates,
    )


__all__ = [
    "CATALOG_BEGIN",
    "CATALOG_END",
    "CLAUSE_CONTENT_COMMON_PROMPT_VERSION",
    "CLAUSE_CONTENT_TARGET_PROMPT_VERSION",
    "CLAUSE_CONTENT_TOOL_PLACEMENT",
    "TARGET_BEGIN",
    "TARGET_END",
    "append_clause_content_prefill_task",
    "append_clause_content_target",
    "build_clause_content_common_messages",
    "build_clause_content_messages",
    "render_clause_content_catalog",
    "render_clause_content_target",
]
