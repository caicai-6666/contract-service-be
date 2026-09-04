"""合同定义与正式入库 HTTP 契约。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldCardinality,
    FieldDefinitionCatalog,
    FieldValueType,
)
from app.service.contract_extraction.model import ClauseDraftData, CoreDraftData


class ContractSchemaModel(BaseModel):
    """合同定义接口共用的严格不可变模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CorePropertyDefinitionResponse(ContractSchemaModel):
    """前端构造一个 Core 属性输入控件所需的定义。"""

    code: str = Field(description="属性在 Core 对象中的稳定英文键。")
    name: str = Field(description="属性供审核人员阅读的中文名称。")
    type: FieldValueType = Field(description="属性允许提交的 JSON 基本类型。")
    required: bool = Field(description="该属性在非空 Core 对象中是否必填。")


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
    core: CoreDraftData = Field(
        description=(
            "按 Core 定义目录稳定 code 提交的完整审核对象；全部目录字段均须出现，"
            "没有最终值的字段使用 null。"
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
