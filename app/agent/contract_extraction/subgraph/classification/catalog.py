"""合同类别 YAML 目录的启动期加载与全量校验。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from app.agent.contract_extraction.subgraph.classification.definition import (
    ContractCategory,
    ContractCategoryCatalog,
    ContractCategoryDefinition,
    ExpertExampleCard,
)

DefinitionModel = TypeVar("DefinitionModel", bound=BaseModel)


class ContractCategoryCatalogError(ValueError):
    """类别目录无法形成可信内存快照。"""


def _load_yaml_model(
    path: Path,
    model: type[DefinitionModel],
) -> DefinitionModel:
    """读取单个 YAML，并把解析或 Schema 错误定位到具体文件。"""
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ContractCategoryCatalogError(f"无法读取类别文件 {path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise ContractCategoryCatalogError(f"类别文件 {path} 的顶层必须是对象")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ContractCategoryCatalogError(
            f"类别文件 {path} 不符合 Schema：{exc}"
        ) from exc


def _validate_exact_entries(
    directory: Path,
    expected: set[str],
) -> None:
    """拒绝目录中缺失的职责文件和未定义的额外文件。"""
    actual = {entry.name for entry in directory.iterdir()}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    problems: list[str] = []
    if missing:
        problems.append(f"缺少 {missing}")
    if unexpected:
        problems.append(f"存在未定义条目 {unexpected}")
    if problems:
        raise ContractCategoryCatalogError(
            f"类别目录 {directory} 布局错误：{'；'.join(problems)}"
        )


def _load_example_directory(directory: Path) -> tuple[ExpertExampleCard, ...]:
    """按文件名稳定加载一个 positive 或 negative 目录。"""
    if not directory.is_dir():
        raise ContractCategoryCatalogError(f"专家示例目录不存在：{directory}")
    entries = sorted(directory.iterdir(), key=lambda path: path.name)
    invalid = [
        entry.name
        for entry in entries
        if not entry.is_file() or entry.suffix != ".yaml"
    ]
    if invalid:
        raise ContractCategoryCatalogError(
            f"专家示例目录 {directory} 包含非 YAML 文件：{invalid}"
        )
    if len(entries) < 3:
        raise ContractCategoryCatalogError(
            f"专家示例目录 {directory} 至少需要 3 张卡片"
        )
    return tuple(_load_yaml_model(path, ExpertExampleCard) for path in entries)


def _catalog_fingerprint(root: Path, paths: tuple[Path, ...]) -> str:
    """把相对路径和原始文件字节共同纳入确定性内容指纹。"""
    digest = sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def load_contract_category_catalog(root: Path) -> ContractCategoryCatalog:
    """全量加载类别定义与正反例，任一错误都会阻止应用启动。"""
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise ContractCategoryCatalogError(
            f"合同类别定义目录不存在或不是目录：{resolved_root}"
        )

    root_entries = sorted(resolved_root.iterdir(), key=lambda path: path.name)
    invalid_root_entries = [entry.name for entry in root_entries if not entry.is_dir()]
    if invalid_root_entries:
        raise ContractCategoryCatalogError(
            f"合同类别根目录只能包含类别子目录：{invalid_root_entries}"
        )
    if not root_entries:
        raise ContractCategoryCatalogError("合同类别定义目录不能为空")

    categories: list[ContractCategory] = []
    loaded_paths: list[Path] = []
    for category_directory in root_entries:
        _validate_exact_entries(
            category_directory,
            {"definition.yaml", "positive", "negative"},
        )
        definition_path = category_directory / "definition.yaml"
        if not definition_path.is_file():
            raise ContractCategoryCatalogError(f"类别定义不存在：{definition_path}")
        definition = _load_yaml_model(
            definition_path,
            ContractCategoryDefinition,
        )
        expected_directory_name = definition.code.replace("_", "-")
        if category_directory.name != expected_directory_name:
            raise ContractCategoryCatalogError(
                f"类别目录 {category_directory.name} 与 code {definition.code} 不一致；"
                f"目录应为 {expected_directory_name}"
            )

        positive_directory = category_directory / "positive"
        negative_directory = category_directory / "negative"
        positive_examples = _load_example_directory(positive_directory)
        negative_examples = _load_example_directory(negative_directory)
        categories.append(
            ContractCategory(
                definition=definition,
                positive_examples=positive_examples,
                negative_examples=negative_examples,
            )
        )
        loaded_paths.extend(
            (
                definition_path,
                *sorted(positive_directory.iterdir(), key=lambda path: path.name),
                *sorted(negative_directory.iterdir(), key=lambda path: path.name),
            )
        )

    codes = [category.definition.code for category in categories]
    if len(codes) != len(set(codes)):
        raise ContractCategoryCatalogError("合同类别 code 不能重复")
    known_codes = set(codes)
    for category in categories:
        for distinction in category.definition.distinguish_from:
            if distinction.category not in known_codes:
                raise ContractCategoryCatalogError(
                    f"类别 {category.definition.code} 引用了未知相邻类别 "
                    f"{distinction.category}"
                )

    ordered_categories = tuple(
        sorted(categories, key=lambda category: category.definition.code)
    )
    return ContractCategoryCatalog(
        root=resolved_root,
        categories=ordered_categories,
        content_sha256=_catalog_fingerprint(resolved_root, tuple(loaded_paths)),
    )


__all__ = [
    "ContractCategoryCatalogError",
    "load_contract_category_catalog",
]
