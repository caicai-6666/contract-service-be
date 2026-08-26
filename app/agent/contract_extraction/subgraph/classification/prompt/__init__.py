"""合同分类子图独占的提示词入口。"""

from app.agent.contract_extraction.subgraph.classification.prompt.category import (
    CLASSIFICATION_CATEGORY_PROMPT_VERSION,
    build_category_judgment_messages,
    render_category_judgment_task,
)
from app.agent.contract_extraction.subgraph.classification.prompt.common import (
    CLASSIFICATION_COMMON_PROMPT_VERSION,
    build_classification_common_messages,
)
from app.agent.contract_extraction.subgraph.classification.prompt.prefill import (
    append_classification_prefill_task,
)
from app.agent.contract_extraction.subgraph.classification.prompt.unmapped import (
    UNMAPPED_TYPE_DESCRIPTION_PROMPT_VERSION,
    append_unmapped_type_description_task,
)

__all__ = [
    "CLASSIFICATION_CATEGORY_PROMPT_VERSION",
    "CLASSIFICATION_COMMON_PROMPT_VERSION",
    "UNMAPPED_TYPE_DESCRIPTION_PROMPT_VERSION",
    "append_classification_prefill_task",
    "append_unmapped_type_description_task",
    "build_category_judgment_messages",
    "build_classification_common_messages",
    "render_category_judgment_task",
]
