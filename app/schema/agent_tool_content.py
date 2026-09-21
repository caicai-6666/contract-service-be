"""工具返回的内容类别；与执行成功/失败及 FIFO 管理状态独立。"""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from .communication_workspace import WorkspaceObject, WorkspaceText


class OrdinaryToolContent(WorkspaceObject):
    type: Literal['ordinary'] = Field(default='ordinary', description='普通结果，tool_result 按现有规则进入工具反馈，不进行页面折叠。')


class ToolPageReference(WorkspaceObject):
    resource_id: WorkspaceText = Field(description='工具管理的资源标识；不携带文件原文、整页数据或图片 Base64。')
    display_id: WorkspaceText = Field(description='本次页面展示的独立标识；重新打开同一页应生成新标识。')
    locator: WorkspaceText = Field(description='资源内部的页面定位，例如第3页或第51–100行；具体含义由对应工具解释。')
    media_type: Literal['text', 'table', 'image'] = Field(description='引用内容的展示形式；实际内容由后续资源解析与上下文展示层取得。')
    description: WorkspaceText = Field(description='可保留在轨迹中的简短页面说明，不填整页内容。')


class FoldableToolContent(WorkspaceObject):
    type: Literal['foldable'] = Field(default='foldable', description='可折叠结果，包含图片、表格或长文本页面引用；普通说明保留在 tool_result，大容量内容不内嵌。')
    pages: list[ToolPageReference] = Field(min_length=1, description='本次打开的页面引用；一项对应一次独立展示。')

    @model_validator(mode='after')
    def unique_displays(self):
        ids = [page.display_id for page in self.pages]
        if len(ids) != len(set(ids)):
            raise ValueError('同次返回的页面展示标识不能重复')
        return self


ToolContent = Annotated[OrdinaryToolContent | FoldableToolContent, Field(discriminator='type')]
ToolContentType = Literal['ordinary', 'foldable']
