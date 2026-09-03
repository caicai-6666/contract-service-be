"""验证 PDF 查重全量文档判断路线对常见页面变换的鲁棒性。"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import platform
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Callable, Literal

import pymupdf
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.contract_extraction.state import ContractExtractionRequest, PreparedPDF
from app.agent.pdf_deduplication.prompt import (
    FULL_DOCUMENT_JUDGMENT_PROMPT_VERSION,
)
from app.agent.pdf_deduplication.state import PDFDuplicateCandidate
from app.agent.pdf_deduplication.subgraph.candidate_judgment.node import (
    decide_candidate_judgment_route,
    judge_full_documents,
)
from app.agent.pdf_deduplication.subgraph.candidate_judgment.state import (
    PDFCandidateJudgmentState,
    PDFCandidateRoutingDecision,
)
from app.core.config import get_settings
from app.infrastructure.inference_metrics import (
    InferenceRequestMetrics,
    bind_inference_metrics_observer,
    bind_inference_stage,
)
from app.service.pdf_preparation import AsyncPDFPreparationService

EXPERIMENT_NAME = "pdf-full-document-deduplication"
EXPERIMENT_VERSION = "1.0.0"
INPUT_ROOT = PROJECT_ROOT / "data/input/test-data"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "experiment" / EXPERIMENT_NAME / "output"

ExpectedRelation = Literal["duplicate", "different"]


@dataclass(frozen=True, slots=True)
class VariantSpec:
    """一项确定性页面级变换。"""

    name: str
    description: str
    transform: Callable[[bytes], bytes] | None = None
    missing_position: Literal["middle"] | None = None


@dataclass(frozen=True, slots=True)
class CaseSpec:
    """一对上传变换件、候选原件及其人工预期。"""

    name: str
    uploaded_source: str
    variant: str
    candidate_source: str
    expected_relation: ExpectedRelation


@dataclass(frozen=True, slots=True)
class PreparedCase:
    """已通过生产预处理和路由计算的实验用例。"""

    spec: CaseSpec
    uploaded_pdf: PreparedPDF
    candidate_pdf: PreparedPDF
    transformed_sha256: str
    routing: PDFCandidateRoutingDecision
    preparation_elapsed_ms: float


def resize_png(png_bytes: bytes, x_factor: float, y_factor: float) -> bytes:
    """改变页面像素宽高，保留指定的几何形变。"""
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
    """通过 JPEG 往返产生可复现的有损压缩伪影。"""
    with Image.open(io.BytesIO(png_bytes)) as image:
        jpeg = io.BytesIO()
        image.convert("RGB").save(
            jpeg,
            format="JPEG",
            quality=quality,
            optimize=False,
        )
        jpeg.seek(0)
        with Image.open(jpeg) as compressed:
            output = io.BytesIO()
            compressed.convert("RGB").save(output, format="PNG", optimize=False)
            return output.getvalue()


VARIANTS: dict[str, VariantSpec] = {
    "nonuniform-wide": VariantSpec(
        name="nonuniform-wide",
        description="页面宽度放大至 1.35 倍、高度缩小至 0.75 倍",
        transform=lambda value: resize_png(value, 1.35, 0.75),
    ),
    "missing-middle": VariantSpec(
        name="missing-middle",
        description="删除三页合同的中间页",
        missing_position="middle",
    ),
    "jpeg-q30": VariantSpec(
        name="jpeg-q30",
        description="页面以 JPEG quality 30 有损压缩后转回 PNG",
        transform=lambda value: jpeg_roundtrip_png(value, 30),
    ),
    "scale-half-jpeg-q30": VariantSpec(
        name="scale-half-jpeg-q30",
        description="页面缩小至 50% 后以 JPEG quality 30 压缩",
        transform=lambda value: jpeg_roundtrip_png(
            resize_png(value, 0.5, 0.5),
            30,
        ),
    ),
}

CASES: tuple[CaseSpec, ...] = (
    CaseSpec(
        name="same-nonuniform-wide",
        uploaded_source="2025072502深圳现象光伏有限公司_已签章.pdf",
        variant="nonuniform-wide",
        candidate_source="2025072502深圳现象光伏有限公司_已签章.pdf",
        expected_relation="duplicate",
    ),
    CaseSpec(
        name="same-missing-middle",
        uploaded_source="2025年蟹卡合同-55份_已签章.pdf",
        variant="missing-middle",
        candidate_source="2025年蟹卡合同-55份_已签章.pdf",
        expected_relation="duplicate",
    ),
    CaseSpec(
        name="same-jpeg-q30",
        uploaded_source="ET-3030加热台合同2025-04-03_已签章.pdf",
        variant="jpeg-q30",
        candidate_source="ET-3030加热台合同2025-04-03_已签章.pdf",
        expected_relation="duplicate",
    ),
    CaseSpec(
        name="same-scale-half-jpeg-q30",
        uploaded_source="源展&现象光伏_已签章(1).pdf",
        variant="scale-half-jpeg-q30",
        candidate_source="源展&现象光伏_已签章(1).pdf",
        expected_relation="duplicate",
    ),
    CaseSpec(
        name="cross-quality",
        uploaded_source="ET-3030加热台合同2025-04-03_已签章.pdf",
        variant="jpeg-q30",
        candidate_source="2025年蟹卡合同-55份_已签章.pdf",
        expected_relation="different",
    ),
    CaseSpec(
        name="cross-nonuniform",
        uploaded_source="2025072502深圳现象光伏有限公司_已签章.pdf",
        variant="nonuniform-wide",
        candidate_source="北京精雕-Holder合同(1)_已签.pdf",
        expected_relation="different",
    ),
)


def parse_args() -> argparse.Namespace:
    """解析可复现实验参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="只执行 PDF 准备与正式路由预检，不调用 MLLM。",
    )
    args = parser.parse_args()
    if args.concurrency <= 0:
        parser.error("--concurrency 必须大于 0")
    return args


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


def render_source_pages(path: Path) -> list[bytes]:
    """以固定 2 倍矩阵将源 PDF 栅格化，作为变换的唯一输入。"""
    pages: list[bytes] = []
    with pymupdf.open(path) as document:
        for page in document:
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
            pages.append(pixmap.tobytes("png"))
    return pages


def build_transformed_pdf(path: Path, variant: VariantSpec) -> bytes:
    """对源页面执行变换并重新封装为纯图像 PDF。"""
    pages = render_source_pages(path)
    if variant.missing_position == "middle":
        if len(pages) < 3:
            raise ValueError(f"{variant.name} 要求源 PDF 至少有 3 页：{path.name}")
        del pages[(len(pages) - 1) // 2]
    elif variant.transform is not None:
        pages = [variant.transform(page) for page in pages]

    transformed = pymupdf.open()
    try:
        for png_bytes in pages:
            with Image.open(io.BytesIO(png_bytes)) as image:
                # 以 144 DPI 对应的物理尺寸封装，保留变换后的宽高比与分辨率。
                page = transformed.new_page(
                    width=max(1, image.width / 2),
                    height=max(1, image.height / 2),
                )
            page.insert_image(page.rect, stream=png_bytes, keep_proportion=False)
        return transformed.tobytes(garbage=4, deflate=True)
    finally:
        transformed.close()


def build_state(
    uploaded_pdf: PreparedPDF,
    candidate_pdf: PreparedPDF,
    candidate_name: str,
) -> PDFCandidateJudgmentState:
    """构造与 ES 召回结果相同契约的单候选节点输入。"""
    candidate = PDFDuplicateCandidate(
        rank=1,
        document_id=candidate_pdf.document_id,
        file_name=candidate_name,
        file_uri=f"/{candidate_pdf.document_id}.pdf",
        page_count=candidate_pdf.page_count,
        score=1.0,
    )
    return {
        "uploaded_pdf": uploaded_pdf,
        "candidate_pdf": candidate_pdf,
        "candidate": candidate,
    }


async def prepare_cases() -> tuple[list[PreparedCase], dict[str, dict[str, object]]]:
    """缓存原件和变换件的生产预处理结果，并执行正式路由。"""
    settings = get_settings()
    service = AsyncPDFPreparationService(settings.mllm)
    source_names = sorted(
        {case.uploaded_source for case in CASES}
        | {case.candidate_source for case in CASES}
    )
    source_paths = {name: INPUT_ROOT / name for name in source_names}
    missing = [name for name, path in source_paths.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"实验固定样本不存在：{missing}")

    originals: dict[str, PreparedPDF] = {}
    source_manifest: dict[str, dict[str, object]] = {}
    for name, path in source_paths.items():
        prepared = await service.prepare(ContractExtractionRequest(pdf_path=path))
        originals[name] = prepared
        source_manifest[name] = {
            "relative_path": path.relative_to(PROJECT_ROOT).as_posix(),
            "source_sha256": sha256_file(path),
            "processed_document_id": prepared.document_id,
            "page_count": prepared.page_count,
            "total_visual_tokens": prepared.total_visual_tokens,
        }

    transformed_cache: dict[tuple[str, str], tuple[PreparedPDF, str]] = {}
    prepared_cases: list[PreparedCase] = []
    for spec in CASES:
        started = perf_counter()
        cache_key = (spec.uploaded_source, spec.variant)
        cached = transformed_cache.get(cache_key)
        if cached is None:
            transformed_bytes = build_transformed_pdf(
                source_paths[spec.uploaded_source],
                VARIANTS[spec.variant],
            )
            transformed_sha256 = sha256_bytes(transformed_bytes)
            uploaded_pdf = await service.prepare(
                ContractExtractionRequest(
                    pdf_bytes=transformed_bytes,
                    file_name=f"{spec.name}.pdf",
                )
            )
            cached = (uploaded_pdf, transformed_sha256)
            transformed_cache[cache_key] = cached
        uploaded_pdf, transformed_sha256 = cached
        candidate_pdf = originals[spec.candidate_source]
        routed_state = decide_candidate_judgment_route(
            build_state(uploaded_pdf, candidate_pdf, spec.candidate_source)
        )
        prepared_cases.append(
            PreparedCase(
                spec=spec,
                uploaded_pdf=uploaded_pdf,
                candidate_pdf=candidate_pdf,
                transformed_sha256=transformed_sha256,
                routing=routed_state["routing_decision"],
                preparation_elapsed_ms=round(
                    (perf_counter() - started) * 1000,
                    3,
                ),
            )
        )
    return prepared_cases, source_manifest


def route_dict(case: PreparedCase) -> dict[str, object]:
    return {
        "case": case.spec.name,
        "expected_relation": case.spec.expected_relation,
        "uploaded_source": case.spec.uploaded_source,
        "candidate_source": case.spec.candidate_source,
        "variant": case.spec.variant,
        "transformed_pdf_sha256": case.transformed_sha256,
        "uploaded_processed_document_id": case.uploaded_pdf.document_id,
        "candidate_processed_document_id": case.candidate_pdf.document_id,
        "uploaded_page_count": case.uploaded_pdf.page_count,
        "candidate_page_count": case.candidate_pdf.page_count,
        "uploaded_visual_tokens": case.uploaded_pdf.total_visual_tokens,
        "candidate_visual_tokens": case.candidate_pdf.total_visual_tokens,
        "preparation_elapsed_ms": case.preparation_elapsed_ms,
        "routing_decision": case.routing.model_dump(mode="json"),
    }


def sum_optional(values: Sequence[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def build_result(
    *,
    started_at: datetime,
    finished_at: datetime,
    preflight_only: bool,
    cases: Sequence[PreparedCase],
    outcomes: Sequence[dict[str, object]],
) -> dict[str, object]:
    route_eligible_count = sum(
        case.routing.strategy == "full_document" for case in cases
    )
    if preflight_only:
        status = "preflight_passed" if route_eligible_count == len(cases) else "preflight_failed"
        return {
            "status": status,
            "started_at": utc_text(started_at),
            "finished_at": utc_text(finished_at),
            "preflight_only": True,
            "case_count": len(cases),
            "route_eligible_count": route_eligible_count,
            "route_eligible_rate": route_eligible_count / len(cases),
            "cases": [route_dict(case) for case in cases],
        }

    matched_count = sum(bool(outcome["matched"]) for outcome in outcomes)
    positives = [
        outcome
        for outcome in outcomes
        if outcome["expected_relation"] == "duplicate"
    ]
    negatives = [
        outcome
        for outcome in outcomes
        if outcome["expected_relation"] == "different"
    ]
    failed_count = sum(
        outcome["actual_relation"] == "failed" for outcome in outcomes
    )
    quality_passed = (
        route_eligible_count == len(cases)
        and matched_count == len(outcomes)
        and failed_count == 0
    )
    prompt_values = [outcome["prompt_tokens"] for outcome in outcomes]
    completion_values = [outcome["completion_tokens"] for outcome in outcomes]
    cached_values = [outcome["cached_tokens"] for outcome in outcomes]
    return {
        "status": "passed" if quality_passed else "failed",
        "started_at": utc_text(started_at),
        "finished_at": utc_text(finished_at),
        "preflight_only": False,
        "case_count": len(cases),
        "route_eligible_count": route_eligible_count,
        "route_eligible_rate": route_eligible_count / len(cases),
        "matched_count": matched_count,
        "exact_relation_accuracy": matched_count / len(outcomes),
        "duplicate_recall": (
            sum(item["actual_relation"] == "duplicate" for item in positives)
            / len(positives)
        ),
        "different_recall": (
            sum(item["actual_relation"] == "different" for item in negatives)
            / len(negatives)
        ),
        "failed_count": failed_count,
        "failed_rate": failed_count / len(outcomes),
        "total_model_request_count": sum(
            int(item["model_request_count"]) for item in outcomes
        ),
        "total_model_request_elapsed_ms": round(
            sum(float(item["model_request_elapsed_ms"]) for item in outcomes),
            3,
        ),
        "mean_case_end_to_end_elapsed_ms": round(
            sum(float(item["end_to_end_elapsed_ms"]) for item in outcomes)
            / len(outcomes),
            3,
        ),
        "total_prompt_tokens": sum_optional(prompt_values),
        "total_completion_tokens": sum_optional(completion_values),
        "total_cached_tokens": sum_optional(cached_values),
        "cases": list(outcomes),
    }


def build_manifest(
    *,
    started_at: datetime,
    args: argparse.Namespace,
    source_manifest: dict[str, dict[str, object]],
    cases: Sequence[PreparedCase],
) -> dict[str, object]:
    settings = get_settings()
    mllm = settings.mllm
    routing = settings.pdf_deduplication
    return {
        "experiment": EXPERIMENT_NAME,
        "experiment_version": EXPERIMENT_VERSION,
        "started_at": utc_text(started_at),
        "preflight_only": args.preflight_only,
        "concurrency": args.concurrency,
        "prompt_version": FULL_DOCUMENT_JUDGMENT_PROMPT_VERSION,
        "input_root": INPUT_ROOT.relative_to(PROJECT_ROOT).as_posix(),
        "sources": source_manifest,
        "variants": {
            name: {
                "description": variant.description,
                "missing_position": variant.missing_position,
            }
            for name, variant in VARIANTS.items()
        },
        "cases": [route_dict(case) for case in cases],
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
        "routing": routing.model_dump(mode="json"),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "git_commit": git_value("rev-parse", "HEAD"),
            "git_worktree_fingerprint": git_worktree_fingerprint(),
        },
    }


async def async_main(args: argparse.Namespace) -> int:
    started_at = utc_now()
    output_dir = args.output_root.resolve() / output_timestamp(started_at)
    output_dir.mkdir(parents=True, exist_ok=False)
    print(f"实验输出：{output_dir}", flush=True)
    try:
        cases, source_manifest = await prepare_cases()
    except Exception as exc:
        write_json(
            output_dir / "result.json",
            {
                "status": "setup_failed",
                "started_at": utc_text(started_at),
                "finished_at": utc_text(),
                "error": {"type": type(exc).__name__, "message": str(exc)[:2000]},
            },
        )
        raise

    write_json(
        output_dir / "manifest.json",
        build_manifest(
            started_at=started_at,
            args=args,
            source_manifest=source_manifest,
            cases=cases,
        ),
    )
    (output_dir / "cases").mkdir(exist_ok=False)
    for case in cases:
        case_dir = output_dir / "cases" / case.spec.name
        case_dir.mkdir(exist_ok=False)
        write_json(case_dir / "routing.json", route_dict(case))

    ineligible = [case for case in cases if case.routing.strategy != "full_document"]
    if args.preflight_only or ineligible:
        result = build_result(
            started_at=started_at,
            finished_at=utc_now(),
            preflight_only=True,
            cases=cases,
            outcomes=(),
        )
        write_json(output_dir / "result.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if not ineligible else 2

    # 正式运行前全部用例已经通过 full_document 预检，绝不回退到翻页路线。
    semaphore = asyncio.Semaphore(args.concurrency)

    async def invoke(case: PreparedCase) -> dict[str, object]:
        # 预检目录已创建；节点执行函数仅在其中补充新的原始产物。
        case_dir = output_dir / "cases" / case.spec.name
        metrics: list[InferenceRequestMetrics] = []
        state = build_state(case.uploaded_pdf, case.candidate_pdf, case.spec.candidate_source)
        state["routing_decision"] = case.routing
        started = utc_now()
        counter = perf_counter()
        print(f"[{case.spec.name}] 开始全量判断", flush=True)
        async with semaphore:
            with bind_inference_metrics_observer(metrics.append):
                with bind_inference_stage(f"full_document:{case.spec.name}"):
                    result_state = await judge_full_documents(state)
        elapsed_ms = round((perf_counter() - counter) * 1000, 3)
        judgment = result_state["judgment"]
        write_json(case_dir / "judgment.json", judgment.model_dump(mode="json"))
        write_json(case_dir / "inference-metrics.json", [item.to_dict() for item in metrics])
        actual = judgment.status
        print(
            f"[{case.spec.name}] 完成：expected={case.spec.expected_relation} "
            f"actual={actual} rounds={judgment.rounds} elapsed_ms={elapsed_ms}",
            flush=True,
        )
        return {
            "case": case.spec.name,
            "started_at": utc_text(started),
            "expected_relation": case.spec.expected_relation,
            "actual_relation": actual,
            "matched": actual == case.spec.expected_relation,
            "rounds": judgment.rounds,
            "end_to_end_elapsed_ms": elapsed_ms,
            "node_elapsed_ms": judgment.elapsed_ms,
            "prompt_tokens": judgment.prompt_tokens,
            "completion_tokens": judgment.completion_tokens,
            "cached_tokens": judgment.cached_tokens,
            "model_request_count": len(metrics),
            "model_request_elapsed_ms": round(sum(item.elapsed_ms for item in metrics), 3),
            "failed_model_request_count": sum(not item.succeeded for item in metrics),
            "judgment_artifact": f"cases/{case.spec.name}/judgment.json",
            "routing_artifact": f"cases/{case.spec.name}/routing.json",
            "metrics_artifact": f"cases/{case.spec.name}/inference-metrics.json",
        }

    outcomes = await asyncio.gather(*(invoke(case) for case in cases))
    result = build_result(
        started_at=started_at,
        finished_at=utc_now(),
        preflight_only=False,
        cases=cases,
        outcomes=outcomes,
    )
    write_json(output_dir / "result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["status"] == "passed" else 1


def main() -> int:
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
