"""问题提出子图的稳定提示词组装入口。"""

from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.prompt.context import (
    QUESTION_GENERATION_CONTEXT_VERSION,
    build_question_generation_messages,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.prompt.embedding import (
    CONTRACT_QUESTION_EMBEDDING_INSTRUCTION,
    RETRIEVAL_EMBEDDING_PROMPT_VERSION,
    USER_QUERY_EMBEDDING_INSTRUCTION,
    render_contract_question_embedding_input,
)
from app.agent.contract_extraction.subgraph.retrieval_view_generation.question_generation.prompt.proposal import (
    QUESTION_PLAN_BEGIN,
    QUESTION_PLAN_COMMENTS,
    QUESTION_PLAN_END,
    QUESTION_PROPOSAL_COMMON_PROMPT_VERSION,
    QUESTION_PROPOSAL_COMMON_TASK,
    QUESTION_PROPOSAL_TARGET_PROMPT_VERSION,
    QUESTION_PROPOSAL_TOOL_PLACEMENT,
    append_question_plan_target,
    build_question_proposal_common_messages,
)

__all__ = [
    "CONTRACT_QUESTION_EMBEDDING_INSTRUCTION",
    "QUESTION_GENERATION_CONTEXT_VERSION",
    "QUESTION_PLAN_BEGIN",
    "QUESTION_PLAN_COMMENTS",
    "QUESTION_PLAN_END",
    "QUESTION_PROPOSAL_COMMON_PROMPT_VERSION",
    "QUESTION_PROPOSAL_COMMON_TASK",
    "QUESTION_PROPOSAL_TARGET_PROMPT_VERSION",
    "QUESTION_PROPOSAL_TOOL_PLACEMENT",
    "RETRIEVAL_EMBEDDING_PROMPT_VERSION",
    "USER_QUERY_EMBEDDING_INSTRUCTION",
    "append_question_plan_target",
    "build_question_generation_messages",
    "build_question_proposal_common_messages",
    "render_contract_question_embedding_input",
]
