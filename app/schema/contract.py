"""合同定义、待审提交与正式合同管理的 HTTP 契约。"""

from uuid import UUID
from app.schema.ingestion_review import ReviewStatus, ReviewIngestionStatus

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
    uploader: str = Field(description="在本平台提交待审核申请的上传人名称，来自 submitted_by，不是外部审核员。")
    ingested_at: datetime = Field(description="带时区的 ISO 8601 入库时间。")
    can_delete: bool = Field(description="是否允许发起删除申请；等待删除审核时为 false。")


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
    """用户提交的完整最终文件名、摘要、备注、Core 和 Clause 确认值。"""

    file_name: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "送审的最终合同展示文件名；不是服务器路径，"
            "不得包含文件系统路径分隔符或控制字符。"
        ),
    )
    summary: str = Field(
        min_length=1,
        max_length=3000,
        description="用户最终确认的合同内容摘要，去除首尾空白后不能为空，最多 3000 个字符；先保存至待审快照，批准后写入正式 SQLite。",
    )

    note: str = Field(
        max_length=10000, strict=True,
        description="必填的入库员审核沟通备注，允许空字符串；最多 10000 字符，仅保存至待审记录并发送中间件。",
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


class ContractIngestionResponse(ContractSchemaModel):
    """持久化待审申请的回执，不代表已发送或已正式入库。"""

    status: Literal["submitted"] = Field(description="申请已保存；不表示审核通过或正式入库。")
    submission_id: UUID = Field(description="本地待审申请唯一标识，重复提交相同内容时保持不变。")
    document_id: str = Field(pattern=r"^[0-9a-f]{64}$", description="处理版 PDF 的 SHA-256。")
    file_name: str = Field(description="用户确认的送审合同名称。")
    page_count: int = Field(gt=0, description="处理版 PDF 的物理页数。")
    submitted_by: str = Field(description="由当前认证身份确定的入库员名称。")
    submitted_at: datetime = Field(description="首次保存待审申请的带时区时间。")
    review_status: ReviewStatus = Field(description="当前审核状态；首次通常为 pending_send，后台任务可能已经推进。")
    ingestion_status: ReviewIngestionStatus = Field(description="当前正式入库状态；首次为 pending。")


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
