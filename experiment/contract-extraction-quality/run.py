"""批量运行合同提取质量与推理指标实验。"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import subprocess
import sys
import traceback
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import BaseModel

from app.agent.contract_extraction.progress import (
    ParallelProgressUpdate,
)
from app.agent.contract_extraction.state import (
    ContractExtractionRequest,
)
from app.agent.contract_extraction.subgraph.classification.catalog import (
    load_contract_category_catalog,
)
from app.agent.contract_extraction.subgraph.field_extraction.catalog import (
    load_field_definition_catalog,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.catalog import (
    load_retrieval_view_guide_catalog,
)
from app.core.config import get_settings
from app.infrastructure.inference_metrics import (
    InferenceRequestMetrics,
    bind_inference_metrics_observer,
    bind_inference_stage,
)
from app.service.contract_extraction.executor import (
    AgentContractExtractionExecutor,
    DocumentUnderstandingOutput,
    ExtractionContext,
    RetrievalViewOutput,
)
from app.service.pdf_preparation import AsyncPDFPreparationService

EXPERIMENT_NAME = "contract-extraction-quality"
DEFAULT_INPUT_DIR = PROJECT_ROOT / "data/input/test-data"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "experiment" / EXPERIMENT_NAME / "output"


def parse_args() -> argparse.Namespace:
    """解析可复现实验参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="递归查找 PDF 的输入目录。",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="UTC 运行目录的父目录。",
    )
    parser.add_argument(
        "--max-contracts",
        type=int,
        default=None,
        help="只运行排序后的前 N 份 PDF；用于冒烟验证。",
    )
    return parser.parse_args()


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(UTC)


def utc_text(value: datetime | None = None) -> str:
    """生成 JSON 使用的 UTC ISO 时间。"""
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def output_timestamp(value: datetime | None = None) -> str:
    """生成不会覆盖历史实验的 UTC 目录名。"""
    return (value or utc_now()).strftime("%Y%m%dT%H%M%S.%fZ")


def write_json(path: Path, value: object) -> None:
    """以稳定、可审阅的 UTF-8 格式写入实验产物。"""
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def git_value(*arguments: str) -> str | None:
    """尽力读取 Git 元数据，不让缺少 Git 阻断实验。"""
    completed = subprocess.run(
        ["git", *arguments],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def git_dirty_fingerprint() -> str | None:
    """记录当前工作区差异指纹，但不复制源代码到 manifest。"""
    completed = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        return None
    return hashlib.sha256(completed.stdout).hexdigest()


def model_json(
    value: BaseModel | None,
    *,
    exclude: object | None = None,
) -> object | None:
    """序列化 Pydantic 结果，并允许剔除图片或高维向量。"""
    if value is None:
        return None
    return value.model_dump(mode="json", exclude=exclude)


def error_json(error: BaseException) -> dict[str, object]:
    """保存有限错误信息和回溯，避免整个实验因单样本中止。"""
    return {
        "type": type(error).__name__,
        "message": str(error),
        "traceback": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )[-12000:],
    }


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """使用线性插值计算百分位。"""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(
        ordered[lower] * (1 - weight) + ordered[upper] * weight,
        3,
    )


def numeric_summary(values: Iterable[int | float | None]) -> dict[str, object]:
    """汇总一组同单位数值；缺失值不进入样本数。"""
    available = [float(value) for value in values if value is not None]
    if not available:
        return {
            "sample_count": 0,
            "mean": None,
            "p50": None,
            "p95": None,
            "max": None,
        }
    return {
        "sample_count": len(available),
        "mean": round(sum(available) / len(available), 3),
        "p50": percentile(available, 0.50),
        "p95": percentile(available, 0.95),
        "max": round(max(available), 3),
    }


def summarize_inference(
    records: Sequence[InferenceRequestMetrics],
) -> dict[str, object]:
    """按 README 口径汇总请求、token 和 vLLM 性能指标。"""
    server_names = (
        "time_to_first_token_ms",
        "queue_time_ms",
        "generation_time_ms",
        "mean_itl_ms",
        "tokens_per_second",
    )
    return {
        "request_count": len(records),
        "successful_request_count": sum(record.succeeded for record in records),
        "failed_request_count": sum(not record.succeeded for record in records),
        "server_metrics_request_count": sum(
            record.server_metrics is not None for record in records
        ),
        "prompt_tokens_total": sum(
            record.prompt_tokens or 0 for record in records
        ),
        "cached_tokens_total": sum(
            record.cached_tokens or 0 for record in records
        ),
        "completion_tokens_total": sum(
            record.completion_tokens or 0 for record in records
        ),
        "total_tokens_total": sum(record.total_tokens or 0 for record in records),
        "client_elapsed_ms": numeric_summary(
            record.elapsed_ms for record in records
        ),
        "client_elapsed_ms_sum": round(
            sum(record.elapsed_ms for record in records),
            3,
        ),
        "server_metrics": {
            name: numeric_summary(
                (
                    record.server_metrics.get(name)
                    if record.server_metrics is not None
                    else None
                )
                for record in records
            )
            for name in server_names
        },
    }


def grouped_inference_summary(
    records: Sequence[InferenceRequestMetrics],
) -> dict[str, object]:
    """同时形成总计、模型类型和业务阶段指标。"""
    by_provider: dict[str, list[InferenceRequestMetrics]] = defaultdict(list)
    by_stage: dict[str, list[InferenceRequestMetrics]] = defaultdict(list)
    for record in records:
        by_provider[record.provider].append(record)
        by_stage[record.stage or "unassigned"].append(record)
    return {
        "overall": summarize_inference(records),
        "by_provider": {
            name: summarize_inference(items)
            for name, items in sorted(by_provider.items())
        },
        "by_stage": {
            name: summarize_inference(items)
            for name, items in sorted(by_stage.items())
        },
    }


def summarize_extraction(
    *,
    document_understanding: DocumentUnderstandingOutput | None,
    context: ExtractionContext | None,
    core: BaseModel | None,
    clause: BaseModel | None,
    retrieval: RetrievalViewOutput | None,
    branch_errors: dict[str, dict[str, object]],
) -> dict[str, object]:
    """计算不依赖人工真值的结构完整性指标。"""
    structure = (
        document_understanding.document_structure
        if document_understanding
        else None
    )
    locations = tuple(getattr(structure, "unit_locations", ()))
    classification = context.classification if context else None
    core_fields = tuple(getattr(core, "fields", ()))
    clauses = tuple(getattr(clause, "clauses", ()))
    questions = retrieval.questions if retrieval else None
    embeddings = retrieval.embeddings if retrieval else None
    vector = retrieval.vector if retrieval else None

    core_statuses = Counter(
        getattr(item, "status", "unknown") for item in core_fields
    )
    clause_statuses = Counter(
        getattr(item, "status", "unknown") for item in clauses
    )
    location_statuses = Counter(
        getattr(item, "status", "unknown") for item in locations
    )
    extracted_object_count = sum(
        len(getattr(item, "objects", ())) for item in core_fields
    )
    extracted_clause_characters = sum(
        len(getattr(item, "content", "") or "")
        for item in clauses
        if getattr(item, "status", None) == "extracted"
    )
    succeeded_branches = sum(
        branch is not None for branch in (core, clause, retrieval)
    )
    return {
        "workflow_status": (
            "completed"
            if document_understanding is not None
            and context is not None
            and succeeded_branches > 0
            else "failed"
        ),
        "successful_branch_count": succeeded_branches,
        "failed_branch_count": len(branch_errors),
        "branch_errors": branch_errors,
        "document": {
            "page_count": (
                document_understanding.prepared_pdf.page_count
                if document_understanding
                else None
            ),
            "source_file_size_bytes": (
                document_understanding.prepared_pdf.source_file_size_bytes
                if document_understanding
                else None
            ),
            "processed_file_size_bytes": (
                document_understanding.prepared_pdf.processed_file_size_bytes
                if document_understanding
                else None
            ),
            "total_visual_tokens": (
                document_understanding.prepared_pdf.total_visual_tokens
                if document_understanding
                else None
            ),
            "unit_count": len(getattr(structure, "units", ())) if structure else 0,
            "located_unit_count": location_statuses.get("located", 0),
            "failed_unit_count": location_statuses.get("failed", 0),
        },
        "classification": {
            "status": getattr(classification, "status", None),
            "match_count": len(getattr(classification, "matches", ())),
            "matched_category_codes": [
                match.decision.category_code
                for match in getattr(classification, "matches", ())
            ],
            "failed_category_count": len(
                getattr(classification, "failed_category_codes", ())
            ),
        },
        "core": {
            "status": getattr(core, "status", None),
            "field_count": len(core_fields),
            "extracted_field_count": core_statuses.get("extracted", 0),
            "abandoned_field_count": core_statuses.get("abandoned", 0),
            "failed_field_count": core_statuses.get("failed", 0),
            "extracted_object_count": extracted_object_count,
        },
        "clause": {
            "status": getattr(clause, "status", None),
            "clause_count": len(clauses),
            "extracted_clause_count": clause_statuses.get("extracted", 0),
            "failed_clause_count": clause_statuses.get("failed", 0),
            "extracted_content_characters": extracted_clause_characters,
        },
        "retrieval": {
            "question_status": getattr(questions, "status", None),
            "question_count": len(getattr(questions, "questions", ())),
            "embedding_status": getattr(embeddings, "status", None),
            "embedding_count": len(getattr(embeddings, "embeddings", ())),
            "failed_embedding_count": len(
                getattr(embeddings, "failed_question_ids", ())
            ),
            "vector_status": getattr(vector, "status", None),
            "vector_dimensions": getattr(vector, "dimensions", None),
            "vector_normalized": getattr(vector, "normalized", None),
        },
    }


async def run_with_stage(
    stage: str,
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    """使当前业务阶段传播到其全部并发模型请求。"""
    with bind_inference_stage(stage):
        return await operation()


async def run_contract(
    *,
    index: int,
    pdf_path: Path,
    input_root: Path,
    output_root: Path,
    executor: AgentContractExtractionExecutor,
    pdf_preparation_service: AsyncPDFPreparationService,
) -> tuple[dict[str, object], list[InferenceRequestMetrics]]:
    """执行一份合同并立即固化其原始产物。"""
    sample_dir = output_root / "samples" / f"{index:03d}"
    sample_dir.mkdir(parents=True, exist_ok=False)
    relative_path = pdf_path.relative_to(input_root).as_posix()
    metrics: list[InferenceRequestMetrics] = []
    progress: list[dict[str, object]] = []
    started_at = utc_now()
    started_counter = perf_counter()

    def observe(metric: InferenceRequestMetrics) -> None:
        metrics.append(metric)

    async def document_understanding_update(
        node_name: str,
        values: dict[str, Any],
    ) -> None:
        progress.append(
            {
                "observed_at": utc_text(),
                "stage": "document_understanding",
                "node": node_name,
                "updated_keys": sorted(values),
            }
        )

    def parallel_progress(stage: str):
        async def callback(update: ParallelProgressUpdate) -> None:
            progress.append(
                {
                    "observed_at": utc_text(),
                    "stage": stage,
                    "phase": update.phase.value,
                    "completed": update.completed,
                    "total": update.total,
                }
            )

        return callback

    document_understanding: DocumentUnderstandingOutput | None = None
    context: ExtractionContext | None = None
    core: BaseModel | None = None
    clause: BaseModel | None = None
    retrieval: RetrievalViewOutput | None = None
    branch_errors: dict[str, dict[str, object]] = {}
    fatal_error: dict[str, object] | None = None

    print(f"[{index}] 开始：{relative_path}", flush=True)
    with bind_inference_metrics_observer(observe):
        try:
            prepared_pdf = await pdf_preparation_service.prepare(
                ContractExtractionRequest(pdf_path=pdf_path)
            )
            progress.append(
                {
                    "observed_at": utc_text(),
                    "stage": "pdf_preparation",
                    "operation": "prepare",
                    "updated_keys": ["prepared_pdf"],
                }
            )
            document_understanding = await run_with_stage(
                "document_understanding",
                lambda: executor.understand_document(
                    prepared_pdf,
                    document_understanding_update,
                ),
            )
            context = await run_with_stage(
                "classification",
                lambda: executor.classify(
                    document_understanding,
                    parallel_progress("classification"),
                ),
            )

            branch_names = ("core", "clause", "retrieval")
            outcomes = await asyncio.gather(
                run_with_stage(
                    "core",
                    lambda: executor.extract_core(
                        context,
                        parallel_progress("core"),
                    ),
                ),
                run_with_stage(
                    "clause",
                    lambda: executor.extract_clause(
                        context,
                        parallel_progress("clause"),
                    ),
                ),
                run_with_stage(
                    "retrieval",
                    lambda: executor.prepare_retrieval_view(
                        context,
                        parallel_progress("retrieval"),
                    ),
                ),
                return_exceptions=True,
            )
            for branch_name, outcome in zip(branch_names, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    branch_errors[branch_name] = error_json(outcome)
                elif branch_name == "core":
                    core = outcome
                elif branch_name == "clause":
                    clause = outcome
                else:
                    retrieval = outcome
        except BaseException as exc:
            fatal_error = error_json(exc)

    elapsed_ms = round((perf_counter() - started_counter) * 1000, 3)
    extraction_artifact = {
        "sample": {
            "relative_path": relative_path,
            "started_at": utc_text(started_at),
            "finished_at": utc_text(),
            "elapsed_ms": elapsed_ms,
        },
        "fatal_error": fatal_error,
        "document_understanding": (
            {
                "prepared_pdf": model_json(
                    document_understanding.prepared_pdf,
                    exclude={"pages": {"__all__": {"png_bytes"}}},
                ),
                "document_structure": model_json(
                    document_understanding.document_structure
                ),
                "unit_discovery_audit": model_json(
                    document_understanding.unit_discovery_audit
                ),
                "unit_grounding_audit": model_json(
                    document_understanding.unit_grounding_audit
                ),
            }
            if document_understanding is not None
            else None
        ),
        "classification": (
            {
                "result": model_json(context.classification),
                "audit": model_json(context.classification_audit),
            }
            if context is not None
            else None
        ),
        "core": model_json(core),
        "clause": model_json(clause),
        "retrieval": (
            {
                "questions": model_json(retrieval.questions),
                "embeddings": model_json(
                    retrieval.embeddings,
                    exclude={"embeddings": {"__all__": {"vector"}}},
                ),
                "contract_vector": model_json(
                    retrieval.vector,
                    exclude={"vector"},
                ),
            }
            if retrieval is not None
            else None
        ),
        "branch_errors": branch_errors,
    }
    write_json(sample_dir / "extraction.json", extraction_artifact)
    write_json(
        sample_dir / "inference-metrics.json",
        {
            "records": [record.to_dict() for record in metrics],
            "summary": grouped_inference_summary(metrics),
        },
    )
    write_json(sample_dir / "progress.json", {"events": progress})

    quality = summarize_extraction(
        document_understanding=document_understanding,
        context=context,
        core=core,
        clause=clause,
        retrieval=retrieval,
        branch_errors=branch_errors,
    )
    if fatal_error is not None:
        quality["fatal_error"] = fatal_error
        quality["workflow_status"] = "failed"
    sample_result = {
        "index": index,
        "relative_path": relative_path,
        "artifact_directory": sample_dir.relative_to(output_root).as_posix(),
        "started_at": utc_text(started_at),
        "finished_at": utc_text(),
        "elapsed_ms": elapsed_ms,
        "quality": quality,
        "inference": grouped_inference_summary(metrics),
    }
    print(
        f"[{index}] 完成：status={quality['workflow_status']} "
        f"elapsed={elapsed_ms / 1000:.1f}s requests={len(metrics)}",
        flush=True,
    )
    return sample_result, metrics


def build_manifest(
    *,
    started_at: datetime,
    input_root: Path,
    pdf_paths: Sequence[Path],
    category_catalog: BaseModel,
    field_catalog: BaseModel,
    retrieval_catalog: BaseModel,
) -> dict[str, object]:
    """构造不包含秘密的静态复现信息。"""
    settings = get_settings()
    return {
        "experiment_name": EXPERIMENT_NAME,
        "started_at": utc_text(started_at),
        "code": {
            "git_commit": git_value("rev-parse", "HEAD"),
            "git_branch": git_value("branch", "--show-current"),
            "working_tree_dirty": bool(git_value("status", "--short")),
            "working_tree_diff_sha256": git_dirty_fingerprint(),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
        },
        "inputs": {
            "root": input_root.as_posix(),
            "selection": "recursive *.pdf sorted by relative path",
            "execution_order": (
                "sequential contracts; production concurrency within contract"
            ),
            "sample_count": len(pdf_paths),
            "samples": [
                {
                    "relative_path": path.relative_to(input_root).as_posix(),
                    "size_bytes": path.stat().st_size,
                }
                for path in pdf_paths
            ],
        },
        "definitions": {
            "contract_category_catalog_sha256": getattr(
                category_catalog, "content_sha256"
            ),
            "contract_category_count": len(
                getattr(category_catalog, "categories")
            ),
            "field_catalog_sha256": getattr(field_catalog, "content_sha256"),
            "core_definition_count": getattr(field_catalog, "definition_count"),
            "retrieval_guide_catalog_sha256": getattr(
                retrieval_catalog, "content_sha256"
            ),
        },
        "models": {
            "mllm": {
                "provider": settings.mllm.provider,
                "base_url": settings.mllm.base_url,
                "model": settings.mllm.model,
                "endpoint": settings.mllm.endpoint,
                "timeout_seconds": settings.mllm.timeout_seconds,
                "max_concurrent_requests": settings.mllm.max_concurrent_requests,
                "context_window_tokens": settings.mllm.context_window_tokens,
                "generation": settings.mllm.generation.model_dump(mode="json"),
                "vision": settings.mllm.vision.model_dump(mode="json"),
            },
            "embedding": {
                "provider": settings.embedding.provider,
                "base_url": settings.embedding.base_url,
                "model": settings.embedding.model,
                "endpoint": settings.embedding.endpoint,
                "timeout_seconds": settings.embedding.timeout_seconds,
                "batch_size": settings.embedding.batch_size,
                "max_concurrent_requests": (
                    settings.embedding.max_concurrent_requests
                ),
                "dimensions": settings.embedding.dimensions,
                "normalize": settings.embedding.normalize,
            },
        },
        "variables": {
            "group": "current implementation",
            "cache_salt": None,
            "contract_concurrency": 1,
            "server_per_request_metrics_expected": True,
        },
    }


async def async_main(args: argparse.Namespace) -> int:
    """加载固定目录并顺序执行全部合同。"""
    if args.max_contracts is not None and args.max_contracts <= 0:
        raise ValueError("--max-contracts 必须大于 0")
    input_root = args.input_dir.resolve()
    if not input_root.is_dir():
        raise FileNotFoundError(f"输入目录不存在：{input_root}")
    pdf_paths = sorted(
        (path for path in input_root.rglob("*.pdf") if path.is_file()),
        key=lambda path: path.relative_to(input_root).as_posix(),
    )
    if args.max_contracts is not None:
        pdf_paths = pdf_paths[: args.max_contracts]
    if not pdf_paths:
        raise ValueError(f"输入目录中没有 PDF：{input_root}")

    settings = get_settings()
    category_catalog = load_contract_category_catalog(
        settings.contract_category_definition_path
    )
    field_catalog = load_field_definition_catalog(settings.field_definition_path)
    retrieval_catalog = load_retrieval_view_guide_catalog(
        settings.retrieval_view_guide_path,
        known_category_codes={
            category.definition.code
            for category in category_catalog.categories
        },
    )
    executor = AgentContractExtractionExecutor(
        category_catalog=category_catalog,
        field_catalog=field_catalog,
        retrieval_guide_catalog=retrieval_catalog,
    )
    pdf_preparation_service = AsyncPDFPreparationService(settings.mllm)

    started_at = utc_now()
    output_root = args.output_root.resolve() / output_timestamp(started_at)
    output_root.mkdir(parents=True, exist_ok=False)
    (output_root / "samples").mkdir()
    manifest = build_manifest(
        started_at=started_at,
        input_root=input_root,
        pdf_paths=pdf_paths,
        category_catalog=category_catalog,
        field_catalog=field_catalog,
        retrieval_catalog=retrieval_catalog,
    )
    write_json(output_root / "manifest.json", manifest)
    print(f"实验输出：{output_root}", flush=True)

    experiment_started = perf_counter()
    sample_results: list[dict[str, object]] = []
    all_metrics: list[InferenceRequestMetrics] = []
    for index, pdf_path in enumerate(pdf_paths, start=1):
        sample_result, metrics = await run_contract(
            index=index,
            pdf_path=pdf_path,
            input_root=input_root,
            output_root=output_root,
            executor=executor,
            pdf_preparation_service=pdf_preparation_service,
        )
        sample_results.append(sample_result)
        all_metrics.extend(metrics)

    statuses = Counter(
        str(result["quality"]["workflow_status"])
        for result in sample_results
    )
    result = {
        "experiment_name": EXPERIMENT_NAME,
        "started_at": utc_text(started_at),
        "finished_at": utc_text(),
        "elapsed_ms": round((perf_counter() - experiment_started) * 1000, 3),
        "sample_count": len(sample_results),
        "completed_sample_count": statuses.get("completed", 0),
        "failed_sample_count": statuses.get("failed", 0),
        "contract_completion_rate": round(
            statuses.get("completed", 0) / len(sample_results),
            6,
        ),
        "samples": sample_results,
        "inference": grouped_inference_summary(all_metrics),
    }
    write_json(output_root / "result.json", result)
    print(
        f"实验完成：{statuses.get('completed', 0)}/{len(sample_results)} "
        f"contracts, {len(all_metrics)} inference requests",
        flush=True,
    )
    return 0 if statuses.get("failed", 0) == 0 else 1


def main() -> int:
    """同步 CLI 入口。"""
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
