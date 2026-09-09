"""业务门禁子图的初始化状态。"""

from typing import Literal

from typing_extensions import TypedDict


class BusinessGateSubgraphState(TypedDict, total=False):
    """仅表达尚未实现校验，不预设后续问题、文件或权限输入契约。"""

    # 子图执行结束不等于准入通过；实际门禁规则实现前不产生放行结论。
    status: Literal["not_implemented"]


__all__ = ["BusinessGateSubgraphState"]
