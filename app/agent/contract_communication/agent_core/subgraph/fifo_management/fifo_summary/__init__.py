"""FIFO 按主题自动摘要子图骨架，主题规划与单主题生成已接入模型。"""
from .schema import SummaryTopic, TopicPlan, SummaryItem, TopicSummary, TopicGenerationRequest, TopicGenerationOutput, TopicSummaryContent, TopicMapResult, FIFOTopicSummary, FIFOSummaryResult
from .planner import run_topic_planner, build_topic_planning_tools
from .workflow import build_fifo_summary_subgraph

__all__ = ['run_topic_planner', 'build_topic_planning_tools', 'SummaryTopic', 'TopicPlan', 'SummaryItem', 'TopicSummary', 'TopicGenerationRequest',
           'TopicSummaryContent', 'run_topic_generator', 'TopicGenerationOutput', 'TopicMapResult', 'FIFOTopicSummary', 'FIFOSummaryResult', 'build_fifo_summary_subgraph']

from .prompt import TOPIC_GENERATION_PROMPT, TOPIC_GENERATION_PROMPT_VERSION

__all__ += ['TOPIC_GENERATION_PROMPT', 'TOPIC_GENERATION_PROMPT_VERSION']

from .generator import run_topic_generator
