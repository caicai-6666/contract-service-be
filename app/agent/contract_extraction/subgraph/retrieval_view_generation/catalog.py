"""检索问题 YAML 指南的启动期加载。"""

from __future__ import annotations

from collections.abc import Collection
from hashlib import sha256
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from app.agent.contract_extraction.subgraph.retrieval_view_generation.definition import (
    CategoryQuestionGuide,
    CommonQuestionGuide,
    QuestionGuideCatalog,
    RetrievalViewGuideCatalog,
)

GuideModel = TypeVar("GuideModel", bound=BaseModel)


class RetrievalViewGuideCatalogError(ValueError):
    """提问指南目录无法形成可信内存快照。"""


def _load_yaml_model(path: Path, model: type[GuideModel]) -> GuideModel:
    """读取单个 YAML，并把解析或 Schema 错误定位到具体文件。"""
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RetrievalViewGuideCatalogError(
            f"无法读取检索问题指南 {path}：{exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise RetrievalViewGuideCatalogError(f"指南文件 {path} 的顶层必须是对象")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise RetrievalViewGuideCatalogError(
            f"指南文件 {path} 不符合 Schema：{exc}"
        ) from exc


def _validate_exact_entries(directory: Path, expected: set[str]) -> None:
    """拒绝职责目录缺项及未定义条目。"""
    actual = {entry.name for entry in directory.iterdir()}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    problems: list[str] = []
    if missing:
        problems.append(f"缺少 {missing}")
    if unexpected:
        problems.append(f"存在未定义条目 {unexpected}")
    if problems:
        raise RetrievalViewGuideCatalogError(
            f"指南目录 {directory} 布局错误：{'；'.join(problems)}"
        )


def _load_category_guides(
    directory: Path,
    *,
    known_category_codes: Collection[str] | None,
) -> tuple[tuple[CategoryQuestionGuide, ...], tuple[Path, ...]]:
    """按文件名加载可稀疏扩充的领域提问指南。"""
    if not directory.is_dir():
        raise RetrievalViewGuideCatalogError(f"分类指南目录不存在：{directory}")
    paths = tuple(sorted(directory.iterdir(), key=lambda path: path.name))
    invalid = [
        path.name for path in paths if not path.is_file() or path.suffix != ".yaml"
    ]
    if invalid:
        raise RetrievalViewGuideCatalogError(
            f"分类指南目录 {directory} 包含非 YAML 文件：{invalid}"
        )

    guides: list[CategoryQuestionGuide] = []
    for path in paths:
        guide = _load_yaml_model(path, CategoryQuestionGuide)
        expected_name = f"{guide.category_code.replace('_', '-')}.yaml"
        if path.name != expected_name:
            raise RetrievalViewGuideCatalogError(
                f"分类指南文件 {path.name} 与 category_code "
                f"{guide.category_code} 不一致；文件应为 {expected_name}"
            )
        if (
            known_category_codes is not None
            and guide.category_code not in known_category_codes
        ):
            raise RetrievalViewGuideCatalogError(
                f"分类指南 {path} 引用了未知合同类别 {guide.category_code}"
            )
        guides.append(guide)

    codes = [guide.category_code for guide in guides]
    if len(codes) != len(set(codes)):
        raise RetrievalViewGuideCatalogError("分类指南 category_code 不能重复")
    return tuple(guides), paths


def _content_fingerprint(root: Path, paths: tuple[Path, ...]) -> str:
    """把相对路径和原始字节共同纳入确定性内容指纹。"""
    digest = sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def load_retrieval_view_guide_catalog(
    root: Path,
    *,
    known_category_codes: Collection[str] | None = None,
) -> RetrievalViewGuideCatalog:
    """全量加载提问指南，任一结构错误都会明确失败。"""
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise RetrievalViewGuideCatalogError(
            f"检索问题指南目录不存在或不是目录：{resolved_root}"
        )
    _validate_exact_entries(resolved_root, {"question"})

    question_root = resolved_root / "question"
    if not question_root.is_dir():
        raise RetrievalViewGuideCatalogError(f"指南职责目录不存在：{question_root}")
    _validate_exact_entries(question_root, {"common.yaml", "category"})

    common_path = question_root / "common.yaml"
    common = _load_yaml_model(common_path, CommonQuestionGuide)
    categories, category_paths = _load_category_guides(
        question_root / "category",
        known_category_codes=known_category_codes,
    )
    question_paths = (common_path, *category_paths)
    fingerprint = _content_fingerprint(resolved_root, question_paths)
    try:
        return RetrievalViewGuideCatalog(
            root=resolved_root,
            question=QuestionGuideCatalog(
                root=question_root,
                common=common,
                categories=categories,
                content_sha256=fingerprint,
            ),
            content_sha256=fingerprint,
        )
    except ValidationError as exc:
        raise RetrievalViewGuideCatalogError(
            f"检索问题指南目录不符合跨文件约束：{exc}"
        ) from exc


__all__ = [
    "RetrievalViewGuideCatalogError",
    "load_retrieval_view_guide_catalog",
]
