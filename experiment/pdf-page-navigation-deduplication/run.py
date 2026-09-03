"""验证长合同候选的 ES 召回与分页导航重复判断。"""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import pymupdf

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.contract_extraction.state import ContractExtractionRequest
from app.agent.pdf_deduplication.node import (
    PDF_PAGE_FUSION_VERSION,
    retrieve_duplicate_candidates,
    vectorize_processed_pdf,
)
from app.agent.pdf_deduplication.prompt import (
    PAGE_NAVIGATION_JUDGMENT_PROMPT_VERSION,
)
from app.agent.pdf_deduplication.state import PDFDuplicateCandidate
from app.agent.pdf_deduplication.subgraph.candidate_judgment.node import (
    decide_candidate_judgment_route,
    judge_with_page_navigation_agent,
)
from app.core.config import get_settings
from app.infrastructure.elasticsearch import create_elasticsearch_client
from app.infrastructure.inference_metrics import (
    InferenceRequestMetrics,
    bind_inference_metrics_observer,
    bind_inference_stage,
)
from app.infrastructure.pdf_candidate_loader import (
    LocalPDFDuplicateCandidateLoader,
)
from app.service.pdf_preparation import AsyncPDFPreparationService

EXPERIMENT_NAME = "pdf-page-navigation-deduplication"
EXPERIMENT_VERSION = "1.0.0"
TARGET_DOCUMENT_ID = (
    "e7591f0da4ef42e7f4ed63510089daf4cb8845fb99cfaaa3dbca26c21b308670"
)
TARGET_SOURCE = (
    PROJECT_ROOT
    / "data/input/real-data/金华泰/现象光伏科技201实验室改造项目-合同扫描件_已签章.pdf"
)
TARGET_PROCESSED = PROJECT_ROOT / "data/contract" / f"{TARGET_DOCUMENT_ID}.pdf"
SELECTED_PAGE_NUMBERS = (1, 2, 11, 20, 21)
EXPECTED_RELATION = "duplicate"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "experiment" / EXPERIMENT_NAME / "output"


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def output_timestamp(value: datetime | None = None) -> str:
    return (value or utc_now()).strftime("%Y%m%dT%H%M%S.%fZ")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def git_value(*arguments: str) -> str | None:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def git_worktree_fingerprint() -> str | None:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
    )
    return sha256_bytes(completed.stdout) if completed.returncode == 0 else None


def build_missing_pages_pdf(source: Path) -> bytes:
    """从处理版 PDF 复制固定物理页，形成可复现的严重缺页样本。"""
    with pymupdf.open(source) as original:
        invalid = [
            page_number
            for page_number in SELECTED_PAGE_NUMBERS
            if page_number > original.page_count
        ]
        if invalid:
            raise ValueError(f"缺页方案超出原件页数：{invalid}")
        transformed = pymupdf.open()
        try:
            for page_number in SELECTED_PAGE_NUMBERS:
                transformed.insert_pdf(
                    original,
                    from_page=page_number - 1,
                    to_page=page_number - 1,
                )
            return transformed.tobytes(garbage=4, deflate=True)
        finally:
            transformed.close()


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("向量维度不一致或为空")
    numerator = math.fsum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(math.fsum(value * value for value in left))
    right_norm = math.sqrt(math.fsum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("不能计算零向量余弦相似度")
    return numerator / (left_norm * right_norm)


def target_candidate(
    source: dict[str, Any],
    *,
    rank: int,
    score: float,
) -> PDFDuplicateCandidate:
    return PDFDuplicateCandidate(
        rank=rank,
        document_id=source["document_id"],
        file_name=source["file_name"],
        file_uri=source["file_uri"],
        page_count=source["page_count"],
        score=score,
    )


def summarize_metrics(metrics: list[InferenceRequestMetrics]) -> dict[str, Any]:
    values = [item.to_dict() for item in metrics]
    model_requests = [item for item in values if item.get("provider") == "mllm"]
    embedding_requests = [
        item for item in values if item.get("provider") == "embedding"
    ]

    def optional_sum(name: str) -> int | None:
        known = [item[name] for item in values if item.get(name) is not None]
        return sum(known) if known else None

    return {
        "request_count": len(values),
        "mllm_request_count": len(model_requests),
        "embedding_request_count": len(embedding_requests),
        "request_elapsed_ms": round(
            sum(float(item.get("elapsed_ms", 0)) for item in values), 3
        ),
        "prompt_tokens": optional_sum("prompt_tokens"),
        "completion_tokens": optional_sum("completion_tokens"),
        "cached_tokens": optional_sum("cached_tokens"),
    }


async def async_main() -> int:
    started_at = utc_now()
    output_dir = DEFAULT_OUTPUT_ROOT / output_timestamp(started_at)
    output_dir.mkdir(parents=True, exist_ok=False)
    print(f"实验输出：{output_dir}", flush=True)

    settings = get_settings()
    client = create_elasticsearch_client(settings)
    metrics: list[InferenceRequestMetrics] = []
    end_to_end_started = perf_counter()
    try:
        if not TARGET_SOURCE.is_file():
            raise FileNotFoundError(f"长合同源文件不存在：{TARGET_SOURCE}")
        if not TARGET_PROCESSED.is_file():
            raise FileNotFoundError(f"长合同处理版不存在：{TARGET_PROCESSED}")
        processed_sha256 = sha256_file(TARGET_PROCESSED)
        if processed_sha256 != TARGET_DOCUMENT_ID:
            raise ValueError(
                "长合同处理版 SHA-256 与固定 document_id 不一致："
                f"{processed_sha256}"
            )
        if not await client.exists(
            index=settings.elasticsearch_index_name,
            id=TARGET_DOCUMENT_ID,
        ):
            raise RuntimeError("目标长合同尚未写入当前 Elasticsearch 索引")

        response = await client.get(
            index=settings.elasticsearch_index_name,
            id=TARGET_DOCUMENT_ID,
            # Elasticsearch 9 默认在 _source 响应中省略 dense_vector；
            # 实验需要原始向量计算目标余弦相似度，因此必须显式取回。
            source_exclude_vectors=False,
        )
        target_source = response["_source"]
        if target_source.get("document_id") != TARGET_DOCUMENT_ID:
            raise ValueError("ES _source.document_id 与文档 ID 不一致")
        target_vector = target_source.get("vectors", {}).get("page_fusion")
        if not isinstance(target_vector, list):
            raise ValueError("ES 目标文档没有可用的 vectors.page_fusion")

        transformed_bytes = build_missing_pages_pdf(TARGET_PROCESSED)
        preparation_started = perf_counter()
        preparation = AsyncPDFPreparationService(settings.mllm)
        uploaded_pdf = await preparation.prepare(
            ContractExtractionRequest(
                pdf_bytes=transformed_bytes,
                file_name="long-contract-pages-1-2-11-20-21.pdf",
            )
        )
        preparation_elapsed_ms = round(
            (perf_counter() - preparation_started) * 1000, 3
        )

        with bind_inference_metrics_observer(metrics.append):
            with bind_inference_stage("page_navigation:embedding"):
                vectorized_state = await vectorize_processed_pdf(
                    {"prepared_pdf": uploaded_pdf}
                )
            with bind_inference_stage("page_navigation:retrieval"):
                retrieved_state = await retrieve_duplicate_candidates(
                    vectorized_state,
                    client=client,
                    index_name=settings.elasticsearch_index_name,
                )

        fusion = retrieved_state["page_fusion_vector"]
        candidate_set = retrieved_state["duplicate_candidates"]
        recalled_target = next(
            (
                candidate
                for candidate in candidate_set.candidates
                if candidate.document_id == TARGET_DOCUMENT_ID
            ),
            None,
        )
        direct_cosine = cosine_similarity(list(fusion.vector), target_vector)
        write_json(
            output_dir / "retrieval.json",
            {
                "minimum_cosine_similarity": (
                    settings.pdf_deduplication.minimum_recall_cosine_similarity
                ),
                "target_document_id": TARGET_DOCUMENT_ID,
                "target_cosine_similarity": direct_cosine,
                "target_recalled": recalled_target is not None,
                "candidates": [
                    candidate.model_dump(mode="json")
                    for candidate in candidate_set.candidates
                ],
                "elapsed_ms": candidate_set.elapsed_ms,
            },
        )

        # 节点实验始终按稳定 ID 从 ES 定向选择目标；若真实召回成功则保留真实排名和分数。
        candidate = recalled_target or target_candidate(
            target_source,
            rank=1,
            score=direct_cosine,
        )
        loader = LocalPDFDuplicateCandidateLoader(settings.mllm)
        candidate_pdf = await loader.load(candidate)
        write_json(
            output_dir / "input.json",
            {
                "variant": "missing-pages-1-2-11-20-21",
                "selected_source_page_numbers": list(SELECTED_PAGE_NUMBERS),
                "transformed_pdf_sha256": sha256_bytes(transformed_bytes),
                "uploaded": {
                    "document_id": uploaded_pdf.document_id,
                    "page_count": uploaded_pdf.page_count,
                    "total_visual_tokens": uploaded_pdf.total_visual_tokens,
                    "processed_file_size_bytes": (
                        uploaded_pdf.processed_file_size_bytes
                    ),
                },
                "candidate": {
                    "document_id": candidate_pdf.document_id,
                    "page_count": candidate_pdf.page_count,
                    "total_visual_tokens": candidate_pdf.total_visual_tokens,
                    "processed_file_size_bytes": (
                        candidate_pdf.processed_file_size_bytes
                    ),
                },
            },
        )
        route_state = decide_candidate_judgment_route(
            {
                "uploaded_pdf": uploaded_pdf,
                "candidate_pdf": candidate_pdf,
                "candidate": candidate,
            }
        )
        routing = route_state["routing_decision"]
        write_json(output_dir / "routing.json", routing.model_dump(mode="json"))
        route_eligible = routing.strategy == "page_navigation_agent"
        if not route_eligible:
            raise RuntimeError(
                f"正式路由未进入 page_navigation_agent：{routing.strategy}"
            )

        judgment_started = perf_counter()
        with bind_inference_metrics_observer(metrics.append):
            with bind_inference_stage("page_navigation:judgment"):
                judged_state = await judge_with_page_navigation_agent(route_state)
        judgment_elapsed_ms = round((perf_counter() - judgment_started) * 1000, 3)
        judgment = judged_state["judgment"]
        write_json(
            output_dir / "judgment.json",
            judgment.model_dump(mode="json"),
        )

        successful_inspections = [
            audit
            for audit in judgment.tool_calls
            if audit.name == "inspect_candidate_pages" and audit.feedback.ok
        ]
        viewed_pages: set[int] = set()
        for audit in successful_inspections:
            arguments = json.loads(audit.raw_arguments)
            viewed_pages.update(arguments.get("page_numbers", []))

        node_passed = (
            route_eligible
            and judgment.status == EXPECTED_RELATION
            and len(successful_inspections) <= 6
            and len(viewed_pages) <= 12
            and judgment.rounds <= 24
        )
        target_recalled = recalled_target is not None
        if node_passed and target_recalled:
            status = "passed"
        elif node_passed:
            status = "partial"
        else:
            status = "failed"
        result = {
            "status": status,
            "started_at": utc_text(started_at),
            "finished_at": utc_text(),
            "expected_relation": EXPECTED_RELATION,
            "actual_relation": judgment.status,
            "page_navigation_node_passed": node_passed,
            "end_to_end_deduplication_passed": node_passed and target_recalled,
            "target_recalled": target_recalled,
            "target_cosine_similarity": direct_cosine,
            "minimum_cosine_similarity": (
                settings.pdf_deduplication.minimum_recall_cosine_similarity
            ),
            "route_eligible": route_eligible,
            "route_strategy": routing.strategy,
            "route_reason": routing.reason,
            "rounds": judgment.rounds,
            "inspection_count": len(successful_inspections),
            "unique_candidate_pages_viewed": sorted(viewed_pages),
            "unique_candidate_page_count": len(viewed_pages),
            "preparation_elapsed_ms": preparation_elapsed_ms,
            "embedding_elapsed_ms": fusion.elapsed_ms,
            "retrieval_elapsed_ms": candidate_set.elapsed_ms,
            "judgment_elapsed_ms": judgment_elapsed_ms,
            "end_to_end_elapsed_ms": round(
                (perf_counter() - end_to_end_started) * 1000, 3
            ),
            "inference": summarize_metrics(metrics),
        }
        write_json(output_dir / "result.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if status in {"passed", "partial"} else 2
    except Exception as exc:
        result_path = output_dir / "result.json"
        if not result_path.exists():
            write_json(
                result_path,
                {
                    "status": "inconclusive",
                    "started_at": utc_text(started_at),
                    "finished_at": utc_text(),
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc)[:2000],
                    },
                    "end_to_end_elapsed_ms": round(
                        (perf_counter() - end_to_end_started) * 1000, 3
                    ),
                },
            )
        raise
    finally:
        write_json(
            output_dir / "inference-metrics.json",
            [item.to_dict() for item in metrics],
        )
        mllm = settings.mllm
        write_json(
            output_dir / "manifest.json",
            {
                "experiment": EXPERIMENT_NAME,
                "experiment_version": EXPERIMENT_VERSION,
                "started_at": utc_text(started_at),
                "target": {
                    "document_id": TARGET_DOCUMENT_ID,
                    "source_path": TARGET_SOURCE.relative_to(PROJECT_ROOT).as_posix(),
                    "source_sha256": (
                        sha256_file(TARGET_SOURCE) if TARGET_SOURCE.is_file() else None
                    ),
                    "processed_path": TARGET_PROCESSED.relative_to(PROJECT_ROOT).as_posix(),
                    "processed_sha256": (
                        sha256_file(TARGET_PROCESSED)
                        if TARGET_PROCESSED.is_file()
                        else None
                    ),
                },
                "variant": {
                    "name": "missing-pages-1-2-11-20-21",
                    "selected_source_page_numbers": list(SELECTED_PAGE_NUMBERS),
                    "transformed_pdf_sha256": (
                        sha256_bytes(transformed_bytes)
                        if "transformed_bytes" in locals()
                        else None
                    ),
                    "expected_relation": EXPECTED_RELATION,
                },
                "index": {
                    "name": settings.elasticsearch_index_name,
                    "minimum_recall_cosine_similarity": (
                        settings.pdf_deduplication.minimum_recall_cosine_similarity
                    ),
                    "top_k": 3,
                },
                "embedding": {
                    "provider": settings.embedding.provider,
                    "base_url": settings.embedding.base_url,
                    "model": settings.embedding.model,
                    "dimensions": settings.embedding.dimensions,
                    "input_version": fusion.embedding_input_version
                    if "fusion" in locals()
                    else None,
                    "fusion_version": PDF_PAGE_FUSION_VERSION,
                },
                "mllm": {
                    "provider": mllm.provider,
                    "base_url": mllm.base_url,
                    "model": mllm.model,
                    "endpoint": mllm.endpoint,
                    "timeout_seconds": mllm.timeout_seconds,
                    "use_media_references": mllm.use_media_references,
                    "context_window_tokens": mllm.context_window_tokens,
                    "generation": mllm.generation.model_dump(mode="json"),
                    "vision": mllm.vision.model_dump(mode="json"),
                },
                "prompt_version": PAGE_NAVIGATION_JUDGMENT_PROMPT_VERSION,
                "candidate_selection": {
                    "retrieval_is_measured_separately": True,
                    "judgment_targeted_by_stable_es_document_id": True,
                    "uses_retrieved_rank_and_score_when_available": True,
                },
                "environment": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "git_commit": git_value("rev-parse", "HEAD"),
                    "git_worktree_fingerprint": git_worktree_fingerprint(),
                },
            },
        )
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
