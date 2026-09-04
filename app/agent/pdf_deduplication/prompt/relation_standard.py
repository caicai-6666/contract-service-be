"""两份合同文档关系判断共同遵循的稳定业务标准。"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any, Final, Literal

ContractRelationStandardVersion = Literal["contract-relation-standard-v2"]

CONTRACT_RELATION_STANDARD_VERSION: Final[ContractRelationStandardVersion] = (
    "contract-relation-standard-v2"
)

UPLOADED_CONTRACT_LABEL: Final = "上传合同 A"
CANDIDATE_CONTRACT_LABEL: Final = "候选合同 B"
UPLOADED_CONTRACT_END_DIVIDER: Final = (
    "==================== 上传合同 A 结束 ===================="
)
CANDIDATE_CONTRACT_START_DIVIDER: Final = (
    "==================== 候选合同 B 开始 ===================="
)
CANDIDATE_CONTRACT_END_DIVIDER: Final = (
    "==================== 候选合同 B 结束 ===================="
)
TOOL_INSTRUCTION_START_DIVIDER: Final = (
    "==================== 可用工具与输出协议 ===================="
)
CANDIDATE_CONTRACT_INPUT_HEADER: Final = f"""{CANDIDATE_CONTRACT_START_DIVIDER}
以下是“{CANDIDATE_CONTRACT_LABEL}”当前包含的连续页面图像。每个物理页码标签后的图像只对应该页；B 的物理页码独立从 1 开始。"""

CONTRACT_RELATION_STANDARD_PROMPT: Final = """你是具有采购、财务和合同档案审核经验的合同关系审核员。你的目标是判断“上传合同 A”与“候选合同 B”是否应当作为两份独立合同同时入库，而不是判断它们是否使用相同模板或讨论合同法律效力。两份合同的页面图像是唯一事实来源；不得使用文件名、检索排名、相似度分数或合同外知识补全页面中没有的事实。

关系定义：
1. duplicate（重复）：两份文档属于同一合同或同一合同版本链，业务上不应作为两份独立合同同时入库。包括同一文件的扫描、压缩、缩放、裁剪、旋转、重渲染或重新导出副本，同一合同的缺页或局部副本，以及同一合同的修订版、最终版、替换版、重新签章版或其他更新版本。修订产生的金额、日期、条款、页面或签章差异不自动排除 duplicate；只要证据表明它们延续并替代同一合同身份，仍判为 duplicate。
2. similar（相似）：两份文档存在明确业务关联、来源关系或高度结构相似性，但应当作为独立文件共同保留。包括主合同与补充协议、变更协议或独立附件，同一项目或交易下职责不同的合同，以及使用同一模板但合同编号、交易事项或合同身份不同的合同。
3. different（不同）：两份文档具有独立合同身份和独立交易事项，也没有值得保留的明确业务关联。普通合同共有的标题、条款结构或通用版式不构成 similar。

判断标准：
1. 先确认每份文档页面代表的文件范围，再综合核对合同名称与编号、签约主体、项目或订单标识、交易标的、规格数量、金额税率与币种、付款交付和有效日期、关键权利义务、版本或替代说明、签字印章，以及页面和版式结构。
2. 合同编号、主体或视觉版式相同都是重要线索，但任何单一线索都不足以独立判定 duplicate。视觉相似不得覆盖合同身份或交易事项明显不同的事实。
3. 发现实质差异时，必须继续区分该差异属于同一合同的版本延续，还是另一份应独立保留的文件；不得仅因金额、日期、条款或签章不同就直接判为 similar 或 different。
4. 页面数量不同不直接决定关系。缺页、空白页、扫描附页和页面顺序差异可能属于 duplicate；独立补充协议、新增法律文件或不同交易内容通常属于 similar 或 different。
5. similar 是已经由证据确认的业务关系，不是不确定结论。页面模糊、关键页缺失、证据冲突或无法可靠区分时，不得用 similar 或其他关系兜底，也不得提交缺少充分证据的关系决定；应使用当前任务提供的补充证据或无法判断出口。

证据与决定：
1. 只使用两份合同页面中可直接观察的文字、数字、表格、签章和版式事实。每项关键证据必须同时标明上传合同与候选合同的物理页码，并使用简短原文或可核对的视觉观察，不复制整页内容。
2. 同时存在支持和反对当前关系的证据时必须完整保留冲突，不得选择性忽略。推理摘要只解释证据如何满足上述关系定义，以及不确定性为何不影响或阻止决定。
3. 严格遵循“跨文档证据在前、简洁推理摘要在中、最终关系决定在后”的顺序。最终关系只能是 duplicate、similar 或 different，不得创造其他业务类别。

边界示例：
- 合同编号和交易身份一致，一份明确为另一份的修订或替换版本：duplicate。
- 多数共有页面一致，一份只是缺页扫描件，且合同身份与交易事实连续：duplicate。
- 同一模板生成，但合同编号、订单或交易事项不同：similar。
- 补充协议明确引用主合同，但自身是需要共同保存的独立文件：similar。
- 合同身份、交易事项和主体均独立，只有通用合同版式相似：different。
- 关键页面不可读，无法确认是修订版本还是独立合同：不得选择任何关系兜底，应使用当前任务提供的补充证据或无法判断出口。"""


def append_contract_relation_standard(
    uploaded_pdf_messages: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """在上传文档页面前缀末尾追加稳定关系标准，不改写输入消息。"""
    messages = deepcopy(list(uploaded_pdf_messages))
    if not messages:
        raise ValueError("上传文档页面前缀不能为空")
    content = messages[-1].get("content")
    if not isinstance(content, list):
        raise TypeError("上传文档页面的最后一条消息必须使用内容块列表")
    content.append(
        {
            "type": "text",
            "text": (
                f"{UPLOADED_CONTRACT_END_DIVIDER}\n"
                f"以上按页码连续提供的全部页面图像属于“{UPLOADED_CONTRACT_LABEL}”。\n"
                f"后续任务中另行提供的第二组页面图像统一标记为“{CANDIDATE_CONTRACT_LABEL}”。\n"
                "A、B 的物理页码分别从 1 开始，引用证据时必须同时写明文档标签和物理页码；"
                "不得仅凭输入先后赋予任何一份合同更高可信度。\n\n"
                f"合同关系共同判断标准：\n{CONTRACT_RELATION_STANDARD_PROMPT}"
            ),
        }
    )
    return messages


__all__ = [
    "CONTRACT_RELATION_STANDARD_PROMPT",
    "CONTRACT_RELATION_STANDARD_VERSION",
    "CANDIDATE_CONTRACT_INPUT_HEADER",
    "CANDIDATE_CONTRACT_END_DIVIDER",
    "CANDIDATE_CONTRACT_LABEL",
    "CANDIDATE_CONTRACT_START_DIVIDER",
    "ContractRelationStandardVersion",
    "TOOL_INSTRUCTION_START_DIVIDER",
    "UPLOADED_CONTRACT_LABEL",
    "UPLOADED_CONTRACT_END_DIVIDER",
    "append_contract_relation_standard",
]
