"""运行 Qwen3-VL-Embedding 合同 PDF 页面向量召回实验。"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import platform
import subprocess
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from openai import AsyncOpenAI
from openai.types.create_embedding_response import CreateEmbeddingResponse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.contract_extraction.state import ContractExtractionRequest, PreparedPDF
from app.agent.pdf_deduplication.prompt import (
    PDF_PAGE_EMBEDDING_INPUT_VERSION,
    PDF_PAGE_EMBEDDING_SYSTEM_INSTRUCTION,
    build_pdf_page_embedding_messages,
)
from app.core.config import EmbeddingSettings, get_settings
from app.service.pdf_preparation import AsyncPDFPreparationService
from app.tool.pdf_page import PDFPageRenderConfig, compress_pdf_pages

EXPERIMENT_NAME = "pdf-page-embedding-recall"
EXPERIMENT_VERSION = "1.1.0"
DEFAULT_INPUT_DIR = PROJECT_ROOT / "data/input/test-data"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "experiment" / EXPERIMENT_NAME / "output"
FUSION_VERSION = "arithmetic-mean-l2-v1"
INSTRUCTIONS = {
    "official-default-v1": "Represent the user's input.",
    PDF_PAGE_EMBEDDING_INPUT_VERSION: PDF_PAGE_EMBEDDING_SYSTEM_INSTRUCTION,
}


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
        "--query-visual-tokens-per-page",
        type=int,
        default=2048,
        help="query 再次栅格化时的单页视觉 token 预算。",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="远程多模态 Embedding 最大并发请求数。",
    )
    parser.add_argument(
        "--max-contracts",
        type=int,
        default=None,
        help="只执行排序后的前 N 份合同；用于冒烟验证。",
    )
    return parser.parse_args()


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(UTC)


def utc_text(value: datetime | None = None) -> str:
    """生成 JSON 使用的 UTC ISO 时间。"""
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def output_timestamp(value: datetime | None = None) -> str:
    """生成不会覆盖历史运行的 UTC 目录名。"""
    return (value or utc_now()).strftime("%Y%m%dT%H%M%S.%fZ")


def write_json(path: Path, value: object) -> None:
    """以稳定的 UTF-8 JSON 写入机器产物。"""
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sha256_bytes(value: bytes) -> str:
    """计算内存数据的稳定身份。"""
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256，避免实验输入整体复制到内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deduplicate_exact_inputs(
    paths: Sequence[Path],
) -> tuple[list[Path], list[dict[str, str]]]:
    """按源文件哈希去重，避免同一 PDF 以多个候选身份污染真值。"""
    canonical_by_sha256: dict[str, Path] = {}
    unique_paths: list[Path] = []
    excluded: list[dict[str, str]] = []
    for candidate in paths:
        path = candidate.resolve()
        source_sha256 = sha256_file(path)
        canonical = canonical_by_sha256.get(source_sha256)
        if canonical is None:
            canonical_by_sha256[source_sha256] = path
            unique_paths.append(path)
            continue
        excluded.append(
            {
                "source_sha256": source_sha256,
                "excluded_relative_path": path.relative_to(PROJECT_ROOT).as_posix(),
                "canonical_relative_path": canonical.relative_to(
                    PROJECT_ROOT
                ).as_posix(),
            }
        )
    return unique_paths, excluded


def git_value(*arguments: str) -> str | None:
    """尽力读取 Git 元数据，不让缺失 Git 阻断实验。"""
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
    """只记录工作区状态指纹，不复制合同或源码内容。"""
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        return None
    return sha256_bytes(completed.stdout)


def mean(values: Sequence[float]) -> float | None:
    """返回非空序列均值，空序列显式返回 None。"""
    if not values:
        return None
    return math.fsum(values) / len(values)


def l2_normalize(vector: Sequence[float]) -> tuple[float, ...]:
    """L2 归一化并拒绝非有限值或零向量。"""
    if not vector or not all(math.isfinite(value) for value in vector):
        raise ValueError("向量不能为空且只能包含有限数值")
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if norm == 0:
        raise ValueError("向量不能是零向量")
    return tuple(value / norm for value in vector)


def fuse_page_vectors(vectors: Sequence[Sequence[float]]) -> tuple[float, ...]:
    """先归一化每页，再等权平均并重新归一化合同向量。"""
    if not vectors:
        raise ValueError("PDF 至少需要一个成功页向量")
    dimensions = len(vectors[0])
    if any(len(vector) != dimensions for vector in vectors):
        raise ValueError("同一 PDF 的页向量维度必须一致")
    normalized = tuple(l2_normalize(vector) for vector in vectors)
    averaged = tuple(
        math.fsum(vector[index] for vector in normalized) / len(normalized)
        for index in range(dimensions)
    )
    return l2_normalize(averaged)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """归一化向量的点积即余弦相似度。"""
    if len(left) != len(right):
        raise ValueError("余弦相似度要求向量维度相同")
    return math.fsum(a * b for a, b in zip(left, right, strict=True))


def error_json(error: BaseException) -> dict[str, str]:
    """保存有限错误信息，不复制远端可能返回的超长正文。"""
    return {
        "type": type(error).__name__,
        "message": str(error)[:2000],
    }


def safe_base_url(settings: EmbeddingSettings) -> str:
    """记录不含查询参数和凭据的服务地址。"""
    return settings.base_url.split("?", 1)[0]


def build_messages(instruction: str, png_bytes: bytes) -> list[dict[str, Any]]:
    """按 vLLM Qwen3-VL-Embedding 官方在线示例构造单图输入。"""
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode(
        "ascii"
    )
    if instruction == PDF_PAGE_EMBEDDING_SYSTEM_INSTRUCTION:
        return build_pdf_page_embedding_messages(data_url)
    return [
        {
            "role": "system",
            "content": [{"type": "text", "text": instruction}],
        },
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": ""},
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": ""}],
        },
    ]


async def create_page_embedding(
    *,
    client: AsyncOpenAI,
    semaphore: asyncio.Semaphore,
    settings: EmbeddingSettings,
    instruction_version: str,
    instruction: str,
    document_id: str,
    source_sha256: str,
    source_name: str,
    variant: str,
    page_number: int,
    png_bytes: bytes,
    width_pixels: int,
    height_pixels: int,
    visual_tokens: int,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """生成一页向量，并分别返回请求摘要与可复用原始向量。"""
    request_started = perf_counter()
    image_sha256 = sha256_bytes(png_bytes)
    common: dict[str, object] = {
        "instruction_version": instruction_version,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "source_name": source_name,
        "variant": variant,
        "page_number": page_number,
        "image_sha256": image_sha256,
        "width_pixels": width_pixels,
        "height_pixels": height_pixels,
        "visual_tokens": visual_tokens,
    }
    try:
        async with semaphore:
            response = await client.post(
                "/embeddings",
                cast_to=CreateEmbeddingResponse,
                body={
                    "messages": build_messages(instruction, png_bytes),
                    "model": settings.model,
                    "encoding_format": "float",
                    "continue_final_message": True,
                    "add_special_tokens": True,
                },
            )
        if len(response.data) != 1:
            raise ValueError(
                f"单页请求必须返回一个向量，实际为 {len(response.data)}"
            )
        vector = tuple(float(value) for value in response.data[0].embedding)
        if len(vector) != settings.dimensions:
            raise ValueError(
                f"向量维度不符：expected={settings.dimensions}, actual={len(vector)}"
            )
        l2_normalize(vector)
        elapsed_ms = (perf_counter() - request_started) * 1000
        usage = response.usage
        request_record = {
            **common,
            "status": "succeeded",
            "model": response.model,
            "dimensions": len(vector),
            "elapsed_ms": round(elapsed_ms, 3),
            "prompt_tokens": usage.prompt_tokens if usage is not None else None,
            "total_tokens": usage.total_tokens if usage is not None else None,
        }
        embedding_record = {
            **common,
            "model": response.model,
            "dimensions": len(vector),
            "vector": vector,
        }
        return request_record, embedding_record
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        return (
            {
                **common,
                "status": "failed",
                "elapsed_ms": round(
                    (perf_counter() - request_started) * 1000,
                    3,
                ),
                "error": error_json(error),
            },
            None,
        )


async def prepare_documents(
    paths: Sequence[Path],
    *,
    query_visual_tokens_per_page: int,
) -> list[dict[str, object]]:
    """生成生产 gallery 和二次低预算栅格化 query 页面。"""
    settings = get_settings()
    preparation = AsyncPDFPreparationService(settings.mllm)
    documents: list[dict[str, object]] = []
    query_config = PDFPageRenderConfig(
        max_render_scale=settings.mllm.vision.max_render_scale,
        visual_token_patch_size=settings.mllm.vision.visual_token_patch_size,
        max_visual_tokens_per_page=query_visual_tokens_per_page,
    )
    for index, path in enumerate(paths, start=1):
        source_sha256 = sha256_file(path)
        prepared = await preparation.prepare(
            ContractExtractionRequest(pdf_path=path)
        )
        query_pages = await asyncio.to_thread(
            compress_pdf_pages,
            prepared.processed_pdf_bytes,
            config=query_config,
        )
        documents.append(
            {
                "index": index,
                "path": path,
                "source_name": path.name,
                "source_sha256": source_sha256,
                "prepared": prepared,
                "query_pages": query_pages,
            }
        )
    return documents


def build_manifest(
    *,
    started_at: datetime,
    settings: EmbeddingSettings,
    documents: Sequence[dict[str, object]],
    query_visual_tokens_per_page: int,
    concurrency: int,
    input_file_count: int,
    excluded_exact_duplicates: Sequence[dict[str, str]],
) -> dict[str, object]:
    """形成不含密钥和合同内容的静态复现清单。"""
    sample_records = []
    for item in documents:
        prepared = item["prepared"]
        if not isinstance(prepared, PreparedPDF):
            raise TypeError("prepared 必须是 PreparedPDF")
        path = item["path"]
        if not isinstance(path, Path):
            raise TypeError("path 必须是 Path")
        sample_records.append(
            {
                "relative_path": path.relative_to(PROJECT_ROOT).as_posix(),
                "source_name": item["source_name"],
                "source_sha256": item["source_sha256"],
                "document_id": prepared.document_id,
                "page_count": prepared.page_count,
                "source_size_bytes": prepared.source_file_size_bytes,
                "processed_size_bytes": prepared.processed_file_size_bytes,
                "gallery_pages": [
                    {
                        "page_number": page.page_number,
                        "image_sha256": page.content_sha256,
                        "width_pixels": page.width_pixels,
                        "height_pixels": page.height_pixels,
                        "render_scale": page.render_scale,
                        "visual_tokens": page.visual_tokens,
                    }
                    for page in prepared.pages
                ],
                "query_pages": [
                    {
                        "page_number": page.page_number,
                        "image_sha256": sha256_bytes(page.png_bytes),
                        "width_pixels": page.width_pixels,
                        "height_pixels": page.height_pixels,
                        "render_scale": page.render_scale,
                        "visual_tokens": page.visual_tokens,
                    }
                    for page in item["query_pages"]
                ],
            }
        )
    return {
        "experiment_name": EXPERIMENT_NAME,
        "experiment_version": EXPERIMENT_VERSION,
        "started_at": utc_text(started_at),
        "git_commit": git_value("rev-parse", "HEAD"),
        "git_worktree_fingerprint": git_worktree_fingerprint(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "model": {
            "provider": settings.provider,
            "base_url": safe_base_url(settings),
            "model": settings.model,
            "endpoint": settings.endpoint,
            "timeout_seconds": settings.timeout_seconds,
            "dimensions": settings.dimensions,
        },
        "instructions": {
            version: {
                "text": text,
                "sha256": sha256_bytes(text.encode("utf-8")),
            }
            for version, text in INSTRUCTIONS.items()
        },
        "fusion": {
            "version": FUSION_VERSION,
            "method": "page_l2_then_arithmetic_mean_then_l2",
        },
        "rendering": {
            "gallery": "production AsyncPDFPreparationService settings",
            "query_source": "gallery processed PDF",
            "query_visual_tokens_per_page": query_visual_tokens_per_page,
        },
        "retrieval": {
            "method": "in_memory_exact_cosine",
            "top_k": [1, 3],
        },
        "concurrency": concurrency,
        "input_file_count": input_file_count,
        "unique_sample_count": len(sample_records),
        "excluded_exact_duplicates": excluded_exact_duplicates,
        "samples": sample_records,
    }


def embedding_key(record: dict[str, object]) -> tuple[str, str, str, int]:
    """返回页向量记录的稳定复合键。"""
    return (
        str(record["instruction_version"]),
        str(record["variant"]),
        str(record["document_id"]),
        int(record["page_number"]),
    )


def build_instruction_result(
    *,
    instruction_version: str,
    documents: Sequence[dict[str, object]],
    embeddings: dict[tuple[str, str, str, int], Sequence[float]],
) -> dict[str, object]:
    """融合全部可用文档并对每个 query 执行全库精确排序。"""
    gallery_vectors: dict[str, tuple[float, ...]] = {}
    query_vectors: dict[str, tuple[float, ...]] = {}
    incomplete: list[dict[str, object]] = []

    for item in documents:
        prepared = item["prepared"]
        if not isinstance(prepared, PreparedPDF):
            raise TypeError("prepared 必须是 PreparedPDF")
        for variant, target in (
            ("gallery", gallery_vectors),
            ("query", query_vectors),
        ):
            page_vectors: list[Sequence[float]] = []
            missing_pages: list[int] = []
            for page_number in range(1, prepared.page_count + 1):
                vector = embeddings.get(
                    (
                        instruction_version,
                        variant,
                        prepared.document_id,
                        page_number,
                    )
                )
                if vector is None:
                    missing_pages.append(page_number)
                else:
                    page_vectors.append(vector)
            if missing_pages:
                incomplete.append(
                    {
                        "document_id": prepared.document_id,
                        "source_name": item["source_name"],
                        "variant": variant,
                        "missing_page_numbers": missing_pages,
                    }
                )
                continue
            target[prepared.document_id] = fuse_page_vectors(page_vectors)

    rankings: list[dict[str, object]] = []
    reciprocal_ranks: list[float] = []
    positive_similarities: list[float] = []
    strongest_negative_similarities: list[float] = []
    margins: list[float] = []
    recall_at_1_hits = 0
    recall_at_3_hits = 0

    names = {
        item["prepared"].document_id: item["source_name"]
        for item in documents
        if isinstance(item["prepared"], PreparedPDF)
    }
    for item in documents:
        prepared = item["prepared"]
        if not isinstance(prepared, PreparedPDF):
            raise TypeError("prepared 必须是 PreparedPDF")
        query = query_vectors.get(prepared.document_id)
        if query is None or not gallery_vectors:
            rankings.append(
                {
                    "document_id": prepared.document_id,
                    "source_name": item["source_name"],
                    "status": "failed",
                    "reason": "query 或 gallery 融合向量不完整",
                }
            )
            reciprocal_ranks.append(0.0)
            continue

        candidates = sorted(
            (
                {
                    "rank": 0,
                    "document_id": candidate_id,
                    "source_name": names[candidate_id],
                    "similarity": round(cosine(query, candidate), 8),
                    "is_positive": candidate_id == prepared.document_id,
                }
                for candidate_id, candidate in gallery_vectors.items()
            ),
            key=lambda candidate: (
                -float(candidate["similarity"]),
                str(candidate["document_id"]),
            ),
        )
        for rank, candidate in enumerate(candidates, start=1):
            candidate["rank"] = rank
        positive = next(
            (candidate for candidate in candidates if candidate["is_positive"]),
            None,
        )
        if positive is None:
            rankings.append(
                {
                    "document_id": prepared.document_id,
                    "source_name": item["source_name"],
                    "status": "failed",
                    "reason": "gallery 中缺少同源正样本",
                    "candidates": candidates,
                }
            )
            reciprocal_ranks.append(0.0)
            continue

        positive_rank = int(positive["rank"])
        positive_similarity = float(positive["similarity"])
        negatives = [
            float(candidate["similarity"])
            for candidate in candidates
            if not candidate["is_positive"]
        ]
        strongest_negative = max(negatives) if negatives else None
        margin = (
            positive_similarity - strongest_negative
            if strongest_negative is not None
            else None
        )
        recall_at_1_hits += positive_rank <= 1
        recall_at_3_hits += positive_rank <= 3
        reciprocal_ranks.append(1.0 / positive_rank)
        positive_similarities.append(positive_similarity)
        if strongest_negative is not None:
            strongest_negative_similarities.append(strongest_negative)
        if margin is not None:
            margins.append(margin)
        rankings.append(
            {
                "document_id": prepared.document_id,
                "source_name": item["source_name"],
                "status": "succeeded",
                "positive_rank": positive_rank,
                "positive_similarity": positive_similarity,
                "strongest_negative_similarity": strongest_negative,
                "positive_negative_margin": (
                    round(margin, 8) if margin is not None else None
                ),
                "top_3": candidates[:3],
                "all_candidates": candidates,
            }
        )

    total_queries = len(documents)
    metrics = {
        "query_count": total_queries,
        "complete_gallery_count": len(gallery_vectors),
        "complete_query_count": len(query_vectors),
        "recall_at_1": recall_at_1_hits / total_queries if total_queries else None,
        "recall_at_3": recall_at_3_hits / total_queries if total_queries else None,
        "mrr": mean(reciprocal_ranks),
        "mean_positive_similarity": mean(positive_similarities),
        "mean_strongest_negative_similarity": mean(
            strongest_negative_similarities
        ),
        "mean_positive_negative_margin": mean(margins),
        "min_positive_negative_margin": min(margins) if margins else None,
    }
    passed = (
        metrics["recall_at_1"] == 1.0
        and metrics["recall_at_3"] == 1.0
        and metrics["min_positive_negative_margin"] is not None
        and float(metrics["min_positive_negative_margin"]) > 0
        and not incomplete
    )
    return {
        "instruction_version": instruction_version,
        "instruction": INSTRUCTIONS[instruction_version],
        "fusion_version": FUSION_VERSION,
        "metrics": metrics,
        "passed": passed,
        "incomplete_vectors": incomplete,
        "rankings": rankings,
    }


def aggregate_requests(records: Sequence[dict[str, object]]) -> dict[str, object]:
    """汇总页面请求成功率、耗时和 token。"""
    succeeded = [record for record in records if record["status"] == "succeeded"]
    elapsed = [float(record["elapsed_ms"]) for record in records]
    return {
        "request_count": len(records),
        "successful_request_count": len(succeeded),
        "failed_request_count": len(records) - len(succeeded),
        "success_rate": len(succeeded) / len(records) if records else None,
        "client_elapsed_ms_sum": math.fsum(elapsed),
        "client_elapsed_ms_mean": mean(elapsed),
        "prompt_tokens_total": sum(
            int(record.get("prompt_tokens") or 0) for record in succeeded
        ),
        "total_tokens_total": sum(
            int(record.get("total_tokens") or 0) for record in succeeded
        ),
    }


def build_comparison(results: dict[str, dict[str, object]]) -> dict[str, object]:
    """比较推荐指令和官方默认指令的核心召回指标。"""
    baseline = results["official-default-v1"]["metrics"]
    recommended = results[PDF_PAGE_EMBEDDING_INPUT_VERSION]["metrics"]
    if not isinstance(baseline, dict) or not isinstance(recommended, dict):
        raise TypeError("指令指标必须是字典")

    def delta(name: str) -> float | None:
        left = recommended.get(name)
        right = baseline.get(name)
        if left is None or right is None:
            return None
        return float(left) - float(right)

    return {
        "recommended_minus_default": {
            "recall_at_1": delta("recall_at_1"),
            "recall_at_3": delta("recall_at_3"),
            "mrr": delta("mrr"),
            "mean_positive_negative_margin": delta(
                "mean_positive_negative_margin"
            ),
            "min_positive_negative_margin": delta(
                "min_positive_negative_margin"
            ),
        },
        "recommended_has_larger_mean_margin": (
            delta("mean_positive_negative_margin") is not None
            and float(delta("mean_positive_negative_margin")) > 0
        ),
    }


async def run(args: argparse.Namespace) -> Path:
    """执行完整实验并返回新建输出目录。"""
    if args.query_visual_tokens_per_page <= 0:
        raise ValueError("query 页面视觉 token 预算必须大于 0")
    if args.concurrency <= 0:
        raise ValueError("并发数必须大于 0")
    input_dir = args.input_dir.resolve()
    output_root = args.output_root.resolve()
    discovered_paths = sorted(
        input_dir.rglob("*.pdf"),
        key=lambda path: path.as_posix(),
    )
    if args.max_contracts is not None:
        if args.max_contracts <= 0:
            raise ValueError("max-contracts 必须大于 0")
        discovered_paths = discovered_paths[: args.max_contracts]
    if not discovered_paths:
        raise FileNotFoundError(f"输入目录没有 PDF：{input_dir}")
    paths, excluded_exact_duplicates = deduplicate_exact_inputs(discovered_paths)

    started_at = utc_now()
    end_to_end_started = perf_counter()
    output_dir = output_root / output_timestamp(started_at)
    output_dir.mkdir(parents=True, exist_ok=False)
    settings = get_settings()
    documents = await prepare_documents(
        paths,
        query_visual_tokens_per_page=args.query_visual_tokens_per_page,
    )
    write_json(
        output_dir / "manifest.json",
        build_manifest(
            started_at=started_at,
            settings=settings.embedding,
            documents=documents,
            query_visual_tokens_per_page=args.query_visual_tokens_per_page,
            concurrency=args.concurrency,
            input_file_count=len(discovered_paths),
            excluded_exact_duplicates=excluded_exact_duplicates,
        ),
    )

    semaphore = asyncio.Semaphore(
        min(args.concurrency, settings.embedding.max_concurrent_requests)
    )
    tasks = []
    async with AsyncOpenAI(
        api_key=settings.embedding.api_key or "vllm-local",
        base_url=settings.embedding.base_url.rstrip("/") + "/",
        timeout=settings.embedding.timeout_seconds,
        max_retries=0,
    ) as client:
        for instruction_version, instruction in INSTRUCTIONS.items():
            for item in documents:
                prepared = item["prepared"]
                if not isinstance(prepared, PreparedPDF):
                    raise TypeError("prepared 必须是 PreparedPDF")
                page_groups = (
                    ("gallery", prepared.pages),
                    ("query", item["query_pages"]),
                )
                for variant, pages in page_groups:
                    for page in pages:
                        tasks.append(
                            create_page_embedding(
                                client=client,
                                semaphore=semaphore,
                                settings=settings.embedding,
                                instruction_version=instruction_version,
                                instruction=instruction,
                                document_id=prepared.document_id,
                                source_sha256=str(item["source_sha256"]),
                                source_name=str(item["source_name"]),
                                variant=variant,
                                page_number=page.page_number,
                                png_bytes=page.png_bytes,
                                width_pixels=page.width_pixels,
                                height_pixels=page.height_pixels,
                                visual_tokens=page.visual_tokens,
                            )
                        )
        print(
            f"开始 {len(tasks)} 个页面请求：contracts={len(documents)}, "
            f"instructions={len(INSTRUCTIONS)}, concurrency={semaphore._value}",
            flush=True,
        )
        outcomes = await asyncio.gather(*tasks)

    request_records = [outcome[0] for outcome in outcomes]
    embedding_records = [
        outcome[1] for outcome in outcomes if outcome[1] is not None
    ]
    write_json(output_dir / "requests.json", {"requests": request_records})
    write_json(
        output_dir / "page-embeddings.json",
        {"embeddings": embedding_records},
    )

    embedding_lookup = {
        embedding_key(record): record["vector"]
        for record in embedding_records
    }
    instruction_results = {
        version: build_instruction_result(
            instruction_version=version,
            documents=documents,
            embeddings=embedding_lookup,
        )
        for version in INSTRUCTIONS
    }
    completed_at = utc_now()
    result = {
        "experiment_name": EXPERIMENT_NAME,
        "experiment_version": EXPERIMENT_VERSION,
        "started_at": utc_text(started_at),
        "completed_at": utc_text(completed_at),
        "end_to_end_elapsed_ms": round(
            (perf_counter() - end_to_end_started) * 1000,
            3,
        ),
        "request_summary": aggregate_requests(request_records),
        "instruction_results": instruction_results,
        "comparison": build_comparison(instruction_results),
    }
    write_json(output_dir / "result.json", result)
    print(output_dir, flush=True)
    return output_dir


def main() -> None:
    """命令行入口。"""
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
