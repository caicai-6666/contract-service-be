"""提取 data/contract 中的合同，并写入开发 Elasticsearch 合同索引。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from elasticsearch import AsyncElasticsearch
from app.agent.contract_extraction.state import ContractExtractionRequest
from app.agent.contract_extraction.subgraph.field_extraction.definition import (
    FieldCardinality,
    FieldDefinitionCatalog,
)
from app.agent.pdf_deduplication.node import vectorize_processed_pdf
from app.core.config import get_settings
from app.infrastructure.contract_index import synchronize_contract_index
from app.infrastructure.elasticsearch import create_elasticsearch_client
from app.service.contract_extraction.executor import AgentContractExtractionExecutor
from app.service.pdf_preparation import AsyncPDFPreparationService
from app.agent.contract_extraction.subgraph.classification.catalog import (
    load_contract_category_catalog,
)
from app.agent.contract_extraction.subgraph.field_extraction.catalog import (
    load_field_definition_catalog,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.catalog import (
    load_retrieval_view_guide_catalog,
)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=PROJECT_ROOT / "data/contract")
    parser.add_argument("--file", type=Path, help="只处理指定 PDF；设置后忽略 --input-dir")
    parser.add_argument(
        "--processed-dir",
        type=Path,
        default=PROJECT_ROOT / "data/contract",
        help="处理版 PDF 的统一保存目录",
    )
    parser.add_argument("--reviewer", default="jason")
    parser.add_argument("--index", default=None, help="默认使用 ELASTICSEARCH_INDEX_NAME")
    parser.add_argument("--overwrite", action="store_true", help="重新提取并覆盖已存在文档")
    return parser.parse_args()


def project_core(result, catalog: FieldDefinitionCatalog) -> dict[str, object]:
    definitions = {definition.name: definition for definition in catalog.core.definitions}
    projected: dict[str, object] = {}
    for outcome in result.fields:
        if outcome.status != "extracted":
            continue
        definition = definitions[outcome.name]
        objects = []
        for extracted in outcome.objects:
            objects.append({
                prop.code: extracted.value[prop.name]
                for prop in definition.properties
                if prop.name in extracted.value
            })
        if not objects:
            continue
        if definition.cardinality is FieldCardinality.SINGLE:
            projected[definition.code] = (
                next(iter(objects[0].values()))
                if len(definition.properties) == 1
                else objects[0]
            )
        else:
            projected[definition.code] = objects
    return projected


def project_clauses(result) -> list[dict[str, object]]:
    clauses = []
    for outcome in result.clauses:
        if outcome.status != "extracted":
            continue
        candidate = outcome.candidate
        item: dict[str, object] = {
            "clause_id": candidate.candidate_id,
            "order": candidate.order,
            "identifier": candidate.identifier,
            "path": [
                " ".join(filter(None, (segment.identifier, segment.title_hint)))
                for segment in candidate.document_path
            ],
            "level": candidate.level,
            "start_page": candidate.evidence.start.page_number,
            "end_page": candidate.evidence.end.page_number,
            "content": outcome.content,
        }
        if candidate.title_hint:
            item["title"] = candidate.title_hint
        if candidate.parent_candidate_id:
            item["parent_clause_id"] = candidate.parent_candidate_id
        clauses.append(item)
    return clauses


async def extract_and_index(
    path: Path,
    *,
    executor: AgentContractExtractionExecutor,
    preparation: AsyncPDFPreparationService,
    field_catalog: FieldDefinitionCatalog,
    elasticsearch: AsyncElasticsearch,
    index_name: str,
    reviewer: str,
    processed_dir: Path,
    overwrite: bool,
) -> None:
    started = perf_counter()
    prepared = await preparation.prepare(ContractExtractionRequest(pdf_path=path))
    processed_path = processed_dir / f"{prepared.document_id}.pdf"
    processed_path.write_bytes(prepared.processed_pdf_bytes)
    if not overwrite and await elasticsearch.exists(index=index_name, id=prepared.document_id):
        print(f"{path.name}: skipped id={prepared.document_id}", flush=True)
        return

    async def ignore_update(_node: str, _values: dict) -> None:
        return None

    understood = await executor.understand_document(prepared, ignore_update)
    context = await executor.classify(understood)
    outcomes = await asyncio.gather(
        executor.extract_core(context),
        executor.extract_clause(context),
        executor.prepare_retrieval_view(context),
        vectorize_processed_pdf({"prepared_pdf": prepared}),
        return_exceptions=True,
    )
    core, clauses, retrieval, visual_vector = outcomes
    if isinstance(core, BaseException):
        raise RuntimeError(f"Core 提取失败：{core}") from core
    if isinstance(retrieval, BaseException):
        raise RuntimeError(f"检索向量生成失败：{retrieval}") from retrieval
    if isinstance(visual_vector, BaseException):
        raise RuntimeError(f"页面融合向量生成失败：{visual_vector}") from visual_vector
    page_fusion_vector = visual_vector["page_fusion_vector"].vector
    if retrieval.vector.vector is None:
        raise RuntimeError("正式检索问题流程没有形成 question_fusion 向量")
    if isinstance(clauses, BaseException):
        raise RuntimeError(f"条款提取失败：{clauses}") from clauses
    if clauses.status == "failed":
        raise RuntimeError("条款提取返回 failed 状态")
    projected_clauses = project_clauses(clauses)
    if not projected_clauses:
        raise RuntimeError("条款提取没有形成任何可入库条款")

    categories = [
        {
            "code": match.decision.category_code,
            "name": match.decision.category_name,
            "scenario": match.decision.scenario,
        }
        for match in context.classification.matches
    ]
    classification: dict[str, object] = {"categories": categories}
    if not categories and context.classification.unmapped_type_description:
        classification["unmapped_type_description"] = (
            context.classification.unmapped_type_description
        )
    document = {
        "document_id": prepared.document_id,
        "file_name": path.name,
        "file_uri": f"/{processed_path.name}",
        "page_count": prepared.page_count,
        "ingestion": {
            "reviewer": reviewer,
            "ingested_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        },
        "classification": classification,
        "core": project_core(core, field_catalog),
        "clauses": projected_clauses,
        "vectors": {
            "question_fusion": list(retrieval.vector.vector),
            "page_fusion": list(page_fusion_vector),
        },
    }
    response = await elasticsearch.index(
        index=index_name,
        id=prepared.document_id,
        document=document,
        refresh="wait_for",
    )
    print(
        f"{path.name}: {response['result']} id={prepared.document_id} "
        f"elapsed={perf_counter() - started:.1f}s",
        flush=True,
    )


async def main() -> None:
    args = parse_args()
    if not args.reviewer.strip():
        raise ValueError("审核人不能为空")
    paths = (
        (args.file.resolve(),)
        if args.file is not None
        else tuple(sorted(args.input_dir.glob("*.pdf")))
    )
    if not paths:
        raise FileNotFoundError(f"没有找到 PDF：{args.input_dir}")

    settings = get_settings()
    category_catalog = load_contract_category_catalog(settings.contract_category_definition_path)
    field_catalog = load_field_definition_catalog(settings.field_definition_path)
    retrieval_catalog = load_retrieval_view_guide_catalog(
        settings.retrieval_view_guide_path,
        known_category_codes={item.definition.code for item in category_catalog.categories},
    )
    executor = AgentContractExtractionExecutor(
        category_catalog=category_catalog,
        field_catalog=field_catalog,
        retrieval_guide_catalog=retrieval_catalog,
    )
    preparation = AsyncPDFPreparationService(settings.mllm)
    elasticsearch = create_elasticsearch_client(settings)
    index_name = args.index or settings.elasticsearch_index_name
    processed_dir = args.processed_dir.resolve()
    processed_dir.mkdir(exist_ok=True)
    try:
        if index_name == settings.elasticsearch_index_name:
            await synchronize_contract_index(elasticsearch, settings, field_catalog)
        for path in paths:
            try:
                await extract_and_index(
                    path,
                    executor=executor,
                    preparation=preparation,
                    field_catalog=field_catalog,
                    elasticsearch=elasticsearch,
                    index_name=index_name,
                    reviewer=args.reviewer.strip(),
                    processed_dir=processed_dir,
                    overwrite=args.overwrite,
                )
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                    raise
                print(f"{path.name}: failed: {type(error).__name__}: {error}", flush=True)
    finally:
        await elasticsearch.close()


if __name__ == "__main__":
    asyncio.run(main())
