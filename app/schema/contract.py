"""合同定义与正式入库 HTTP 契约。"""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, StringConstraints, field_validator, model_validator

from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldCardinality,
    FieldDefinitionCatalog,
    FieldValueType,
    FieldConstraints,
)
from app.service.contract_extraction.model import ClauseDraftData, CoreDraftData


class ContractSchemaModel(BaseModel):
    """合同定义接口共用的严格不可变模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ContractMetadataResponse(ContractSchemaModel):
    """已成功入库合同的公开元数据，不暴露内部入库状态。"""

    document_id: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="处理版 PDF 的 SHA-256 文档标识。"
    )
    file_name: str = Field(description="用户最终确认的合同展示名称。")
    category: str = Field(description="类别 code 以 / 分隔的摘要；未映射时保留类型说明。")
    contract_time: date | None = Field(description="签订日期 YYYY-MM-DD，缺失时为 null。")
    file_uri: str = Field(description="处理版 PDF 的稳定根相对读取地址。")
    reviewer: str = Field(description="确认最终结果并执行入库的审核人名称。")
    ingested_at: datetime = Field(description="带时区的 ISO 8601 入库时间。")


class ContractSummaryResponse(ContractSchemaModel):
    """已有合同摘要；缺失值不等同于合同不存在。"""
    document_id: str = Field(pattern=r'^[0-9a-f]{64}$', description='合同PDF的SHA-256文档标识。')
    summary: str | None = Field(description='已保存的合同内容摘要，尚未生成时为null；不包含用户注意事项。')


class ContractNoteRequest(ContractSchemaModel):
    """合同身份由路径提供，作者和时间由服务端生成。"""
    content: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=10000)] = Field(
        description='用户撰写的合同注意事项正文，去除首尾空白后为1至10000字符；属于用户意见，不是合同原文。')


class ContractNoteResponse(ContractSchemaModel):
    note_id: str = Field(description='服务端生成的注意事项唯一标识。')
    content: str = Field(description='注意事项正文。')
    author_name: str = Field(description='创建时登录用户的名称快照。')
    created_at: datetime = Field(description='服务端生成的UTC创建时间，ISO 8601格式。')


class ContractCategoryResponse(ContractSchemaModel):
    """前端构建类别选项所需的 SQLite 类别身份。"""

    category_id: int = Field(gt=0, description="当前 SQLite 数据库中的类别主键。")
    code: str = Field(min_length=1, description="权威类别目录的稳定英文代码。")
    name: str = Field(min_length=1, description="类别的标准中文名称。")


class CorePropertyDefinitionResponse(ContractSchemaModel):
    """前端构造一个 Core 属性输入控件所需的定义。"""

    code: str = Field(description="属性在 Core 对象中的稳定英文键。")
    name: str = Field(description="属性供审核人员阅读的中文名称。")
    type: FieldValueType = Field(description="属性允许提交的 JSON 基本类型。")
    required: bool = Field(description="该属性在非空 Core 对象中是否必填。")
    constraints: FieldConstraints = Field(description="枚举、数值范围及倍数约束；未设置的约束为 null。")
    extraction_rule: str = Field(description="字段提取与标准化规则，供审核时参考。")


class CoreFieldDefinitionResponse(ContractSchemaModel):
    """前端构造一个 Core 字段审核区域所需的定义。"""

    code: str = Field(description="字段在 Core 中的稳定英文键。")
    name: str = Field(description="字段供审核人员阅读的中文名称。")
    cardinality: FieldCardinality = Field(
        description="single 表示单项，multiple 表示可增删的多项列表。"
    )
    properties: tuple[CorePropertyDefinitionResponse, ...] = Field(
        min_length=1,
        description="字段每一项需要填写的扁平属性定义。",
    )


class CoreDefinitionCatalogResponse(
    RootModel[tuple[CoreFieldDefinitionResponse, ...]]
):
    """按启动期目录顺序返回的 Core 表单定义列表。"""


class ContractIngestionRequest(ContractSchemaModel):
    """用户提交的完整最终文件名、Core 和 Clause 审核值。"""

    file_name: str = Field(
        min_length=1,
        max_length=255,
        description=(
            "写入 Elasticsearch 的最终展示文件名；不是服务器路径，"
            "不得包含文件系统路径分隔符或控制字符。"
        ),
    )
    summary: str = Field(
        min_length=1,
        max_length=3000,
        description="用户最终确认的合同内容摘要，去除首尾空白后不能为空，最多 3000 个字符；仅写入 SQLite。",
    )

    @field_validator("file_name", "summary", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object) -> object:
        """先去除首尾空白，再执行非空和长度校验。"""
        return value.strip() if isinstance(value, str) else value

    core: CoreDraftData = Field(
        description=(
            "按 Core 定义目录稳定 code 提交的完整审核对象；全部目录字段均须出现，"
            "签订日期 signing_date 入库必填；其他没有最终值的字段使用 null。"
        )
    )
    clauses: ClauseDraftData = Field(
        description=(
            "按原合同阅读顺序提交的完整最终条款；order 从 1 连续增长，"
            "父条款必须先于子条款出现。"
        )
    )


class ContractIngestionAuditResponse(ContractSchemaModel):
    """服务端根据当前登录用户形成的最终责任信息。"""

    reviewer: str = Field(description="确认最终结果并执行入库的审核人名称。")
    ingested_at: datetime = Field(description="Elasticsearch 入库请求的带时区时间。")


class ContractIngestionResponse(ContractSchemaModel):
    """合同正式写入并释放内存运行后的稳定响应。"""

    status: Literal["ingested"] = Field(description="固定的正式入库成功状态。")
    document_id: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="处理版 PDF 的 SHA-256，同时也是 Elasticsearch 文档 _id。",
    )
    file_name: str = Field(description="实际写入 Elasticsearch 的最终展示文件名。")
    file_uri: str = Field(description="处理版 PDF 的稳定根相对读取地址。")
    page_count: int = Field(gt=0, description="处理版 PDF 的物理页数。")
    ingestion: ContractIngestionAuditResponse = Field(
        description="由服务端补充的审核人与入库时间。"
    )


def project_core_definition_catalog(
    catalog: FieldDefinitionCatalog,
) -> CoreDefinitionCatalogResponse:
    """从完整业务定义中筛出前端生成审核表单所需的信息。"""
    return CoreDefinitionCatalogResponse(
        root=tuple(
            CoreFieldDefinitionResponse(
                code=definition.code,
                name=definition.name,
                cardinality=definition.cardinality,
                properties=tuple(
                    CorePropertyDefinitionResponse(
                        code=property_definition.code,
                        name=property_definition.name,
                        type=property_definition.type,
                        required=property_definition.required,
                        constraints=property_definition.constraints,
                        extraction_rule=property_definition.extraction_rule,
                    )
                    for property_definition in definition.properties
                ),
            )
            for definition in catalog.core.definitions
        )
    )


__all__ = [
    "ContractIngestionAuditResponse",
    "ContractIngestionRequest",
    "ContractIngestionResponse",
    "CoreDefinitionCatalogResponse",
    "CoreFieldDefinitionResponse",
    "CorePropertyDefinitionResponse",
    "project_core_definition_catalog",
]


class ContractRelationRequest(ContractSchemaModel):
    """两份合同的无向关联；创建信息由服务端绑定。"""

    document_id_a: str = Field(pattern=r"^[0-9a-f]{64}$", description="第一份已正式入库合同的完整 document_id。")
    document_id_b: str = Field(pattern=r"^[0-9a-f]{64}$", description="第二份已正式入库合同的完整 document_id，必须与第一份不同；关联不区分方向。")
    description: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=10000)] = Field(description="关系描述，去除首尾空白后为 1 至 10000 字符；可说明两份合同的角色及关联原因，创建后不可修改。")

    @model_validator(mode="after")
    def validate_endpoints(self):
        if self.document_id_a == self.document_id_b:
            raise ValueError("不能将合同关联到自身")
        return self


class ContractRelationResponse(ContractSchemaModel):
    relation_id: str = Field(description="服务端生成的关系 UUID；删除重建时不复用。")
    document_id_a: str = Field(description="按字典序排序后较小的完整合同 ID。")
    document_id_b: str = Field(description="按字典序排序后较大的完整合同 ID。")
    description: str = Field(description="创建时保存的关系描述。")
    created_at: datetime = Field(description="后端生成的 UTC 创建时间。")
    created_by: str = Field(description="创建关系时的登录用户名称。")


class ContractNeighborResponse(ContractSchemaModel):
    """指定合同的一跳关联，不重复返回起点合同 ID。"""

    relation_id: str = Field(description="关系 UUID，可用于删除该关联。")
    document_id: str = Field(pattern=r"^[0-9a-f]{64}$", description="与当前合同直接关联的对方合同完整 ID。")
    description: str = Field(description="创建时保存的关系描述，不因查询方向而改写。")
    created_at: datetime = Field(description="关系创建时的 UTC 时间。")
    created_by: str = Field(description="关系创建时的登录用户名称。")
