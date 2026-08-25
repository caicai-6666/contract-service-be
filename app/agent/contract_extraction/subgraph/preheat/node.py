"""下游公共前缀预热子图节点。"""

from datetime import UTC, datetime
from time import perf_counter

from app.agent.contract_extraction.state import (
    ContractPrefillContext,
    ContractPreheatResult,
)
from app.agent.contract_extraction.subgraph.preheat.prompt import (
    CONTRACT_PREFILL_PROMPT_VERSION,
    append_prefill_task,
    build_contract_prefill_messages,
    contract_prefill_sha256,
)
from app.agent.contract_extraction.subgraph.preheat.state import PreheatSubgraphState
from app.core.config import get_settings
from app.infrastructure.mllm import MLLMClient, MLLMUnavailableError


def assemble_prefill_context(
    state: PreheatSubgraphState,
) -> PreheatSubgraphState:
    """组装“PDF 公共前缀 + 权威文档结构”的下游公共前缀。"""
    prepared_pdf = state["prepared_pdf"]
    structure = state["document_structure"]
    if structure.document_id != prepared_pdf.document_id:
        raise ValueError("文档结构与 PreparedPDF 的 document_id 不一致")

    messages = build_contract_prefill_messages(
        prepared_pdf.pages,
        state["prompt_context"].pages,
        structure,
    )
    return {
        "prefill_context": ContractPrefillContext(
            document_id=prepared_pdf.document_id,
            prompt_version=CONTRACT_PREFILL_PROMPT_VERSION,
            messages=tuple(messages),
            prefix_sha256=contract_prefill_sha256(messages),
        )
    }


async def prefill_contract_context(
    state: PreheatSubgraphState,
) -> PreheatSubgraphState:
    """向本地 vLLM 发送单 token 请求，预热完整下游公共前缀。"""
    context = state["prefill_context"]
    settings = get_settings().mllm
    started_at = perf_counter()

    async with MLLMClient(settings) as client:
        try:
            completion = await client.create_chat_completion(
                messages=append_prefill_task(context.messages),
                max_completion_tokens=1,
                min_tokens=1,
                temperature=0,
                enable_thinking=False,
            )
        except MLLMUnavailableError as exc:
            return {
                "preheat": ContractPreheatResult(
                    status="degraded",
                    document_id=context.document_id,
                    prompt_version=context.prompt_version,
                    model=settings.model,
                    completed_at=datetime.now(UTC),
                    prefix_sha256=context.prefix_sha256,
                    elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
                    error=str(exc),
                )
            }

    return {
        "preheat": ContractPreheatResult(
            status="warmed",
            document_id=context.document_id,
            prompt_version=context.prompt_version,
            model=completion.model or settings.model,
            completed_at=datetime.now(UTC),
            prefix_sha256=context.prefix_sha256,
            elapsed_ms=round((perf_counter() - started_at) * 1000, 3),
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            cached_tokens=completion.cached_tokens,
        )
    }
