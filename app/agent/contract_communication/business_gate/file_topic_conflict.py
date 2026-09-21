"""摘要主题冲突检查：逐文件判断、确定性熔断和可供拒绝回复消费的提示。"""

import asyncio
from functools import partial

from app.core.config import get_settings
from app.infrastructure.mllm import MLLMClient
from .node import _inspect_relevance_json, _run_file_relevance_jobs, route_relevance_checks
from .prompt.file_topic_conflict import (
    FILE_TOPIC_CONFLICT_PROMPT_VERSION, build_file_topic_conflict_messages,
)
from .schema import (FileTopicConflictGeneration, FileTopicConflictValidationError,
    validate_file_topic_conflict, build_file_topic_conflict_validation_feedback)
from .state import (BusinessGateSubgraphState, FileSummary, FileTopicConflictResult, FileTopicConflictFeedback,
                    FileTopicConflictIssue)


async def inspect_file_topic_conflict(file, *, settings, max_attempts=3, client_factory=MLLMClient):
    """复用统一 JSON 接口及有限纠错，失败轨迹仅留私有审计。"""
    return await _inspect_relevance_json(
        identity={'file_index': file.file_index, 'file_name': file.original_file_name},
        settings=settings, max_attempts=max_attempts, client_factory=client_factory,
        messages=build_file_topic_conflict_messages(display_name=file.display_name, summary=file.summary),
        generation_type=FileTopicConflictGeneration, result_type=FileTopicConflictResult,
        schema_name='file_topic_conflict', prompt_version=FILE_TOPIC_CONFLICT_PROMPT_VERSION,
        validate=validate_file_topic_conflict, error_type=FileTopicConflictValidationError,
        build_feedback=build_file_topic_conflict_validation_feedback)


def collect_file_topic_conflict(results):
    """任一已确认冲突即拒绝；未命中但存在执行失败时按故障拒绝，不默认放行。"""
    hits = tuple(row for row in results if row.status == 'completed' and row.result is True)
    failed = any(row.status == 'failed' for row in results)
    feedback = None
    status = 'not_implemented'
    decision = False if results else 'skipped'
    if hits:
        status, decision = 'rejected', True
        feedback = FileTopicConflictFeedback(
            hint='已确认以下文件包含显著偏离主体用途的独立无关内容，本轮停止处理，尚未开展相关性评估或业务分析。'
                 '请依据对应文件摘要中的具体内容和已有页码定位问题，不将整份文件称为无关，不推断恶意。',
            issues=tuple(FileTopicConflictIssue(file_index=row.file_index + 1, file_name=row.file_name,
                hint='该文件摘要披露了与主体用途无关的独立内容。请根据对应摘要说明具体部分；'
                     '调整该部分并保留完整业务正文和必要附件后重新提交。') for row in hits))
    elif failed:
        status, decision = 'failed', 'failed'
        feedback = FileTopicConflictFeedback(
            hint='文件主题冲突检查未能全部完成，尚未取得完整判断；这不代表文件存在主题冲突，请稍后重试。')
    return {'status': status, 'file_topic_conflict': decision,
            'file_topic_conflict_results': results, 'file_topic_conflict_feedback': feedback,
            'relevance_score': None, 'relevance_threshold': None,
            'user_hints': (feedback.hint,) if feedback else ()}


async def check_file_topic_conflict_async(state, *, settings=None, max_concurrency=4,
                                         max_attempts=3, client_factory=MLLMClient):
    """接收已验证身份的轻量摘要；每文件独立上下文，有界并发后统一收束。"""
    if type(max_concurrency) is not int or max_concurrency < 1:
        raise ValueError('max_concurrency 必须是正整数')
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError('max_attempts 必须为1至3的整数')
    try:
        files = tuple(sorted((FileSummary.model_validate(f) for f in state.get('file_summaries', ())),
                             key=lambda f: f.file_index))
        if len({f.file_index for f in files}) != len(files):
            raise ValueError('文件下标不能重复')
    except (ValueError, TypeError):
        return {'status': 'failed', 'file_topic_conflict': 'failed', 'file_topic_conflict_results': (),
                'file_topic_conflict_feedback': FileTopicConflictFeedback(
                    hint='文件摘要信息校验未能完成，无法继续主题冲突检查；这不代表文件内容存在问题。'),
                'relevance_score': None, 'relevance_threshold': None, 'user_hints': ()}
    results = await _run_file_relevance_jobs(files, max_concurrency=max_concurrency,
        inspect=partial(inspect_file_topic_conflict, settings=settings or get_settings().mllm,
                        max_attempts=max_attempts, client_factory=client_factory))
    return collect_file_topic_conflict(results)


def check_file_topic_conflict(state, **kwargs):
    return asyncio.run(check_file_topic_conflict_async(state, **kwargs))


def route_after_gate_initialization(state: BusinessGateSubgraphState):
    # 初始化先验证摘要完整性和原文件绑定；无附件不调用主题冲突模型。
    if state.get('status') not in {'failed', 'rejected'} and state.get('file_summaries'):
        return ['check_file_topic_conflict']
    return route_relevance_checks(state)


def route_after_file_topic_conflict(state: BusinessGateSubgraphState):
    if state.get('status') in {'failed', 'rejected'}:
        return ['reject_request']
    return route_relevance_checks(state)
