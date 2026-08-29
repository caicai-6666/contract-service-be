"""条款候选顺序发现节点的稳定任务提示词与动态工作区。"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any, Final

import yaml

from app.agent.contract_extraction.context import append_contract_task
from app.agent.contract_extraction.state import ContractPrefillContext
from app.agent.contract_extraction.subgraph.clause_extraction.tool import (
    AnalyzeClauseHierarchyArguments,
    ClauseCandidateWorkspaceItem,
)
from app.agent.contract_extraction.tool_protocol import TOOL_CALL_XML_INSTRUCTION

CLAUSE_DISCOVERY_PROMPT_VERSION: Final = "clause-discovery-v13"
CLAUSE_DISCOVERY_TOOL_PLACEMENT: Final = "before_task"

_CLAUSE_DISCOVERY_TASK_BASE = """你已获得当前合同的原始 PDF、文档导航结构和分类结果。当前任务是按合同原始阅读顺序发现全部待提取条款候选，只记录具有自身直接正文的主条款和各级子条款。你只需确定条款身份、层级和精简起止锚点，不提取完整正文；已确认锚点将用于逐条提取详细原文。

事实来源与已有资料：
1. 原始 PDF 页面是条款事实和边界的唯一权威来源。
2. 已提供的文档导航结构只用于定位宏观区域，合同分类只用于理解交易语境；两者都不能替代页面核查，也不能改变原文条款边界。
3. 不读取 Core 或其他字段提取结果，不使用文件名、常识或模板补全条款。

条款候选定义：
1. 条款候选必须具有可识别边界，并且存在属于该候选自身、可从 PDF 直接对应的正文文字，用于表达权利、义务、条件、限制、程序、责任、效力或风险分配。
2. “自身直接正文”是移除全部下级条款正文后仍属于当前候选的文字。编号、标题、章节名、分组名、项目符号或“如下”等纯结构文字只能帮助导航，不能单独满足候选准入条件。
3. 有独立规范内容的子层级条款必须单独记录，不因其属于同一主条款而合并；每个候选通过 document_path 保留原合同完整层级，通过 parent_candidate_id 关联最近的已记录正文祖先。
4. 无编号条款只要具有独立规范内容和可核对边界，也必须记录；identifier 使用简短稳定描述，不得虚构原始编号。
5. 只有编号、标题、章节名或分组名，且其后直接进入下级条款的结构父级，不得单独记录正文候选。它仍是原合同结构的一部分，必须保留在下级候选的 document_path 中；它不能成为 parent_candidate_id，但也绝不能使下级候选的 document_path 或 level 被压平。

粒度与排除边界：
1. “第三条 乙方义务”下可见的“（1）”“（2）”分别表达独立义务时，先检查“第三条”在排除两项正文后是否仍有自己的规范文字；有则记录父条款和两个子条款，没有则跳过纯标题，只记录“（1）”“（2）”。
2. 同一句中的产品、规格、人员、材料、步骤或事实枚举，即使带序号，也不因枚举形式自动成为多个条款；只有各项分别形成独立规范内容时才拆分。
3. 纯合同标题、主体身份、普通商品清单、纯价格表、页眉页脚、页码、目录索引、空白占位和单纯签字盖章资料不属于条款。
4. 附件标题、附件目录、附件编号、图纸、规格表、报价表、产品清单及其他附件实体默认不作为条款候选；即使正文引用附件或声明附件属于合同组成部分，也不能仅据此把附件实体记录为条款。
5. 正文或附件附近若存在独立表达权利、义务、责任、适用条件、法律效力或风险分配的规范性文字，仍按真实边界记录；不能因文字提到附件、位于附件附近或版式特殊而排除。
6. 表格或签署附近若实际包含独立权利义务、技术约束、承诺或效力规则，仍按真实规范内容记录，不能仅因版式特殊而排除。
7. 一个候选可以跨页；换页、续表或排版断行不能单独造成拆分。

边界证据规则：
1. evidence.start 和 evidence.end 都是必填的包含式锚点：start 定位当前条款的编号、标题或首句，end 定位当前条款自身最后一段原文。
2. 起止 anchor 必须保持 PDF 可核对原文，每个最多 160 个字符，只保留足以区分相邻边界的短片段，不复制完整条款。
3. end 只能引用当前候选自身的末尾原文，固定属于当前候选；不得使用下一条款开头、签署区、附件标题或其他非当前条款文字，也不得提交 inclusion、边界类型或 null。
4. 叶子候选的 end 使用该条款最后一句或最后一个可核对短片段；即使后面紧接下一候选，也不能把下一候选开头当作 end。
5. 只有确有自身直接正文的父候选才可记录；其 end 应定位父候选自己的最后一段直接正文。子孙正文不属于父候选 end，即使父级视觉范围覆盖子孙条款。
6. 物理页码从 1 开始，不使用合同印刷页码。边界不清、遮挡或缺页时先调用 think 核对，不得虚构锚点。

权威层级定义与证据优先级：
1. 合同条款层级是原文作者通过编号、标题、项目符号、排版和作用范围明确表达的结构包含关系；本任务只复原原文结构，不按法律重要性或语义主题重新组织合同。
2. 层级证据按以下顺序判断：明确的复合编号前缀；局部编号体系及其重置；标题、缩进、对齐、字体、项目符号和间距；“如下”“包括”“具体为”等引导语及其范围；内容语义。语义只可确认已有结构证据，不能单独创建父子关系。
3. 候选 C 的原合同父级 P 由 PDF 的编号、标题、版式和作用范围决定，与 P 是否具有直接正文、是否进入候选目录无关。P 即使是被跳过的纯结构标题，也必须出现在 C 的 document_path 中；parent_candidate_id 只引用该路径上最近的已记录正文祖先，不能替代原合同父级。
4. 相同编号形式、相同缩进或对齐、相同排版样式且连续递增的一组候选，默认互为同级；如果它们前面没有独立可见的父标题或引导范围，不得把第一项改造成其余各项的父级。该同级证据优先于主题上的包含关系。
5. 父标题和第一个子项可以位于同一行，但只有父标记后还存在不属于子项的独立规范正文时，父级才能单独记录；例如“四、甲方责任与义务：（1）提供图纸”若“四、甲方责任与义务”只是标题，则只记录“（1）提供图纸”。
6. 编号体系按局部结构解释，不映射固定层数，也不限制最大层级。“3→3.1→1)”可以形成三级；标题“7”下重新出现缩进的“1.”“2.”也可成为其子级，但 identifier 必须保留页面原始标记，不得擅自改写为“7.1”“7.2”。
7. 视觉列表中的同款项目符号是同级强证据；纯分组标题只用于确认各项目互为同级，不记录为候选。只有分组标题同时拥有自己的独立规范正文时，列表项目才引用它作为直属父候选。表格数据行、产品清单和事实枚举仍适用粒度排除规则。
8. 无编号规范段落只有在具有独立视觉或语言边界时才单独记录；若只是已有条款在换行、换页后的连续正文，则仍属于原条款。换页本身不改变层级。
9. 层级证据不足或相互冲突时，采用保守结构：候选与最近的结构可比候选保持同级；若不存在已确认的共同父级，则作为顶层。不得用推测填补缺失父级。

顺序与层级输出规则：
1. 按文档阅读顺序进行深度优先发现：遇到父级先判断其是否具有自身直接正文；符合准入条件才记录，然后进入内部子候选。纯结构父级只在 think 中说明已跳过，随后继续记录其范围内有直接正文的下级候选。
2. level 表达当前条款在原合同中的绝对结构深度：最外层为 1，必须等于 document_path 的项目数；它不等同于编号中数字、点号或符号的数量，也绝不因任何父标题未进入正文候选而改变。
3. document_path 从原合同最外层条款开始，逐级列到当前候选自身；最后一项必须与当前 identifier/title_hint 一致。没有自身直接正文的纯编号、标题和分组父级不生成候选，但必须作为路径项保留，禁止把其下条款提升为顶层。
4. parent_candidate_id 只用于关联 document_path 上最近的已记录正文祖先：有则必须引用该 candidate_id；若路径中的所有父级都因没有直接正文而未记录，则传 null，即使当前 level 大于 1。candidate_id 和 order 由程序生成，模型不得提交。
5. 不得重复工作区已有候选，不得跳过尚未检查的内部子层级，也不得按语义主题重新排序原文。

工具、记忆与工作区：
1. 每轮必须且只能调用当前提供的一个工具，禁止输出普通文本。
2. 首轮必须且只能调用 analyze_clause_hierarchy，先扫描整份 PDF，再按 evidence、reasoning_summary、decision 的顺序提交页面结构证据、详细层级分析和合同专属提取指导；本轮不逐条记录候选，也不复制完整正文。
3. analyze_clause_hierarchy 成功后，程序将完整分析写入工作区、清空首轮短期记忆并永久移除该工具。此后每轮都必须读取这份层级分析；它是发现计划而不是新的合同事实，若局部页面证据与计划冲突，始终以原始 PDF 为准并调用 think 说明修正后的判断。
4. 层级分析完成后，think 用于思考下一个尚未记录候选，不写入工作区；边界清楚时调用 record_clause_candidate，一次只提交一个候选，参数严格按照 evidence、reasoning_summary、decision 的顺序。
5. 候选 reasoning_summary 先确认移除下级条款后仍存在当前候选自己的直接正文，再指出可见编号、排版或引导范围，最后说明边界、原合同绝对层级、完整 document_path 和最近的已记录正文祖先；不得把纯标题当作直接正文，不得重复完整正文或引入证据之外的新事实。
6. 候选成功后，程序把精简候选追加到工作区并清空本轮短期记忆。更新后的工作区会继续提供层级分析、完整候选目录、candidate_id、顺序和锚点；下一次行动必须以它为准。
7. 新一轮从最后候选的起始位置之后继续深度优先检查：先判断其范围内是否存在明确的子层级结构证据；有证据才检查并记录子层级，没有证据则直接检查后续同级或更高层级条款。不能机械地把后续内容都当作子级，也不能机械地从真实父候选结束锚点之后开始。
8. 只有刚记录的最后一个候选有误时，才调用 revise_last_clause_candidate 提交完整替代内容；不得尝试修改更早工作区记录或改写首轮层级分析。
9. 工具返回 ok=false 时，按照反馈指出的参数位置、问题和改进方向修正，不把失败调用写入工作区。
10. 遇到附件索引、附件实体或其他排除项时，只在 think 中说明已跳过并继续检查；若后续没有条款则调用 finish_clause_discovery。是否还有待发现条款只由 finish_clause_discovery 决定，不由候选 end 表达。绝不能调用 record_clause_candidate 来“确认边界”，也不能提交 null、none、n/a、“不记录”或类似占位标识。

完成检查：
1. 结束条款发现前必须调用 think，核对所有条款区域、所有可见编号体系、无编号规范段落、跨页延续、附件附近的规范性文字和最后一个条款边界。
2. 重点检查是否漏掉有直接正文的子层级、是否错误记录只有编号或标题的结构父级、是否把同级序列误挂为父子、是否创造原文不存在的父级、是否把事实枚举误当条款，以及是否错误纳入签章、普通清单或附件实体。
3. 只有确认全部主条款和子层级条款均已记录后，才能调用 finish_clause_discovery；该工具独立决定候选发现是否结束。其结束证据只证明整份扫描的检查终点，不替代最后候选必填且属于其自身的 end。
4. 排除附件实体只表示不生成候选，不表示停止阅读。decision.last_checked_page 必须填写实际检查到的最后物理页；即使最后几页全部是被排除的附件，也必须检查这些页面，并在 evidence 中提供来自该最后检查页的简短视觉内容描述。

层级与粒度校准示例：
示例一——纯标题不提取但保留层级：页面依次出现“第三条 乙方义务”“（1）按期交付”“（2）负责安装”“第四条 付款方式”。若“第三条 乙方义务”之后立即进入“（1）”，移除两项后只剩编号和标题，则不记录“第三条”的正文候选，但“（1）”“（2）”仍是 level=2，document_path 分别为“第三条 > （1）”和“第三条 > （2）”，parent_candidate_id 为 null。若页面写成“第三条 乙方义务：乙方应负责总体协调。具体义务如下：（1）按期交付……”，则总体协调文字是第三条自己的直接正文，应记录第三条，并让“（1）”等通过 parent_candidate_id 引用它。

示例二——禁止虚构父级：页面从“（1）订购产品”开始，随后以相同缩进和样式连续出现“（2）制造厂商”“（3）交货日期”直至“（11）争议解决”，且不存在单独父标题。这十一项必须作为同级候选；不得创建原文不存在的“主条款（1）至（11）”，也不得把“（1）”扩成其余十项的父级。

示例三——多级纯标题不得压平：页面出现“3. 支付条款”“3.1 付款安排”，随后连续出现具有完整付款义务的“1)”“2)”“3)”“4)”，最后出现自身包含开票义务正文的“3.2 发票”。若“3”和“3.1”都只有标题，则二者均不记录正文候选；“1)”至“4)”仍为 level=3，document_path 必须保留“3. 支付条款 > 3.1 付款安排 > 当前子项”；“3.2”仍为 level=2，路径为“3. 支付条款 > 3.2 发票”。它们的 parent_candidate_id 均为 null。若“3.1”在子项之外另有自己的付款规则，才记录它，并让“1)”至“4)”引用该正文候选。

示例四——分组标题不提取但保留路径：纯标题“附加信息”下连续出现相同图标和缩进的“付款方式”“交货方式”“保修期限”“合同生效与形式”“其它约定”，若标题没有自己的直接正文，则不记录“附加信息”的正文候选，五项仍按原始顺序作为 level=2 的同级候选，document_path 保留“附加信息 > 当前项”；不能生成一个没有内容的父候选，也不能把五项提升为 level=1。

示例五——事实枚举不是层级：“3. 产品包括：（1）休闲鞋；（2）运动鞋”若只是合同标的枚举，不拆成两个条款；若“（1）”“（2）”分别规定交付、验收或责任等独立规范内容，才分别记录。不得仅根据序号决定粒度。

示例六——附件边界：页面先出现“本合同的附件是本合同不可分割的部分，与本合同具有同等法律效力”，随后出现“8.附件：设备技术图纸”和具体图纸页面。前一句独立规定附件法律效力，应记录为无编号效力条款；“8.附件”只是附件索引，后续图纸是附件实体，二者均不记录。排除附件实体后仍须继续检查后续页面，确认不存在其他正文条款。
"""
CLAUSE_DISCOVERY_TASK: Final = (
    f"{_CLAUSE_DISCOVERY_TASK_BASE}\n工具调用格式：\n"
    f"{TOOL_CALL_XML_INSTRUCTION}"
)

WORKSPACE_BEGIN = "===== 条款发现工作区：开始 ====="
WORKSPACE_END = "===== 条款发现工作区：结束 ====="
DIRECTION_BEGIN = "===== 下一步方向：开始 ====="
DIRECTION_END = "===== 下一步方向：结束 ====="

_WORKSPACE_COMMENTS = """# 工作区由程序维护，是已成功记录层级分析和候选的权威长记忆；模型只能读取和引用，不能改写已有内容。
# clause_discovery_workspace：当前条款发现任务的完整工作区根对象。
# hierarchy_analysis：首轮工具生成的整份合同层级分析；null 表示尚未完成首轮分析，此时不能记录候选。
# hierarchy_analysis.evidence：支持整份合同层级分析的页面结构观察，不是逐条候选证据。
# hierarchy_analysis.evidence[].page_numbers：当前观察对应的 PDF 物理页码，按升序排列。
# hierarchy_analysis.evidence[].observation：页面可见的编号、标题、缩进、项目符号及区域事实，不是完整正文。
# hierarchy_analysis.reasoning_summary：从页面观察推导编号体系、同级序列、父子关系和特殊边界的详细分析。
# hierarchy_analysis.decision：此后候选发现持续遵循的合同专属层级指导。
# hierarchy_analysis.decision.structure_summary：整份合同条款组织方式、预计层级和跨页关系的摘要。
# hierarchy_analysis.decision.extraction_guidance：按重要性排列的逐条发现指导；若与局部 PDF 证据冲突，以 PDF 为准。
# completed_candidates：按合同原始阅读顺序保存的全部成功候选；列表中的条目不得重复生成。
# completed_candidates[].candidate_id：程序生成的稳定候选 ID；用于 parent_candidate_id 引用，模型不得自行创建或修改。
# completed_candidates[].order：程序生成的全局发现顺序，从 1 连续递增；不是合同原始条款编号。
# completed_candidates[].evidence：该候选已确认、供详细原文提取使用的起止边界证据，不是完整正文。
# completed_candidates[].evidence.start：包含式起点；page_number 和 anchor 均属于当前候选开头。
# completed_candidates[].evidence.start.page_number：起始锚点所在的 PDF 物理页码，从 1 开始；不是合同印刷页码。
# completed_candidates[].evidence.start.anchor：来自起始页的短原文锚点，用于定位当前候选开头。
# completed_candidates[].evidence.end：当前候选自身最后一段原文的必填包含式锚点；不能引用下一候选或非条款区域。
# completed_candidates[].evidence.end.page_number：结束锚点所在的 PDF 物理页码，不得早于起始页码。
# completed_candidates[].evidence.end.anchor：来自结束页且属于当前候选末尾的短原文锚点；详细原文提取必须包含它。
# completed_candidates[].decision：已经接受的候选身份和层级决定，不包含完整正文或程序运行信息。
# completed_candidates[].decision.identifier：候选的原始编号；无原始编号时为简短稳定描述，不等同于 candidate_id。
# completed_candidates[].decision.title_hint：候选主题提示；null 表示没有可可靠确认的独立标题。
# completed_candidates[].decision.document_path：原合同从最外层到当前候选的完整结构路径；包含未进入正文候选的纯标题。
# completed_candidates[].decision.document_path[].identifier：该层在 PDF 中可见的原始编号或稳定标识。
# completed_candidates[].decision.document_path[].title_hint：该层可空的简短主题；null 表示无法可靠确认。
# completed_candidates[].decision.parent_candidate_id：路径上最近的已记录正文祖先 candidate_id；null 也可能表示父级均为未记录纯标题。
# completed_candidates[].decision.level：当前候选在原合同中的绝对深度，必须等于 document_path 项目数，不能因父标题未提取而改变。
# continuation：程序生成的本轮恢复信息，只说明从哪里继续，不是新的合同事实。
# continuation.last_candidate_id：工作区最后一个成功候选的 ID；null 表示尚未记录候选。
# continuation.instruction：清空短期记忆后必须执行的下一步扫描要求。"""


class _IndentedSafeDumper(yaml.SafeDumper):
    """让 YAML 列表相对父字段保持两空格缩进。"""

    def increase_indent(
        self,
        flow: bool = False,
        indentless: bool = False,
    ) -> None:
        del indentless
        return super().increase_indent(flow, False)


def _workspace_candidate(item: ClauseCandidateWorkspaceItem) -> dict[str, Any]:
    """按模型恢复工作所需顺序投影一个候选，不携带短期推理。"""
    return {
        "candidate_id": item.candidate_id,
        "order": item.order,
        "evidence": item.evidence.model_dump(mode="json"),
        "decision": {
            "identifier": item.identifier,
            "title_hint": item.title_hint,
            "document_path": [
                segment.model_dump(mode="json") for segment in item.document_path
            ],
            "parent_candidate_id": item.parent_candidate_id,
            "level": item.level,
        },
    }


def render_clause_discovery_workspace(
    workspace: tuple[ClauseCandidateWorkspaceItem, ...],
    hierarchy_analysis: AnalyzeClauseHierarchyArguments | None = None,
) -> str:
    """把程序工作区渲染为字段有注释、顺序稳定的精简 YAML。"""
    if workspace:
        last_candidate_id = workspace[-1].candidate_id
        instruction = (
            "从最后候选的起始位置之后继续深度优先检查：先判断其范围内是否有明确的"
            "子层级结构证据；有证据才找未记录子级，否则直接找后续同级或更高层级条款。"
        )
    elif hierarchy_analysis is None:
        last_candidate_id = None
        instruction = "首轮扫描整份 PDF 并完成合同层级分析；此时不要记录具体候选。"
    else:
        last_candidate_id = None
        instruction = "依据工作区层级分析，从合同条款区域的第一个候选开始逐条核对。"
    payload = {
        "clause_discovery_workspace": {
            "hierarchy_analysis": (
                None
                if hierarchy_analysis is None
                else hierarchy_analysis.model_dump(mode="json")
            ),
            "completed_candidates": [_workspace_candidate(item) for item in workspace],
            "continuation": {
                "last_candidate_id": last_candidate_id,
                "instruction": instruction,
            },
        }
    }
    serialized = yaml.dump(
        payload,
        Dumper=_IndentedSafeDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100_000,
    ).strip()
    return (
        f"{WORKSPACE_BEGIN}\n```yaml\n{_WORKSPACE_COMMENTS}\n"
        f"{serialized}\n```\n{WORKSPACE_END}"
    )


def render_clause_discovery_direction(
    workspace: tuple[ClauseCandidateWorkspaceItem, ...],
    hierarchy_analysis: AnalyzeClauseHierarchyArguments | None = None,
) -> str:
    """在动态消息尾部给出短期记忆重置后的明确恢复方向。"""
    if hierarchy_analysis is None:
        instruction = (
            "尚未完成合同层级分析。首轮必须调用 analyze_clause_hierarchy，扫描整份 PDF "
            "并提交页面证据、详细层级分析和持续提取指导；本轮不要记录具体条款候选。"
        )
    elif not workspace:
        instruction = (
            "合同层级分析已写入工作区，尚未记录任何候选。请依据该分析回到原始 PDF "
            "核对边界，跳过没有自身直接正文的纯编号或标题，并从原始阅读顺序中"
            "第一个具有直接正文的条款开始。"
        )
    else:
        last = workspace[-1]
        instruction = (
            f"你已完成条款“{last.identifier}”（{last.candidate_id}）的候选记录。"
            "接下来请从该条款的起始位置之后继续深度优先检查：先依据编号、父标题、"
            "缩进或项目符号判断其范围内是否确有子层级；只记录具有自身直接正文的"
            "子条款，纯编号、标题或分组父级只跳过并继续检查。没有符合条件的子级时，"
            "直接提取之后尚未记录的同级或更高层级条款。不要把同级序列误挂为子级，"
            "也不要重复工作区中的已有候选。"
        )
    return f"{DIRECTION_BEGIN}\n{instruction}\n{DIRECTION_END}"


def build_clause_discovery_task_messages(
    prefill_context: ContractPrefillContext,
) -> list[dict[str, Any]]:
    """在最终合同公共前缀尾部追加字节稳定的节点任务描述。"""
    return append_contract_task(
        prefill_context.messages,
        task_suffix=CLAUSE_DISCOVERY_TASK,
    )


def append_clause_discovery_workspace(
    task_messages: Iterable[dict[str, Any]],
    workspace: tuple[ClauseCandidateWorkspaceItem, ...],
    hierarchy_analysis: AnalyzeClauseHierarchyArguments | None = None,
) -> list[dict[str, Any]]:
    """在任务描述之后以独立 user 消息追加动态工作区。"""
    messages = deepcopy(list(task_messages))
    if not messages:
        raise ValueError("条款发现任务消息不能为空")
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"{render_clause_discovery_workspace(workspace, hierarchy_analysis)}\n\n"
                        f"{render_clause_discovery_direction(workspace, hierarchy_analysis)}"
                    ),
                }
            ],
        }
    )
    return messages


def build_clause_discovery_messages(
    prefill_context: ContractPrefillContext,
    workspace: tuple[ClauseCandidateWorkspaceItem, ...] = (),
    hierarchy_analysis: AnalyzeClauseHierarchyArguments | None = None,
) -> list[dict[str, Any]]:
    """构造“公共前缀 → 稳定任务 → 动态工作区”的完整无短期记忆消息。"""
    return append_clause_discovery_workspace(
        build_clause_discovery_task_messages(prefill_context),
        workspace,
        hierarchy_analysis,
    )


__all__ = [
    "CLAUSE_DISCOVERY_PROMPT_VERSION",
    "CLAUSE_DISCOVERY_TASK",
    "CLAUSE_DISCOVERY_TOOL_PLACEMENT",
    "DIRECTION_BEGIN",
    "DIRECTION_END",
    "WORKSPACE_BEGIN",
    "WORKSPACE_END",
    "append_clause_discovery_workspace",
    "build_clause_discovery_messages",
    "build_clause_discovery_task_messages",
    "render_clause_discovery_direction",
    "render_clause_discovery_workspace",
]
