"""复用页面向量，比较首页和尾页加权的合同召回实验。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_RUN = (
    PROJECT_ROOT
    / "experiment/pdf-page-embedding-robustness/output/20260901T111326.358868Z"
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "experiment/pdf-page-fusion-weighting/output"
STRATEGIES = {
    "uniform": "所有保留页面权重 1.0",
    "home-1.5": "原始物理第 1 页权重 1.5，其余页面权重 1.0",
    "tail-1.5": "原始物理最后一页权重 1.5，其余页面权重 1.0",
    "both-ends-1.5": "原始物理第 1 页和最后一页权重 1.5，其余页面权重 1.0",
}


def parse_args() -> argparse.Namespace:
    """解析实验参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, default=DEFAULT_SOURCE_RUN)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args()


def utc_now() -> datetime:
    """返回当前 UTC 时间。"""
    return datetime.now(UTC)


def utc_text(value: datetime | None = None) -> str:
    """生成 JSON 使用的 UTC ISO 时间。"""
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def output_timestamp(value: datetime | None = None) -> str:
    """生成唯一 UTC 运行目录名。"""
    return (value or utc_now()).strftime("%Y%m%dT%H%M%S.%fZ")


def write_json(path: Path, value: object) -> None:
    """写入稳定格式 JSON。"""
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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


def l2_normalize(vector: Sequence[float]) -> tuple[float, ...]:
    """L2 归一化并拒绝零向量。"""
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if not vector or not math.isfinite(norm) or norm == 0:
        raise ValueError("向量不能为空、必须有限且不能为零向量")
    return tuple(value / norm for value in vector)


def fuse(
    page_vectors: dict[int, Sequence[float]],
    *,
    page_count: int,
    strategy: str,
) -> tuple[float, ...]:
    """按原始物理页码应用权重，再归一化合同向量。"""
    if not page_vectors:
        raise ValueError("融合至少需要一个页面向量")
    dimensions = len(next(iter(page_vectors.values())))
    weighted_sum = [0.0] * dimensions
    total_weight = 0.0
    for page_number, raw_vector in sorted(page_vectors.items()):
        vector = l2_normalize(raw_vector)
        if strategy == "home-1.5":
            weight = 1.5 if page_number == 1 else 1.0
        elif strategy == "tail-1.5":
            weight = 1.5 if page_number == page_count else 1.0
        elif strategy == "both-ends-1.5":
            weight = 1.5 if page_number in {1, page_count} else 1.0
        else:
            weight = 1.0
        total_weight += weight
        for index, value in enumerate(vector):
            weighted_sum[index] += weight * value
    return l2_normalize(tuple(value / total_weight for value in weighted_sum))


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """计算余弦相似度。"""
    return math.fsum(a * b for a, b in zip(left, right, strict=True))


def metrics_for_rankings(rankings: Sequence[dict[str, object]]) -> dict[str, object]:
    """汇总一个策略的一组 query 排名。"""
    succeeded = [item for item in rankings if item["status"] == "succeeded"]
    ranks = [int(item["positive_rank"]) for item in succeeded]
    margins = [
        float(item["positive_negative_margin"])
        for item in succeeded
        if item["positive_negative_margin"] is not None
    ]
    positive = [float(item["positive_similarity"]) for item in succeeded]
    negative = [
        float(item["strongest_negative_similarity"])
        for item in succeeded
        if item["strongest_negative_similarity"] is not None
    ]
    count = len(rankings)
    return {
        "query_count": count,
        "completed_count": len(succeeded),
        "recall_at_1": sum(rank <= 1 for rank in ranks) / count if count else None,
        "recall_at_3": sum(rank <= 3 for rank in ranks) / count if count else None,
        "mrr": sum(1 / rank for rank in ranks) / count if count else None,
        "mean_positive_similarity": sum(positive) / len(positive) if positive else None,
        "mean_strongest_negative_similarity": sum(negative) / len(negative) if negative else None,
        "mean_positive_negative_margin": sum(margins) / len(margins) if margins else None,
        "min_positive_negative_margin": min(margins) if margins else None,
    }


def run(args: argparse.Namespace) -> Path:
    """读取页面向量并执行三组融合策略。"""
    source_run = args.source_run.resolve()
    manifest = json.loads((source_run / "manifest.json").read_text(encoding="utf-8"))
    source_result = json.loads((source_run / "result.json").read_text(encoding="utf-8"))
    source_embeddings = json.loads(
        (source_run / "page-embeddings.json").read_text(encoding="utf-8")
    )["embeddings"]
    source_samples = [
        sample for sample in manifest["samples"] if int(sample["page_count"]) >= 2
    ]
    source_ids = {sample["document_id"] for sample in source_samples}
    sample_by_id = {sample["document_id"]: sample for sample in source_samples}
    gallery_pages: dict[tuple[str, int], Sequence[float]] = {}
    query_pages: dict[tuple[str, str, int], Sequence[float]] = {}
    for record in source_embeddings:
        document_id = str(record["document_id"])
        if document_id not in source_ids:
            continue
        page_number = int(record["page_number"])
        vector = record["vector"]
        variant = str(record["variant"])
        if variant == "gallery":
            gallery_pages[(document_id, page_number)] = vector
        else:
            query_pages[(document_id, variant, page_number)] = vector

    gallery: dict[str, tuple[float, ...]] = {}
    for document_id, sample in sample_by_id.items():
        page_count = int(sample["page_count"])
        pages = {
            page: gallery_pages[(document_id, page)]
            for page in range(1, page_count + 1)
            if (document_id, page) in gallery_pages
        }
        if len(pages) == page_count:
            gallery[document_id] = fuse(pages, page_count=page_count, strategy="uniform")

    variant_names = tuple(source_result["variant_results"])
    rankings_by_strategy: dict[str, list[dict[str, object]]] = {
        strategy: [] for strategy in STRATEGIES
    }
    for variant in variant_names:
        source_variant = source_result["variant_results"][variant]
        for source_ranking in source_variant["rankings"]:
            document_id = source_ranking["document_id"]
            if document_id not in source_ids:
                continue
            kept_pages = tuple(int(page) for page in source_ranking["kept_page_numbers"])
            page_count = int(sample_by_id[document_id]["page_count"])
            query = {
                page: query_pages[(document_id, variant, page)]
                for page in kept_pages
                if (document_id, variant, page) in query_pages
            }
            for strategy in STRATEGIES:
                ranking_base: dict[str, object] = {
                    "variant": variant,
                    "source_name": sample_by_id[document_id]["source_name"],
                    "document_id": document_id,
                    "page_count": page_count,
                    "kept_page_numbers": list(kept_pages),
                    "page_coverage_ratio": len(kept_pages) / page_count,
                }
                if len(query) != len(kept_pages) or document_id not in gallery:
                    rankings_by_strategy[strategy].append(
                        {**ranking_base, "status": "failed", "reason": "向量不完整"}
                    )
                    continue
                query_vector = fuse(query, page_count=page_count, strategy=strategy)
                candidates = sorted(
                    (
                        {
                            "document_id": candidate_id,
                            "similarity": round(cosine(query_vector, vector), 8),
                            "is_positive": candidate_id == document_id,
                        }
                        for candidate_id, vector in gallery.items()
                    ),
                    key=lambda item: (-float(item["similarity"]), str(item["document_id"])),
                )
                for rank, candidate in enumerate(candidates, start=1):
                    candidate["rank"] = rank
                positive = next(item for item in candidates if item["is_positive"])
                negatives = [
                    float(item["similarity"])
                    for item in candidates
                    if not item["is_positive"]
                ]
                strongest_negative = max(negatives) if negatives else None
                margin = (
                    float(positive["similarity"]) - strongest_negative
                    if strongest_negative is not None
                    else None
                )
                rankings_by_strategy[strategy].append(
                    {
                        **ranking_base,
                        "status": "succeeded",
                        "positive_rank": positive["rank"],
                        "positive_similarity": positive["similarity"],
                        "strongest_negative_similarity": strongest_negative,
                        "positive_negative_margin": round(margin, 8) if margin is not None else None,
                        "top_3": candidates[:3],
                    }
                )

    started = utc_now()
    output_dir = args.output_root.resolve() / output_timestamp(started)
    output_dir.mkdir(parents=True, exist_ok=False)
    result = {
        "experiment_name": "pdf-page-fusion-weighting",
        "experiment_version": "1.1.0",
        "started_at": utc_text(started),
        "completed_at": utc_text(),
        "source_run": source_run.relative_to(PROJECT_ROOT).as_posix(),
        "instruction_version": manifest["instruction"]["version"],
        "sample_count": len(source_samples),
        "page_count": sum(int(sample["page_count"]) for sample in source_samples),
        "variant_count": len(variant_names),
        "strategy_results": {
            strategy: {
                "weight_rule": rule,
                "metrics": metrics_for_rankings(rankings),
                "by_variant": {
                    variant: metrics_for_rankings(
                        [item for item in rankings if item["variant"] == variant]
                    )
                    for variant in variant_names
                },
                "rankings": rankings,
            }
            for strategy, rule in STRATEGIES.items()
            for rankings in [rankings_by_strategy[strategy]]
        },
    }
    baseline = result["strategy_results"]["uniform"]["metrics"]
    comparison: dict[str, dict[str, float | None]] = {}
    for strategy in ("home-1.5", "tail-1.5", "both-ends-1.5"):
        current = result["strategy_results"][strategy]["metrics"]
        comparison[strategy] = {
            key: (
                float(current[key]) - float(baseline[key])
                if current[key] is not None and baseline[key] is not None
                else None
            )
            for key in (
                "recall_at_1",
                "recall_at_3",
                "mrr",
                "mean_positive_negative_margin",
                "min_positive_negative_margin",
            )
        }
    result["comparison_to_uniform"] = comparison
    write_json(
        output_dir / "manifest.json",
        {
            "experiment_name": "pdf-page-fusion-weighting",
            "experiment_version": "1.1.0",
            "started_at": utc_text(started),
            "git_commit": git_value("rev-parse", "HEAD"),
            "git_worktree_fingerprint": git_value("status", "--porcelain=v1"),
            "python_version": platform.python_version(),
            "source_run": source_run.relative_to(PROJECT_ROOT).as_posix(),
            "source_run_sha256": hashlib.sha256(
                (source_run / "result.json").read_bytes()
            ).hexdigest(),
            "filter": {"minimum_page_count": 2},
            "instruction_version": manifest["instruction"]["version"],
            "fusion_strategies": STRATEGIES,
            "sample_count": len(source_samples),
            "page_count": sum(int(sample["page_count"]) for sample in source_samples),
            "variant_count": len(variant_names),
            "samples": source_samples,
        },
    )
    write_json(output_dir / "result.json", result)
    print(output_dir, flush=True)
    return output_dir


if __name__ == "__main__":
    run(parse_args())
