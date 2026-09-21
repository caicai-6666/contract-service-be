"""文件摘要、文件与文字相关性的模型输出契约，独立于公开状态投影。"""

from app.infrastructure.model_json import validate_model_json
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


NonBlankText = Annotated[str, Field(min_length=1, pattern=r"[\s\S]*\S[\s\S]*")]


class RejectionReplyGeneration(BaseModel):
    """只允许组织拒绝回复；是否放行由程序决定，模型不能改写。"""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    evidence: list[NonBlankText] = Field(max_length=8, description='引用所给处理记录或文件摘要中支持回复的简短事实，摘要证据注明上传序号；纯文字寒暄也可引用用户问候及无附件的输入事实。不编造未检查文件或历史，缺少依据时允许空列表')
    reasoning: NonBlankText = Field(max_length=600, description='简要说明记录中已确认的主要障碍、每项调整为何必要，以及哪些文件或历史情况仍未核验；文件类型差异须依据摘要明确内容，不能仅凭用户称呼、业务无关或摘要未提及某内容作判断。纯文字寒暄只说明为何适用礼貌回应。不把未知当作事实，不输出冗长探索过程')
    message: NonBlankText = Field(max_length=2000, description='直接给用户的简洁中文纯文本。将摘要中明确的内容作为已掌握的文件事实直接陈述，不说“摘要显示”“根据摘要”等内部来源转述，不伪称逐页检查；无法确认的内容仍保留不确定性。无附件、仅寒暄且没有已确认的服务故障或检查未完成时，礼貌回应、简要介绍合同及财务法律业务协助范围并邀请表达需求，不强调处理停止或要求重提。其他情况说明本次无法继续的具体障碍及最少必要调整，并明确调整后重新提问或提交；业务文件夹带独立非业务内容时，按摘要指出问题文件和已有确切页码，建议调整该部分并保留完整业务内容，不把整份文件说成无关、不猜页码；服务故障则建议稍后重新提交。不凭空增加上传要求，不从本轮没有附件推断旧文件不可用，不承诺未经确认的工具能力。不得暗示本次等待补充后自动续接，不含代码、链接、HTML 或业务分析结论')


def validate_rejection_reply(content: str) -> RejectionReplyGeneration:
    """校验完整对象；反馈只报告字段问题，不回显原始输出。"""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('包含重复字段，请每个字段只提交一次。')
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError('只能使用标准 JSON，不能包含 NaN 或 Infinity。')

    try:
        json.loads(content, object_pairs_hook=unique, parse_constant=reject_constant)
        return validate_model_json(RejectionReplyGeneration, content)
    except json.JSONDecodeError as exc:
        raise ValueError('请提交完整 JSON 对象，不附加代码块或其他文字。') from exc
    except ValidationError as exc:
        details = '; '.join('.'.join(map(str, error['loc'])) + '：' + error['msg']
                            for error in exc.errors(include_input=False, include_url=False))
        raise ValueError('字段未符合约定，请修正：' + details) from exc


class FileSummaryGeneration(BaseModel):
    """只约束模型可生成的三个字段，不允许模型改写文件身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    reasoning: NonBlankText = Field(description="先简要列出物理页码和可核对的短原文或视觉事实，再说明它们如何支持命名与概述；保留歧义，不输出冗长探索过程。")
    summary: NonBlankText = Field(description="仅依据当前文件全部已提供页面生成的中文内容概述，说明可辨认的主要对象、场景、事项和内容范围，不只描述图片或表格等载体形式；用途未知时仍保留可见事实，仅说明原文件中影响理解的具体限制。显著偏离主体的独立内容即便仅一页，也须在本字段明确保留输入标签中的物理页码、实际内容及与主体的关系，不能只写在 reasoning；用途关系不明时保留不确定性，不误判合法附件或证据。不依赖用户问题，不补造对象、背景或用途，不作风险分析、恶意判断、准入结论或建议。")
    display_name: NonBlankText = Field(description="依据页面内容生成的简明中文展示名称，突出可确认的主要对象和文件类型；对象可辨认时不只写载体名称，无法确认具体对象时不猜测。只给一个名称主体，不带扩展名、路径或解释，不是原始文件名或唯一标识。")


class SummaryValidationError(ValueError):
    """可反馈给模型的摘要结构错误，不包含原始响应或内部堆栈。"""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SummaryValidationError('同一对象包含重复字段，无法确定应采用哪个值。请每个字段只提交一次。')
        result[key] = value
    return result


def _reject_nonfinite(value):
    raise SummaryValidationError('JSON 不允许 NaN 或 Infinity；三个字段都应填写非空文字。')


def validate_file_summary(content: str) -> FileSummaryGeneration:
    """严格读取完整 JSON，只兼容容器编码，不截取代码块或猜测修补。"""
    try:
        json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_nonfinite)
    except json.JSONDecodeError as exc:
        raise SummaryValidationError(
            f'输出不是完整有效的 JSON（第 {exc.lineno} 行、第 {exc.colno} 列附近）。'
            '请核对双引号、逗号及括号，只提交完整对象，不附代码块或额外说明。') from exc
    try:
        return validate_model_json(FileSummaryGeneration, content)
    except ValidationError as exc:
        issues = []
        for error in exc.errors(include_url=False, include_input=False):
            path = '.'.join(map(str, error['loc'])) or '整体结果'
            kind = error['type']
            reason = ('顶层必须是包含 reasoning、summary、display_name 的 JSON 对象，不能是数组或 null。'
                      if kind == 'model_type' else
                      '缺少必填字段，请补充该字段。' if kind == 'missing' else
                      '该字段不在约定结构中，请删除；文件身份由程序保留，不应生成。' if kind == 'extra_forbidden' else
                      '必须填写非空字符串，不能使用空白、null、数字、数组或对象。')
            issues.append(f'- {path}：{reason}')
        raise SummaryValidationError('\n'.join(issues)) from exc
    except ValueError as exc:
        raise SummaryValidationError('内嵌 JSON 不合法，请按字段类型提交完整对象或数组。') from exc


def build_summary_validation_feedback(problem: str) -> dict[str, str]:
    return {'role': 'user', 'content': (
        '【文件摘要校验反馈】\n上一份输出尚未被接受，原因如下：\n'
        f'{problem}\n请重新核对原始页面，提交包含 reasoning、summary、display_name 的完整 JSON 对象，'
        '不要只提交修改字段，也不要生成工具调用。')}


TextBusinessRelevanceDecision = Literal['related', 'uncertain', 'unrelated']


class TextBusinessRelevanceGeneration(BaseModel):
    """只接受简短证据理由和三态判断，不允许模型生成分数或整体准入状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    reasoning: NonBlankText = Field(description="非空字符串，不限制语言；先引用本轮用户文字中的简短原文，保留原文语言，再说明相关、不相关或证据不足的依据。不得把翻译当作原文，不猜测对象用途、文件内容或历史，不输出冗长推理。")
    result: TextBusinessRelevanceDecision = Field(description="related：本轮文字可确认财务、法律、合同或直接关联事项，且没有明确非业务请求目的；uncertain：没有明确非业务目的，但缺少识别业务主题所需的信息，依赖不可见文件或前文；unrelated：包含任一明确非业务请求目的，即便其他部分相关，或整体明确不属于业务范围。背景、引用或否定对象不自动算作请求目的。仅缺少回答细节仍为 related；执行故障不是 uncertain。本字段不代表整轮准入结果。")


class TextRelevanceValidationError(ValueError):
    """可直接反馈给模型的文字判断校验错误，不回显无效输出。"""


def _validate_relevance(content, *, model, error_type, source, boolean_result=False):
    """拒绝重复字段、非标准 JSON 和隐式类型转换，只校验完整响应。"""
    fields = '、'.join(model.model_fields)
    result_rule = ('必须是 JSON 布尔值 true 或 false，不能使用字符串、数字、null 或 uncertain。'
                   if boolean_result else
                   '必须是字符串 related、uncertain 或 unrelated 之一，不能使用布尔值、数字、null 或其他拼写。')
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise error_type(f'同一对象包含重复字段。请 {fields} 各提交一次。')
            result[key] = value
        return result

    def reject_constant(_):
        raise error_type('JSON 不允许 NaN 或 Infinity；result ' + result_rule)

    try:
        json.loads(content, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise error_type(
            f'第 {exc.lineno} 行、第 {exc.colno} 列附近不是有效 JSON。请核对双引号、逗号和括号，'
            '只提交完整对象，不附代码块或其他文字。') from exc
    try:
        return validate_model_json(model, content)
    except ValidationError as exc:
        issues = []
        for error in exc.errors(include_url=False, include_input=False):
            path = '.'.join(map(str, error['loc'])) or '整体结果'
            kind = error['type']
            reason = (f'顶层必须是包含 {fields} 的 JSON 对象，不能是数组或 null。'
                      if kind == 'model_type' else
                      '缺少必填字段，请补充。' if kind == 'missing' else
                      f'不是允许的字段，请删除；只保留 {fields}。' if kind == 'extra_forbidden' else
                      result_rule if path == 'result' else
                      '必须是字符串列表，允许 []；每项须为注明来源的非空短原文，不能是空白、数字或对象。' if path.startswith('evidence') else
                      f'必须是非空字符串，先引用{source}的短原文再说明判断依据。')
            issues.append(f'- {path}：{reason}')
        raise error_type('\n'.join(issues)) from exc
    except ValueError as exc:
        raise error_type('内嵌 JSON 不合法，请按字段类型提交完整对象或数组。') from exc


def validate_text_business_relevance(content: str) -> TextBusinessRelevanceGeneration:
    return _validate_relevance(content, model=TextBusinessRelevanceGeneration,
                               error_type=TextRelevanceValidationError, source='本轮文字')


def build_text_relevance_validation_feedback(problem: str) -> dict[str, str]:
    return {'role': 'user', 'content': (
        '【文字业务相关性校验反馈】\n上一份输出尚未被接受，原因如下：\n'
        f'{problem}\n请依据原始用户文字重新提交包含 reasoning、result 的完整 JSON 对象，'
        '不要只提交修改字段，不生成工具调用。')}


FileBusinessRelevanceDecision = Literal['related', 'uncertain', 'unrelated']


class FileBusinessRelevanceGeneration(BaseModel):
    """单文件模型结果；不让模型绑定身份、重命名文件或决定多文件分数。"""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    reasoning: NonBlankText = Field(description='不限制语言；先引用当前 display_name 或 summary 中的简短原文，再解释业务相关、不相关或无法判断的依据。不虚构原文件页码、对象用途或未提供的用户背景。')
    result: FileBusinessRelevanceDecision = Field(description='related：名称和摘要足以确认文件属于财务、法律、合同、经营管理或业务设备技术资料，且没有明确独立的非业务实质内容；uncertain：主题或用途信息不足、关键冲突无法消除；unrelated：文件整体或任一独立组成部分明确属于业务范围之外，即便其他部分相关。业务背景、引用、证据和必要附件不自动算无关内容。仅判断当前文件，不推断未披露页面、恶意或整轮准入。')


class FileRelevanceValidationError(ValueError):
    """文件相关性最小纠错信息，不泄露其他文件或原始响应。"""


def validate_file_business_relevance(content: str) -> FileBusinessRelevanceGeneration:
    return _validate_relevance(content, model=FileBusinessRelevanceGeneration,
                               error_type=FileRelevanceValidationError, source='display_name 或 summary')


def build_file_relevance_validation_feedback(problem: str) -> dict[str, str]:
    return {'role': 'user', 'content': (
        '【文件业务相关性校验反馈】\n上一份输出尚未被接受，原因如下：\n'
        f'{problem}\n请只依据当前文件的 display_name 和 summary，重新提交 reasoning、result 的完整 JSON 对象，'
        '不要只提交修改字段，不输出文件身份、分数或工具调用。')}


class FileTextRelevanceGeneration(BaseModel):
    """当前文字与全部摘要的整体关联判断；允许文字明确引用历史文件。"""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    reasoning: NonBlankText = Field(description='非空字符串，不限制语言；先引用文字区原文，解释当前或历史文件的指代和操作关联；按当前文件内容判断时补充上传序号、名称或摘要依据。历史引用须说明内容未提供，不伪造其身份、内容或与本轮文件的映射。区分依据不足与明确无关，不把无法回答等同于不相关。')
    result: bool = Field(description='整体 JSON 布尔值。true：文字明确要求处理当前或历史文件，或与至少一份本轮文件存在具体主题、对象关联；false：无上述关联、有效操作被撤销、条件不满足或依据不足。不是逐文件判断，不保证所有本轮附件相关、旧文件可用或整轮准入；不生成分数。')


class FileTextRelevanceValidationError(ValueError):
    """文件与文字判断的最小纠错信息，不回显错误输出。"""


def validate_file_text_relevance(content: str) -> FileTextRelevanceGeneration:
    return _validate_relevance(content, model=FileTextRelevanceGeneration,
        error_type=FileTextRelevanceValidationError, source='文字区及适用的文件摘要区', boolean_result=True)


def build_file_text_relevance_validation_feedback(problem: str) -> dict[str, str]:
    return {'role': 'user', 'content': (
        '【文件与文字相关性校验反馈】\n上一份输出尚未被接受，原因如下：\n'
        f'{problem}\n请依据文字区和完整文件摘要区重新提交整体 reasoning、result JSON；历史文件仅按文字引用判断，不补造历史内容；'
        'result 必须是布尔值 true 或 false。不要只提交修改字段，不输出文件身份、分数或工具调用。')}


ContextRelevanceBasis = Literal['history_continuation', 'prior_file_reference', 'none']


class ContextRelevanceGeneration(BaseModel):
    """上下文关联证据、简要理由和判断；不生成分数或整轮准入结论。"""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    evidence: list[NonBlankText] = Field(description='注明历史轮次及消息、本轮文字或文件摘要来源的短原文列表；明确操作先前文件但历史未展示时可只引用本轮指引和操作要求。不补造历史；无可引用依据时允许空列表。')
    reasoning: NonBlankText = Field(description='简洁解释可见业务历史如何支持本轮延续；先前文件引用必须说明原文中的明确文档对象、先前指向及当前操作，不能把可能是文件的前文或回答当成文件证据；无关联时区分无关与证据不足。不补造事实或声称文件已定位核验。')
    basis: ContextRelevanceBasis = Field(description='关联依据类型：history_continuation 表示可见业务历史支持本轮延续；prior_file_reference 要求本轮原文明示先前文档指向及当前操作，不核验上传事实，不要求可见历史收录附件或唯一定位；仅有上面、第二点、先前回答等普通前文指代则不成立。none 表示两者均不满足。二者都满足时优先 history_continuation。')
    result: bool = Field(description='JSON 布尔值。basis 为 history_continuation 或 prior_file_reference 时必须 true，为 none 时必须 false。不是整轮准入结论，不代表文件可用或答案正确。')

    @model_validator(mode='after')
    def validate_basis(self):
        if self.result != (self.basis != 'none'):
            raise ValueError('basis 与 result 不一致：none 必须对应 false，其他依据必须对应 true。')
        return self


class ContextRelevanceValidationError(ValueError):
    """可反馈的结构错误，不回显无效原响应。"""


def validate_context_relevance(content: str) -> ContextRelevanceGeneration:
    return _validate_relevance(content, model=ContextRelevanceGeneration,
        error_type=ContextRelevanceValidationError, source='已提供历史与本轮输入', boolean_result=True)


def build_context_relevance_validation_feedback(problem: str) -> dict[str, str]:
    return {'role': 'user', 'content': (
        '【上下文相关性校验反馈】\n上一份输出尚未被接受，原因如下：\n'
        f'{problem}\n请依据原始资料重新提交 evidence、reasoning、basis、result 四个字段的完整 JSON；'
        'evidence 为短原文字符串列表，reasoning 为非空文字；basis 为 history_continuation、prior_file_reference 或 none，'
        'none 对应 result=false，其他依据对应 result=true。'
        '不要只提交修改字段，不补造未展示历史，不生成工具调用。')}


class FileTopicConflictGeneration(BaseModel):
    """只判断摘要披露的主题冲突；true 表示命中，而非通过门禁。"""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    reasoning: NonBlankText = Field(description='先引用当前摘要的简短依据，再说明独立内容与主体用途的关系；保留已有页码，不猜测页码、未披露内容或恶意。证据不足时说明限制。')
    result: bool = Field(description='JSON 布尔值。true：摘要明确披露显著偏离主体用途的独立无关内容，即便只有一页；false：未能确认此类冲突，包括合法附件、关系未知或普通条款差异。false 不代表业务准入。')


class FileTopicConflictValidationError(ValueError):
    """主题冲突输出的最小可恢复校验错误。"""


def validate_file_topic_conflict(content: str) -> FileTopicConflictGeneration:
    return _validate_relevance(content, model=FileTopicConflictGeneration,
        error_type=FileTopicConflictValidationError, source='文件摘要', boolean_result=True)


def build_file_topic_conflict_validation_feedback(problem: str) -> dict[str, str]:
    return {'role': 'user', 'content': '【文件主题冲突校验反馈】\n'
        + problem + '\n请依据原始摘要重新提交 reasoning、result 的完整 JSON 对象；'
        'result 必须为布尔值 true 或 false，不输出工具调用或其他字段。'}
