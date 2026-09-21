"""视觉判断的唯一结构契约及字段级、业务级校验。"""

from app.infrastructure.model_json import validate_model_json
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, ValidationInfo, model_validator


# 约束解码后端可能采用完整匹配语义，不能仅用 \S，否则只能生成一个字符。
# 两侧显式允许任意字符，保证“含至少一个非空白字符”与本地校验语义一致。
NonBlank = Annotated[str, Field(strict=True, min_length=1, pattern=r"[\s\S]*\S[\s\S]*")]


class VisualEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    page_number: int = Field(ge=1, description="证据对应的原始物理页码，从1开始，必须属于当前文件；不能猜测未提供页面。")
    description: NonBlank = Field(max_length=600, description="该页可直接观察的文字清晰程度或视觉异常，简短描述位置与现象，不补写看不清的原文。")


class VisualReadabilityJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    evidence: tuple[VisualEvidence, ...] = Field(min_length=1, description="非空页面证据数组，支持本次可读性判断；页码必须属于当前文件。")
    reasoning: NonBlank = Field(max_length=1600, description="说明页面证据如何支持可读性结论，只给简短理由，不展开冗长推理或法律评价。")
    hint: NonBlank | None = Field(max_length=600, description="给用户的中文友好提示：不可读时说明问题页、具体原因及可行处理办法；可读时必须为null。")
    result: StrictBool = Field(description="整份文档是否视觉可读：true表示可读，false表示不可读；必须为JSON布尔值，不接受字符串或数字。")

    @model_validator(mode="after")
    def validate_business(self, info: ValidationInfo):
        if self.result and self.hint is not None:
            raise ValueError("文档已判为可读，不应给用户拒绝提示；请将 hint 改为 null，保留证据和判断。")
        if not self.result and self.hint is None:
            raise ValueError("你已判断文档不可读，但 hint 没有说明原因，用户无法知道哪里有问题、如何处理。请依据 evidence 说明问题页、具体视觉问题及改善方式，不编造原因。")
        page_count = (info.context or {}).get("page_count")
        if page_count is not None and any(e.page_number > page_count for e in self.evidence):
            raise ValueError(f"当前文件只有 {page_count} 页，evidence 引用了不存在的页面。请重新核对图像标签，只引用第 1 至 {page_count} 页，不猜测页码。")
        return self


class JudgmentValidationError(ValueError):
    """可反馈给模型的校验错误，不包含异常堆栈。"""


def judgment_json_schema(page_count: int) -> dict:
    """解码与本地校验共享同一 Schema 来源；文件差异只限制证据页码。"""
    if type(page_count) is not int or page_count < 1:
        raise ValueError("视觉判断至少需要一页")
    schema = VisualReadabilityJudgment.model_json_schema()
    schema["$defs"]["VisualEvidence"]["properties"]["page_number"]["maximum"] = page_count
    return schema


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JudgmentValidationError("同一 JSON 对象出现重复字段，无法确定你希望采用哪个值；请每个字段只提交一次。")
        result[key] = value
    return result


def _reject_nonfinite(value):
    raise JudgmentValidationError("JSON 中不能出现 NaN 或 Infinity；请按字段定义提交正常值。")


def validate_judgment(content: str, *, page_count: int) -> VisualReadabilityJudgment:
    """兼容容器编码；拒绝代码块、重复键、标量类型转换与越界页码。"""
    try:
        json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_nonfinite)
    except json.JSONDecodeError as exc:
        raise JudgmentValidationError(
            f"上一份输出不是完整有效的 JSON（第 {exc.lineno} 行，第 {exc.colno} 列附近）。"
            "请检查双引号、逗号和括号；只返回完整对象，不要包裹 Markdown 代码块或追加说明。"
        ) from exc
    try:
        return validate_model_json(VisualReadabilityJudgment, content, context={"page_count": page_count})
    except ValidationError as exc:
        issues = []
        for error in exc.errors(include_url=False, include_input=False):
            path = ".".join(map(str, error["loc"])) or "整体结果"
            kind = error["type"]
            if kind == "value_error":
                reason = str(error.get("ctx", {}).get("error", error["msg"]))
            elif path == "result":
                reason = "该字段表示整份文档是否可读；请填写不加引号的 true（可读）或 false（不可读），不能使用字符串、数字或 null。"
            elif kind == "missing":
                reason = "缺少必填字段。四个顶层字段 evidence、reasoning、hint、result 必须全部提交；每条证据必须有 page_number 和 description。"
            elif kind == "extra_forbidden":
                reason = "这个字段不在约定结构中，程序无法使用；请删除多余字段，不要自行扩展输出。"
            elif path.startswith("hint"):
                reason = "hint 用于向用户解释拒绝原因。不可读时请填写非空文字，指出实际问题和处理办法；可读时填写 null，不能使用空白文字。"
            elif "page_number" in path:
                reason = f"页码用于定位证据，必须是 1 至 {page_count} 的整数，不能使用字符串、小数或布尔值。"
            else:
                reason = "请按 Schema 提交正确类型与长度：evidence 是非空数组，每项含整数页码与非空描述；reasoning、description 使用非空简短文字，不要用对象、数字或空白代替。"
            issues.append(f"- {path}：{reason}")
        raise JudgmentValidationError("\n".join(issues)) from exc
    except ValueError as exc:
        raise JudgmentValidationError('内嵌 JSON 不合法，请按字段类型提交完整对象或数组。') from exc


def build_validation_feedback(problem: str) -> dict[str, str]:
    return {"role": "user", "content": (
        "【可读性结果校验反馈】\n上一条 assistant 消息是你尚未通过校验的完整输出，不是已接受的判断。\n"
        f"本次暂时无法使用这份结果，原因如下：\n{problem}\n"
        "请对照原始页面与上一版输出进行修正，保留有证据支持的内容；重新提交含 evidence、reasoning、hint、result 的完整 JSON 对象，而不是只提交修改字段。"
    )}
