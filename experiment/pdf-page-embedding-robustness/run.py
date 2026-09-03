"""运行合同 PDF 页面向量鲁棒性实验。"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import math
import platform
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from openai import AsyncOpenAI
from openai.types.create_embedding_response import CreateEmbeddingResponse
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.contract_extraction.state import ContractExtractionRequest, PreparedPDF
from app.agent.pdf_deduplication.prompt import (
    PDF_PAGE_EMBEDDING_INPUT_VERSION,
    build_pdf_page_embedding_messages,
)
from app.core.config import EmbeddingSettings, get_settings
from app.service.pdf_preparation import AsyncPDFPreparationService

EXPERIMENT_NAME = "pdf-page-embedding-robustness"
EXPERIMENT_VERSION = "1.0.0"
DEFAULT_INPUT_DIR = PROJECT_ROOT / "data/input/test-data"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "experiment" / EXPERIMENT_NAME / "output"


@dataclass(frozen=True, slots=True)
class VariantSpec:
    """一个确定性页面扰动及其适用范围。"""

    name: str
    family: str
    description: str
    transform: Callable[[bytes], bytes] | None = None
    missing_position: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentSample:
    """一份经过生产准备的唯一合同样本。"""

    path: Path
    source_sha256: str
    prepared: PreparedPDF


def parse_args() -> argparse.Namespace:
    """解析可复现实验参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
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
        help="只执行排序后的前 N 份唯一合同。",
    )
    return parser.parse_args()


def utc_now() -> datetime:
    """返回当前 UTC 时间。"""
    return datetime.now(UTC)


def utc_text(value: datetime | None = None) -> str:
    """生成 JSON 使用的 UTC ISO 时间。"""
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def output_timestamp(value: datetime | None = None) -> str:
    """生成不会覆盖历史运行的 UTC 目录名。"""
    return (value or utc_now()).strftime("%Y%m%dT%H%M%S.%fZ")


def write_json(path: Path, value: object) -> None:
    """以稳定 UTF-8 JSON 写入实验产物。"""
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sha256_bytes(value: bytes) -> str:
    """计算内存字节的 SHA-256。"""
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_value(*arguments: str) -> str | None:
    """尽力读取 Git 元数据。"""
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
    """记录工作区状态指纹，不复制源码和合同内容。"""
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
    )
    return sha256_bytes(completed.stdout) if completed.returncode == 0 else None


def l2_normalize(vector: Sequence[float]) -> tuple[float, ...]:
    """归一化向量并拒绝非有限或零向量。"""
    if not vector or not all(math.isfinite(value) for value in vector):
        raise ValueError("向量不能为空且只能包含有限数值")
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if norm == 0:
        raise ValueError("向量不能是零向量")
    return tuple(value / norm for value in vector)


def fuse_page_vectors(vectors: Sequence[Sequence[float]]) -> tuple[float, ...]:
    """等权融合全部已保留页面，并重新 L2 归一化。"""
    if not vectors:
        raise ValueError("融合至少需要一个页面向量")
    dimensions = len(vectors[0])
    if any(len(vector) != dimensions for vector in vectors):
        raise ValueError("融合页面的向量维度必须一致")
    normalized = tuple(l2_normalize(vector) for vector in vectors)
    averaged = tuple(
        math.fsum(vector[index] for vector in normalized) / len(normalized)
        for index in range(dimensions)
    )
    return l2_normalize(averaged)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """计算两个同维向量的余弦相似度。"""
    if len(left) != len(right):
        raise ValueError("余弦相似度要求维度一致")
    return math.fsum(a * b for a, b in zip(left, right, strict=True))


def error_json(error: BaseException) -> dict[str, str]:
    """保存有限错误信息。"""
    return {"type": type(error).__name__, "message": str(error)[:2000]}


def resize_png(png_bytes: bytes, x_factor: float, y_factor: float) -> bytes:
    """按给定宽高比例缩放页面并输出 PNG。"""
    if x_factor <= 0 or y_factor <= 0:
        raise ValueError("缩放比例必须大于零")
    with Image.open(io.BytesIO(png_bytes)) as image:
        source = image.convert("RGB")
        size = (
            max(1, round(source.width * x_factor)),
            max(1, round(source.height * y_factor)),
        )
        resized = source.resize(size, Image.Resampling.LANCZOS)
        output = io.BytesIO()
        resized.save(output, format="PNG", optimize=False)
        return output.getvalue()


def jpeg_roundtrip_png(png_bytes: bytes, quality: int) -> bytes:
    """经历 JPEG 质量压缩后转回 PNG，保留压缩伪影并统一媒体类型。"""
    if not 1 <= quality <= 95:
        raise ValueError("JPEG quality 必须在 1 到 95 之间")
    with Image.open(io.BytesIO(png_bytes)) as image:
        source = image.convert("RGB")
        jpeg = io.BytesIO()
        source.save(jpeg, format="JPEG", quality=quality, optimize=False)
        jpeg.seek(0)
        with Image.open(jpeg) as compressed:
            output = io.BytesIO()
            compressed.convert("RGB").save(output, format="PNG", optimize=False)
            return output.getvalue()


VARIANTS: tuple[VariantSpec, ...] = (
    VariantSpec(
        "scale-0.50", "uniform-scale", "宽高同比缩小到 50%", lambda data: resize_png(data, 0.50, 0.50)
    ),
    VariantSpec(
        "scale-0.75", "uniform-scale", "宽高同比缩小到 75%", lambda data: resize_png(data, 0.75, 0.75)
    ),
    VariantSpec(
        "scale-1.25", "uniform-scale", "宽高同比放大到 125%", lambda data: resize_png(data, 1.25, 1.25)
    ),
    VariantSpec(
        "scale-1.50", "uniform-scale", "宽高同比放大到 150%", lambda data: resize_png(data, 1.50, 1.50)
    ),
    VariantSpec(
        "nonuniform-wide-1.35x-0.75y",
        "nonuniform-scale",
        "宽度放大 35%，高度缩小 25%",
        lambda data: resize_png(data, 1.35, 0.75),
    ),
    VariantSpec(
        "nonuniform-tall-0.75x-1.35y",
        "nonuniform-scale",
        "宽度缩小 25%，高度放大 35%",
        lambda data: resize_png(data, 0.75, 1.35),
    ),
    VariantSpec(
        "jpeg-q85", "quality", "JPEG 质量 85 后转回 PNG", lambda data: jpeg_roundtrip_png(data, 85)
    ),
    VariantSpec(
        "jpeg-q60", "quality", "JPEG 质量 60 后转回 PNG", lambda data: jpeg_roundtrip_png(data, 60)
    ),
    VariantSpec(
        "jpeg-q30", "quality", "JPEG 质量 30 后转回 PNG", lambda data: jpeg_roundtrip_png(data, 30)
    ),
    VariantSpec(
        "scale-0.50-jpeg-q30",
        "combined",
        "缩小到 50% 后 JPEG 质量 30，再转回 PNG",
        lambda data: jpeg_roundtrip_png(resize_png(data, 0.50, 0.50), 30),
    ),
    VariantSpec("missing-first", "missing-page", "删除第 1 页", missing_position="first"),
    VariantSpec("missing-middle", "missing-page", "删除中间页", missing_position="middle"),
    VariantSpec("missing-last", "missing-page", "删除最后一页", missing_position="last"),
)


def applicable(spec: VariantSpec, page_count: int) -> bool:
    """判断扰动是否适用于指定页数。"""
    if spec.missing_position == "first" or spec.missing_position == "last":
        return page_count >= 2
    if spec.missing_position == "middle":
        return page_count >= 3
    return True


def kept_page_numbers(spec: VariantSpec, page_count: int) -> tuple[int, ...]:
    """返回扰动后仍参与融合的物理页码。"""
    pages = list(range(1, page_count + 1))
    if spec.missing_position == "first":
        pages.pop(0)
    elif spec.missing_position == "last":
        pages.pop()
    elif spec.missing_position == "middle":
        pages.pop((page_count - 1) // 2)
    return tuple(pages)


def deduplicate_inputs(paths: Sequence[Path]) -> tuple[list[Path], list[dict[str, str]]]:
    """按源 PDF 哈希保留规范路径，记录完全重复路径。"""
    seen: dict[str, Path] = {}
    unique: list[Path] = []
    excluded: list[dict[str, str]] = []
    for candidate in paths:
        path = candidate.resolve()
        source_sha256 = sha256_file(path)
        canonical = seen.get(source_sha256)
        if canonical is None:
            seen[source_sha256] = path
            unique.append(path)
        else:
            excluded.append(
                {
                    "source_sha256": source_sha256,
                    "excluded_relative_path": path.relative_to(PROJECT_ROOT).as_posix(),
                    "canonical_relative_path": canonical.relative_to(PROJECT_ROOT).as_posix(),
                }
            )
    return unique, excluded


async def prepare_documents(paths: Sequence[Path]) -> list[DocumentSample]:
    """使用生产 PDF 准备服务生成 gallery 页面。"""
    settings = get_settings()
    preparation = AsyncPDFPreparationService(settings.mllm)
    samples: list[DocumentSample] = []
    for path in paths:
        prepared = await preparation.prepare(ContractExtractionRequest(pdf_path=path))
        samples.append(
            DocumentSample(
                path=path,
                source_sha256=sha256_file(path),
                prepared=prepared,
            )
        )
    return samples


def build_manifest(
    *,
    started_at: datetime,
    settings: EmbeddingSettings,
    samples: Sequence[DocumentSample],
    discovered_count: int,
    excluded: Sequence[dict[str, str]],
    concurrency: int,
) -> dict[str, object]:
    """形成静态复现清单。"""
    return {
        "experiment_name": EXPERIMENT_NAME,
        "experiment_version": EXPERIMENT_VERSION,
        "started_at": utc_text(started_at),
        "git_commit": git_value("rev-parse", "HEAD"),
        "git_worktree_fingerprint": git_worktree_fingerprint(),
        "python_version": platform.python_version(),
        "model": {
            "provider": settings.provider,
            "base_url": settings.base_url.split("?", 1)[0],
            "model": settings.model,
            "endpoint": settings.endpoint,
            "timeout_seconds": settings.timeout_seconds,
            "dimensions": settings.dimensions,
        },
        "instruction": {
            "version": PDF_PAGE_EMBEDDING_INPUT_VERSION,
            "source": "app.agent.pdf_deduplication.prompt",
        },
        "fusion": {
            "version": "arithmetic-mean-l2-v1",
            "method": "page_l2_then_arithmetic_mean_then_l2",
        },
        "concurrency": concurrency,
        "discovered_file_count": discovered_count,
        "unique_sample_count": len(samples),
        "excluded_exact_duplicates": list(excluded),
        "variants": [
            {
                "name": spec.name,
                "family": spec.family,
                "description": spec.description,
                "missing_position": spec.missing_position,
            }
            for spec in VARIANTS
        ],
        "samples": [
            {
                "relative_path": sample.path.relative_to(PROJECT_ROOT).as_posix(),
                "source_name": sample.path.name,
                "source_sha256": sample.source_sha256,
                "document_id": sample.prepared.document_id,
                "page_count": sample.prepared.page_count,
                "processed_size_bytes": sample.prepared.processed_file_size_bytes,
                "gallery_pages": [
                    {
                        "page_number": page.page_number,
                        "image_sha256": page.content_sha256,
                        "width_pixels": page.width_pixels,
                        "height_pixels": page.height_pixels,
                        "visual_tokens": page.visual_tokens,
                    }
                    for page in sample.prepared.pages
                ],
            }
            for sample in samples
        ],
    }


async def embed_page(
    *,
    client: AsyncOpenAI,
    semaphore: asyncio.Semaphore,
    settings: EmbeddingSettings,
    sample: DocumentSample,
    variant: str,
    family: str,
    page_number: int,
    image_bytes: bytes,
    width_pixels: int,
    height_pixels: int,
    visual_tokens: int,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """请求单页向量并返回不含图片的请求摘要和可复用向量。"""
    started = perf_counter()
    image_sha256 = sha256_bytes(image_bytes)
    common: dict[str, object] = {
        "document_id": sample.prepared.document_id,
        "source_sha256": sample.source_sha256,
        "source_name": sample.path.name,
        "variant": variant,
        "family": family,
        "page_number": page_number,
        "image_sha256": image_sha256,
        "width_pixels": width_pixels,
        "height_pixels": height_pixels,
        "visual_tokens": visual_tokens,
    }
    try:
        data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        async with semaphore:
            acquired = perf_counter()
            response = await client.post(
                "/embeddings",
                cast_to=CreateEmbeddingResponse,
                body={
                    "messages": build_pdf_page_embedding_messages(data_url),
                    "model": settings.model,
                    "encoding_format": "float",
                    "continue_final_message": True,
                    "add_special_tokens": True,
                },
            )
            http_elapsed_ms = (perf_counter() - acquired) * 1000
        if len(response.data) != 1:
            raise ValueError(f"单页请求返回向量数为 {len(response.data)}，预期为 1")
        vector = tuple(float(value) for value in response.data[0].embedding)
        if len(vector) != settings.dimensions:
            raise ValueError(
                f"向量维度不符：expected={settings.dimensions}, actual={len(vector)}"
            )
        l2_normalize(vector)
        usage = response.usage
        return (
            {
                **common,
                "status": "succeeded",
                "model": response.model,
                "dimensions": len(vector),
                "queue_elapsed_ms": round((acquired - started) * 1000, 3),
                "http_elapsed_ms": round(http_elapsed_ms, 3),
                "elapsed_ms": round((perf_counter() - started) * 1000, 3),
                "prompt_tokens": usage.prompt_tokens if usage is not None else None,
                "total_tokens": usage.total_tokens if usage is not None else None,
            },
            {
                **common,
                "model": response.model,
                "dimensions": len(vector),
                "vector": vector,
            },
        )
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        return (
            {
                **common,
                "status": "failed",
                "elapsed_ms": round((perf_counter() - started) * 1000, 3),
                "error": error_json(error),
            },
            None,
        )


def query_page_records(
    sample: DocumentSample,
    spec: VariantSpec,
) -> list[tuple[int, bytes, int, int, int]]:
    """生成一个变体的页面字节和元数据。"""
    kept = set(kept_page_numbers(spec, sample.prepared.page_count))
    records: list[tuple[int, bytes, int, int, int]] = []
    for page in sample.prepared.pages:
        if page.page_number not in kept:
            continue
        image_bytes = page.png_bytes if spec.transform is None else spec.transform(page.png_bytes)
        with Image.open(io.BytesIO(image_bytes)) as image:
            width, height = image.size
        records.append((page.page_number, image_bytes, width, height, 0))
    return records


def build_metrics(
    *,
    samples: Sequence[DocumentSample],
    spec: VariantSpec,
    gallery: dict[str, tuple[float, ...]],
    query: dict[tuple[str, int], tuple[float, ...]],
) -> dict[str, object]:
    """对一个扰动变体执行合同级融合和精确排序。"""
    rankings: list[dict[str, object]] = []
    by_bucket: dict[str, list[dict[str, object]]] = {"single": [], "multi": []}
    for sample in samples:
        doc_id = sample.prepared.document_id
        kept = kept_page_numbers(spec, sample.prepared.page_count)
        page_vectors = [query[(doc_id, page)] for page in kept if (doc_id, page) in query]
        query_vector = fuse_page_vectors(page_vectors) if len(page_vectors) == len(kept) else None
        if query_vector is None or doc_id not in gallery:
            ranking = {
                "source_name": sample.path.name,
                "document_id": doc_id,
                "page_count": sample.prepared.page_count,
                "kept_page_numbers": list(kept),
                "page_coverage_ratio": len(kept) / sample.prepared.page_count,
                "status": "failed",
                "reason": "query 或 gallery 页面向量不完整",
            }
            rankings.append(ranking)
            by_bucket["multi" if sample.prepared.page_count > 1 else "single"].append(ranking)
            continue
        candidates = sorted(
            (
                {
                    "document_id": candidate_id,
                    "similarity": round(cosine(query_vector, vector), 8),
                    "is_positive": candidate_id == doc_id,
                }
                for candidate_id, vector in gallery.items()
            ),
            key=lambda item: (-float(item["similarity"]), str(item["document_id"])),
        )
        for rank, candidate in enumerate(candidates, start=1):
            candidate["rank"] = rank
        positive = next(item for item in candidates if item["is_positive"])
        negatives = [float(item["similarity"]) for item in candidates if not item["is_positive"]]
        strongest_negative = max(negatives) if negatives else None
        margin = (
            float(positive["similarity"]) - strongest_negative
            if strongest_negative is not None
            else None
        )
        ranking = {
            "source_name": sample.path.name,
            "document_id": doc_id,
            "page_count": sample.prepared.page_count,
            "kept_page_numbers": list(kept),
            "page_coverage_ratio": len(kept) / sample.prepared.page_count,
            "status": "succeeded",
            "positive_rank": positive["rank"],
            "positive_similarity": positive["similarity"],
            "strongest_negative_similarity": strongest_negative,
            "positive_negative_margin": round(margin, 8) if margin is not None else None,
            "top_3": candidates[:3],
        }
        rankings.append(ranking)
        by_bucket["multi" if sample.prepared.page_count > 1 else "single"].append(ranking)

    def bucket_metrics(items: Sequence[dict[str, object]]) -> dict[str, object]:
        succeeded = [item for item in items if item["status"] == "succeeded"]
        ranks = [int(item["positive_rank"]) for item in succeeded]
        margins = [float(item["positive_negative_margin"]) for item in succeeded if item["positive_negative_margin"] is not None]
        return {
            "query_count": len(items),
            "completed_count": len(succeeded),
            "recall_at_1": sum(rank <= 1 for rank in ranks) / len(items) if items else None,
            "recall_at_3": sum(rank <= 3 for rank in ranks) / len(items) if items else None,
            "mrr": sum((1 / rank) for rank in ranks) / len(items) if items else None,
            "mean_positive_similarity": sum(float(item["positive_similarity"]) for item in succeeded) / len(succeeded) if succeeded else None,
            "mean_strongest_negative_similarity": sum(float(item["strongest_negative_similarity"]) for item in succeeded if item["strongest_negative_similarity"] is not None) / len([item for item in succeeded if item["strongest_negative_similarity"] is not None]) if any(item["strongest_negative_similarity"] is not None for item in succeeded) else None,
            "mean_positive_negative_margin": sum(margins) / len(margins) if margins else None,
            "min_positive_negative_margin": min(margins) if margins else None,
        }

    metrics = bucket_metrics(rankings)
    return {
        "variant": spec.name,
        "family": spec.family,
        "description": spec.description,
        "applicable_contract_count": len(rankings),
        "kept_page_policy": spec.missing_position or "all pages",
        "metrics": metrics,
        "subset_metrics": {name: bucket_metrics(items) for name, items in by_bucket.items() if items},
        "rankings": rankings,
    }


async def run(args: argparse.Namespace) -> Path:
    """执行完整鲁棒性实验。"""
    if args.concurrency <= 0:
        raise ValueError("并发数必须大于零")
    input_dir = args.input_dir.resolve()
    discovered = sorted(input_dir.rglob("*.pdf"), key=lambda path: path.as_posix())
    if args.max_contracts is not None:
        if args.max_contracts <= 0:
            raise ValueError("max-contracts 必须大于零")
        discovered = discovered[: args.max_contracts]
    if not discovered:
        raise FileNotFoundError(f"输入目录没有 PDF：{input_dir}")
    paths, excluded = deduplicate_inputs(discovered)
    started_at = utc_now()
    started = perf_counter()
    output_dir = args.output_root.resolve() / output_timestamp(started_at)
    output_dir.mkdir(parents=True, exist_ok=False)
    settings = get_settings()
    samples = await prepare_documents(paths)
    write_json(
        output_dir / "manifest.json",
        build_manifest(
            started_at=started_at,
            settings=settings.embedding,
            samples=samples,
            discovered_count=len(discovered),
            excluded=excluded,
            concurrency=args.concurrency,
        ),
    )

    semaphore = asyncio.Semaphore(min(args.concurrency, settings.embedding.max_concurrent_requests))
    request_tasks = []
    async with AsyncOpenAI(
        api_key=settings.embedding.api_key or "vllm-local",
        base_url=settings.embedding.base_url.rstrip("/") + "/",
        timeout=settings.embedding.timeout_seconds,
        max_retries=0,
    ) as client:
        for sample in samples:
            for page in sample.prepared.pages:
                request_tasks.append(
                    embed_page(
                        client=client,
                        semaphore=semaphore,
                        settings=settings.embedding,
                        sample=sample,
                        variant="gallery",
                        family="gallery",
                        page_number=page.page_number,
                        image_bytes=page.png_bytes,
                        width_pixels=page.width_pixels,
                        height_pixels=page.height_pixels,
                        visual_tokens=page.visual_tokens,
                    )
                )
            for spec in VARIANTS:
                if not applicable(spec, sample.prepared.page_count):
                    continue
                for page_number, image_bytes, width, height, _ in query_page_records(sample, spec):
                    request_tasks.append(
                        embed_page(
                            client=client,
                            semaphore=semaphore,
                            settings=settings.embedding,
                            sample=sample,
                            variant=spec.name,
                            family=spec.family,
                            page_number=page_number,
                            image_bytes=image_bytes,
                            width_pixels=width,
                            height_pixels=height,
                            visual_tokens=0,
                        )
                    )
        print(
            f"开始 {len(request_tasks)} 个页面请求：contracts={len(samples)}, "
            f"variants={len(VARIANTS)}, concurrency={min(args.concurrency, settings.embedding.max_concurrent_requests)}",
            flush=True,
        )
        outcomes = await asyncio.gather(*request_tasks)

    request_records = [outcome[0] for outcome in outcomes]
    embedding_records = [outcome[1] for outcome in outcomes if outcome[1] is not None]
    write_json(output_dir / "requests.json", {"requests": request_records})
    write_json(output_dir / "page-embeddings.json", {"embeddings": embedding_records})

    gallery: dict[str, tuple[float, ...]] = {}
    gallery_pages: dict[tuple[str, int], Sequence[float]] = {}
    query_pages: dict[tuple[str, int, str], Sequence[float]] = {}
    for record in embedding_records:
        document_id = str(record["document_id"])
        variant = str(record["variant"])
        page_number = int(record["page_number"])
        vector = record["vector"]
        if variant == "gallery":
            gallery_pages[(document_id, page_number)] = vector
        else:
            query_pages[(document_id, page_number, variant)] = vector
    for sample in samples:
        pages = [gallery_pages[(sample.prepared.document_id, page.page_number)] for page in sample.prepared.pages if (sample.prepared.document_id, page.page_number) in gallery_pages]
        if len(pages) == sample.prepared.page_count:
            gallery[sample.prepared.document_id] = fuse_page_vectors(pages)

    variant_results: dict[str, dict[str, object]] = {}
    for spec in VARIANTS:
        applicable_samples = [sample for sample in samples if applicable(spec, sample.prepared.page_count)]
        query: dict[tuple[str, int], Sequence[float]] = {}
        for sample in applicable_samples:
            for page_number in kept_page_numbers(spec, sample.prepared.page_count):
                vector = query_pages.get((sample.prepared.document_id, page_number, spec.name))
                if vector is not None:
                    query[(sample.prepared.document_id, page_number)] = vector
        variant_results[spec.name] = build_metrics(
            samples=applicable_samples,
            spec=spec,
            gallery=gallery,
            query=query,
        )

    request_successes = sum(record["status"] == "succeeded" for record in request_records)
    result = {
        "experiment_name": EXPERIMENT_NAME,
        "experiment_version": EXPERIMENT_VERSION,
        "started_at": utc_text(started_at),
        "completed_at": utc_text(),
        "end_to_end_elapsed_ms": round((perf_counter() - started) * 1000, 3),
        "request_summary": {
            "request_count": len(request_records),
            "successful_request_count": request_successes,
            "failed_request_count": len(request_records) - request_successes,
            "success_rate": request_successes / len(request_records) if request_records else None,
            "http_elapsed_ms_mean": (
                sum(float(record["http_elapsed_ms"]) for record in request_records if record["status"] == "succeeded") / request_successes
                if request_successes
                else None
            ),
            "queue_elapsed_ms_mean": (
                sum(float(record["queue_elapsed_ms"]) for record in request_records if record["status"] == "succeeded") / request_successes
                if request_successes
                else None
            ),
            "prompt_tokens_total": sum(int(record.get("prompt_tokens") or 0) for record in request_records),
        },
        "instruction_version": PDF_PAGE_EMBEDDING_INPUT_VERSION,
        "fusion_version": "arithmetic-mean-l2-v1",
        "variant_results": variant_results,
    }
    write_json(output_dir / "result.json", result)
    print(output_dir, flush=True)
    return output_dir


def main() -> None:
    """命令行入口。"""
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
