"""单个 Core 提取对象定义的任务提示词。"""

from __future__ import annotations

from typing import Any

import yaml

from app.agent.contract_extraction.context import append_contract_task
from app.agent.contract_extraction.state import ContractPrefillContext
from app.agent.contract_extraction.subgraph.field_extraction.core.state import (
    CoreContext,
)
from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldDefinition,
)
from app.agent.contract_extraction.tool_protocol import TOOL_CALL_XML_INSTRUCTION

CORE_COMMON_PROMPT_VERSION = "core-common-v4"
CORE_EXTRACTION_PROMPT_VERSION = "core-extraction-v6"

FIELD_DEFINITION_GUIDE = """提取对象定义属性说明：
- name：当前唯一处理的对象类别；不能改名或创造新类别。
- aliases：合同中可能指向该对象类别的同义标题，只用于定位证据。
- meaning / excludes：分别定义对象成立条件和排除边界。
- cardinality：single 只允许一个对象；multiple 允许逐次提交多个独立对象。
- properties：单个对象的扁平属性定义。每项包含 name、aliases、type、required、meaning 和 excludes。
- 属性 type 只允许 string、integer、number、boolean；属性值不能是对象或数组。
- required 为 false 的属性没有可靠证据时直接省略，不使用 null 或伪造默认值。"""

CORE_COMMON_TASK = """你已获得当前合同的原始 PDF、文档导航结构、分类结果和 Core 对象提取通用规则。当前唯一对象定义及与其匹配的工具将在独立材料中给出；只有读到对象定义和可用工具后才能执行提取。

原始 PDF 是唯一事实来源，已提供的文档导航结构和合同分类只用于导航与规则选择，不能替代页面证据。

执行要求：
1. 每轮必须且只能调用本轮实际提供的一个工具，禁止输出普通文本。可用终止工具会根据已经成功提取的对象变化；只能调用当前确实可见的工具。
2. think 用于比较候选证据、已提取对象和剩余对象；自然语言推理只保留在当前定义的短期记忆中。
3. 有充分证据支持一个完整对象时调用 extract_object。每次只提交一个对象，value 必须严格匹配 properties 生成的扁平 Schema。
4. evidence 至少包含一条物理页码和可核对的短内容。一个对象的多个属性可以共享证据，也可分别提供短证据；当前任务不输出视觉坐标。
5. reasoning 只说明证据如何支持该对象及其属性、如何排除混淆，保持简洁且不得重复 value 的 JSON；紧随其后的 value 是本次提取的唯一正式决定。
6. cardinality=single 时，第一次成功调用 extract_object 后自动结束。
7. cardinality=multiple 时，每次成功对象都会写入当前短期记忆。继续查找未提取对象；确认已经穷尽后调用 finish_extraction，理由必须以“因此，当前对象定义已提取完毕。”结束。
8. 尚未成功提取任何对象，且合同没有该对象或证据不足时调用 abandon_extraction；理由必须以“因此，当前对象无法从该合同中可靠提取。”结束。
9. 已成功提取对象后不得放弃整个定义；不要重复提交相同对象，也不要将多个实例合并进一个对象。
10. 不使用文件名、常识、默认值或主观“我方/相对方”视角补全事实。工具返回错误时，根据反馈修正下一轮调用。

{field_definition_guide}

工具调用格式：
{tool_call_xml_instruction}
"""

CORE_FIELD_TASK = """当前唯一提取对象定义如下。定义是对象语义、基数和属性约束的权威来源，不得改名、扩展或创造定义外属性。

```yaml
{field_definition}
```
"""

def serialize_field_definition(definition: FieldDefinition) -> str:
    """按模型提取对象定义顺序生成稳定 YAML。"""
    # code 与 tokenize 仅决定 Elasticsearch mapping，不能污染模型提取任务。
    model_definition = definition.model_dump(mode="json")
    model_definition.pop("code", None)
    for property_definition in model_definition["properties"]:
        property_definition.pop("code", None)
        property_definition.pop("tokenize", None)
    return yaml.safe_dump(
        model_definition,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()


def build_core_common_messages(
    prefill_context: ContractPrefillContext,
) -> list[dict[str, Any]]:
    """在最终合同前缀后追加全部 Core 字段共享的稳定规则。"""
    return append_contract_task(
        prefill_context.messages,
        task_suffix=CORE_COMMON_TASK.format(
            field_definition_guide=FIELD_DEFINITION_GUIDE,
            tool_call_xml_instruction=TOOL_CALL_XML_INSTRUCTION,
        ),
    )


def build_core_messages(
    core_context: CoreContext,
    definition: FieldDefinition,
) -> list[dict[str, Any]]:
    """复用 Core 公共任务前缀，再追加当前对象定义。"""
    return append_contract_task(
        core_context.messages,
        task_suffix=CORE_FIELD_TASK.format(
            field_definition=serialize_field_definition(definition),
        ),
    )


__all__ = [
    "CORE_COMMON_PROMPT_VERSION",
    "CORE_EXTRACTION_PROMPT_VERSION",
    "FIELD_DEFINITION_GUIDE",
    "build_core_common_messages",
    "build_core_messages",
    "serialize_field_definition",
]
