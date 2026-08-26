"""最终公共前缀预热子图节点。"""

from datetime import UTC, datetime
from time import perf_counter

from app.agent.contract_extraction.context import (
    CONTRACT_PREFILL_CONTEXT_VERSION,
    build_contract_prefill_messages,
    context_sha256,
)
from app.agent.contract_extraction.state import (
    ContractPrefillContext,
    ContractPreheatResult,
)
from app.agent.contract_extraction.subgraph.classification.state import (
    ContractClassificationResult,
)
from app.agent.contract_extraction.subgraph.preheat.prompt import (
    append_prefill_task,
)
from app.agent.contract_extraction.subgraph.preheat.state import PreheatSubgraphState
from app.core.config import get_settings
from app.infrastructure.mllm import MLLMClient, MLLMUnavailableError


def assemble_prefill_context(
    state: PreheatSubgraphState,
) -> PreheatSubgraphState:
    """在基础前缀末尾追加分类结果，形成最终下游公共前缀。"""
    base_context = state["base_context"]
    classification = state["classification"]
    if not isinstance(classification, ContractClassificationResult):
        raise TypeError(
            "classification 必须是 ContractClassificationResult，"
            "不能将占位对象或分类运行审计写入最终公共前缀"
        )
    if classification.document_id != base_context.document_id:
        raise ValueError("分类结果与基础前缀的 document_id 不一致")

    messages = build_contract_prefill_messages(
        base_context.messages,
        classification,
    )
    return {
        "prefill_context": ContractPrefillContext(
            document_id=base_context.document_id,
            prompt_version=CONTRACT_PREFILL_CONTEXT_VERSION,
            messages=tuple(messages),
            prefix_sha256=context_sha256(messages),
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
