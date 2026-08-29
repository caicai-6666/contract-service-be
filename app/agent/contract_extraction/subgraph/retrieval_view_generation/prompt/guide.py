"""把权威提问 YAML 对象确定性渲染为模型可读 Bullet。"""

from __future__ import annotations

from collections.abc import Sequence

from app.agent.contract_extraction.subgraph.retrieval_view_generation.definition import (
    CategoryQuestionGuide,
    CommonQuestionGuide,
    QuestionAttentionPoint,
    RetrievalViewGuideCatalog,
)

QUESTION_GUIDE_RENDER_VERSION = "retrieval-view-question-guide-v3"

QUESTION_GUIDE_CATALOG_BEGIN = "===== 合同检索提问指南目录：开始 ====="
QUESTION_GUIDE_CATALOG_END = "===== 合同检索提问指南目录：结束 ====="
SELECTED_QUESTION_GUIDES_BEGIN = "===== 当前问题规划相关提问指南：开始 ====="
SELECTED_QUESTION_GUIDES_END = "===== 当前问题规划相关提问指南：结束 ====="
COMMON_QUESTION_GUIDE_BEGIN = "===== 合同检索提问通用指南：开始 ====="
COMMON_QUESTION_GUIDE_END = "===== 合同检索提问通用指南：结束 ====="


def _append_nested_list(
    lines: list[str],
    *,
    label: str,
    values: Sequence[str],
    indent: int,
) -> None:
    """使用固定缩进追加一个有标签的 Bullet 列表。"""
    prefix = " " * indent
    lines.append(f"{prefix}- {label}：")
    item_prefix = " " * (indent + 2)
    lines.extend(f"{item_prefix}- {value}" for value in values)


def _render_question_attention_point(
    point: QuestionAttentionPoint,
    *,
    namespace: str,
    index: int,
    total: int,
) -> list[str]:
    """渲染一个提问关注点，不暴露 YAML 字段名。"""
    lines = [
        f"  - 关注点 {index}/{total}：{point.name}",
        f"    - 稳定标识：{namespace}.{point.code}",
        f"    - 法律意义：{point.legal_significance}",
        f"    - 行业实践意义：{point.practice_significance}",
    ]
    _append_nested_list(
        lines,
        label="适用情形",
        values=point.applicable_when,
        indent=4,
    )
    _append_nested_list(
        lines,
        label="重点检查",
        values=point.inspect_for,
        indent=4,
    )
    _append_nested_list(
        lines,
        label="未明确约定时仍值得提问",
        values=point.material_if_missing,
        indent=4,
    )
    _append_nested_list(
        lines,
        label="不属于该关注点",
        values=point.excludes,
        indent=4,
    )
    return lines


def render_common_question_guide(guide: CommonQuestionGuide) -> str:
    """把通用提问指南渲染为稳定 Bullet 区块。"""
    lines = [
        COMMON_QUESTION_GUIDE_BEGIN,
        f"- 指南名称：{guide.name}",
        f"- 指南目的：{guide.purpose}",
    ]
    _append_nested_list(
        lines,
        label="选题原则",
        values=guide.selection_rules,
        indent=0,
    )
    _append_nested_list(
        lines,
        label="问题表达规则",
        values=guide.question_rules,
        indent=0,
    )
    lines.append("- 通用关注点：")
    total = len(guide.attention_points)
    for index, point in enumerate(guide.attention_points, start=1):
        lines.extend(
            _render_question_attention_point(
                point,
                namespace="common",
                index=index,
                total=total,
            )
        )
    lines.append(COMMON_QUESTION_GUIDE_END)
    return "\n".join(lines)


def render_category_question_guide(guide: CategoryQuestionGuide) -> str:
    """把一个领域提问指南渲染为稳定 Bullet 区块。"""
    begin = f"===== 合同检索提问领域指南 {guide.category_code}：开始 ====="
    end = f"===== 合同检索提问领域指南 {guide.category_code}：结束 ====="
    lines = [
        begin,
        f"- 领域命名空间：{guide.category_name}（{guide.category_code}）",
        f"- 指南目的：{guide.purpose}",
    ]
    _append_nested_list(
        lines,
        label="领域选题原则",
        values=guide.selection_rules,
        indent=0,
    )
    lines.append("- 领域专项关注点：")
    total = len(guide.attention_points)
    for index, point in enumerate(guide.attention_points, start=1):
        lines.extend(
            _render_question_attention_point(
                point,
                namespace=guide.category_code,
                index=index,
                total=total,
            )
        )
    lines.append(end)
    return "\n".join(lines)


def render_question_guides(catalog: RetrievalViewGuideCatalog) -> str:
    """按目录稳定顺序渲染通用指南和全部领域提问指南。"""
    sections = (
        render_common_question_guide(catalog.question.common),
        *(
            render_category_question_guide(guide)
            for guide in catalog.question.categories
        ),
    )
    return "\n".join(
        (
            QUESTION_GUIDE_CATALOG_BEGIN,
            "- 使用方式：结合当前合同，从通用指南和下列全部领域指南中选择真正适用的关注点；领域名称只是组织命名空间，不表示合同已被归入该领域。",
            "- 去重要求：跨领域语义相同或可由同一个问题覆盖的关注点只生成一个问题。",
            "",
            "\n\n".join(sections),
            QUESTION_GUIDE_CATALOG_END,
        )
    )


def _get_question_attention_point(
    catalog: RetrievalViewGuideCatalog,
    stable_code: str,
) -> tuple[str, QuestionAttentionPoint]:
    """按 namespace.code 精确取得一个权威提问关注点。"""
    try:
        namespace, point_code = stable_code.split(".", maxsplit=1)
    except ValueError as exc:
        raise KeyError(
            f"提问关注点稳定标识必须使用 namespace.code：{stable_code}"
        ) from exc

    if namespace == "common":
        points = catalog.question.common.attention_points
    else:
        try:
            points = catalog.question.get_category(namespace).attention_points
        except KeyError as exc:
            raise KeyError(f"提问指南中不存在关注点 {stable_code}") from exc

    for point in points:
        if point.code == point_code:
            return namespace, point
    raise KeyError(f"提问指南中不存在关注点 {stable_code}")


def render_selected_question_guides(
    catalog: RetrievalViewGuideCatalog,
    attention_codes: Sequence[str],
) -> str:
    """按问题规划中的稳定标识只渲染真正相关的权威关注点。"""
    if not attention_codes:
        raise ValueError("当前问题规划至少需要一个提问指南关注点标识")
    if len(attention_codes) != len(set(attention_codes)):
        raise ValueError("当前问题规划中的提问指南关注点标识不能重复")

    selected = tuple(
        _get_question_attention_point(catalog, stable_code)
        for stable_code in attention_codes
    )
    total = len(selected)
    lines = [
        SELECTED_QUESTION_GUIDES_BEGIN,
        "- 使用方式：以下关注点由既定问题规划精确选择；应共同服务于同一个自然用户问题，不得扩展到未列出的指南事项。",
        f"- 组合数量：{total} 个紧密相关的指南关注点。",
        "- 相关权威关注点：",
    ]
    for index, (namespace, point) in enumerate(selected, start=1):
        lines.extend(
            _render_question_attention_point(
                point,
                namespace=namespace,
                index=index,
                total=total,
            )
        )
    lines.append(SELECTED_QUESTION_GUIDES_END)
    return "\n".join(lines)


__all__ = [
    "COMMON_QUESTION_GUIDE_BEGIN",
    "COMMON_QUESTION_GUIDE_END",
    "QUESTION_GUIDE_CATALOG_BEGIN",
    "QUESTION_GUIDE_CATALOG_END",
    "QUESTION_GUIDE_RENDER_VERSION",
    "SELECTED_QUESTION_GUIDES_BEGIN",
    "SELECTED_QUESTION_GUIDES_END",
    "render_category_question_guide",
    "render_common_question_guide",
    "render_question_guides",
    "render_selected_question_guides",
]
