"""首次打开网页：HTTP、离线正文提取与基于关注重点的模型精炼。"""
import asyncio
from contextlib import AsyncExitStack
from copy import deepcopy
from datetime import datetime, timezone
import logging
import httpx
import lxml.html
import trafilatura

logger = logging.getLogger(__name__)


def _error(stage, code, message):
    return {'error_code': code, 'failed_stage': stage, 'hint': message}

from app.core.config import get_settings
from app.infrastructure.mllm import MLLMClient
from app.infrastructure.model_json import load_model_json, validate_model_payload
from .schema import WebPageRequest, WebPageResult, WebPageRefinement
from .prompt import PROMPT_VERSION, build_refinement_messages
from .state import WebPageState



async def fetch_html(state: WebPageState, *, timeout_seconds=30, max_bytes=3 * 1024 * 1024,
                     max_redirects=5, transport=None) -> dict:
    """流式读取解压后的 HTML；超时覆盖重定向和整个读取过程，不发布半截正文。"""
    request = WebPageRequest.model_validate(state['request'])
    metadata = {'request': request}
    try:
        async with asyncio.timeout(timeout_seconds):
            # 每次独立客户端，不携带登录凭据，也不跨网页保存 Cookie。
            async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True,
                                         max_redirects=max_redirects, transport=transport) as client:
                async with client.stream('GET', str(request.url), headers={
                    'Accept': 'text/html,application/xhtml+xml',
                }) as response:
                    metadata.update(final_url=str(response.url), fetched_at=datetime.now(timezone.utc).isoformat())
                    response.raise_for_status()
                    media = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                    if media and media not in ('text/html', 'application/xhtml+xml'):
                        return {**metadata, **_error('fetch_html', 'unsupported_content_type',
                                '链接未返回 HTML 网页，请选择网页正文链接。')}
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            return {**metadata, **_error('fetch_html', 'response_too_large',
                                    '网页内容超过读取上限，请选择更精简的页面。')}
                        chunks.append(chunk)
                    html = b''.join(chunks)
                    if not html.strip():
                        return {**metadata, **_error('fetch_html', 'empty_response', '网页返回空内容，请选择其他来源。')}
                    return {**metadata, 'html': html, 'error_code': None, 'failed_stage': None, 'hint': None}
    except (TimeoutError, httpx.TimeoutException):
        return {**metadata, **_error('fetch_html', 'http_timeout', '网页读取超时，请稍后重试或选择其他来源。')}
    except httpx.HTTPStatusError as exc:
        return {**metadata, **_error('fetch_html', 'http_status_error',
                f'网页服务器返回 HTTP {exc.response.status_code}，未取得正文，请选择其他来源。')}
    except httpx.TooManyRedirects:
        return {**metadata, **_error('fetch_html', 'too_many_redirects', '网页重定向次数过多，无法读取正文。')}
    except httpx.RequestError:
        logger.exception('网页 HTTP 读取失败')
        return {**metadata, **_error('fetch_html', 'http_request_failed', '网页网络请求失败，请稍后重试或选择其他来源。')}


def _extract(html, url):
    # 保留原始字节，让解析器依据 HTML 编码声明识别中文，避免固定 UTF-8 误解码。
    text = trafilatura.extract(html, url=url, output_format='markdown', include_links=True,
                               include_tables=True, include_comments=False)
    root = lxml.html.fromstring(html)
    for element in root.xpath('//script|//style|//noscript'):
        element.drop_tree()
    visible = ' '.join(root.text_content().split())
    # 只保留页面真实出现的限制附近原文；登录入口本身不作为熔断依据。
    clues = []
    for marker in ('登录', '登陆', '验证码', '访问受限', '权限', '订阅', 'sign in', 'log in', 'subscribe', 'captcha'):
        index = visible.lower().find(marker)
        if index >= 0:
            fragment = visible[max(0, index - 60):index + 160]
            if fragment not in clues:
                clues.append(fragment)
    return (text or '').strip(), '\n'.join(clues)


async def extract_text(state: WebPageState) -> dict:
    """离线提取正文及访问限制线索；不将非空文本误判为有效业务内容。"""
    if not state.get('html'):
        return _error('extract_text', 'empty_text', '页面没有可提取的正文，请选择其他来源。')
    try:
        text, access_hint = await asyncio.to_thread(_extract, state['html'], state.get('final_url'))
    except Exception:
        logger.exception('网页正文提取失败')
        return _error('extract_text', 'extraction_failed', '网页正文解析失败，请选择其他来源。')
    if not text or not any(char.isalnum() for char in text):
        return {**_error('extract_text', 'empty_text', '页面没有可提取的正文，可能需要登录或动态加载，请选择其他来源。'),
                'access_hint': access_hint}
    return {'extracted_text': text, 'access_hint': access_hint,
            'error_code': None, 'failed_stage': None, 'hint': None}


async def refine_content(state: WebPageState, *, settings=None, client=None, audit=None, max_attempts=3) -> dict:
    """普通生成后本地验证；纠错只追加最小反馈，原始响应保留在宿主私有审计。"""
    request = WebPageRequest.model_validate(state['request'])
    text = state.get('extracted_text', '')
    if not text.strip():
        return _error('refine_content', 'empty_text', '没有可供精炼的网页正文。')
    settings = settings or get_settings()
    records = audit if audit is not None else []
    messages = build_refinement_messages(text=text, focus=request.focus)
    try:
        async with AsyncExitStack() as stack:
            model = client if client is not None else await stack.enter_async_context(MLLMClient(settings.mllm))
            for attempt in range(1, max_attempts + 1):
                response = await model.create_chat_completion(
                    messages=deepcopy(messages),
                    max_completion_tokens=settings.mllm.generation.max_completion_tokens,
                    temperature=settings.mllm.generation.temperature,
                    enable_thinking=settings.mllm.generation.enable_thinking)
                event = {'stage': 'refine_content', 'prompt_version': PROMPT_VERSION, 'attempt': attempt,
                         'response': response.raw_response, 'accepted': False}
                records.append(event)
                try:
                    if response.finish_reason != 'stop' or response.has_tool_calls or response.refusal:
                        raise ValueError('必须正常结束并返回完整 JSON，不能包含工具调用或拒答标记')
                    output = validate_model_payload(WebPageRefinement, load_model_json(response.content))
                except (ValueError, TypeError) as exc:
                    # 不把失败的正文、推断或完整响应反灌到下一轮；私有审计完整保留。
                    event['validation_error'] = str(exc)
                    messages.append({'role': 'user', 'content':
                        '上一轮输出未通过本地校验，不能作为结果。请按 Schema 输出完整 JSON，'
                        '字段不得缺失，evidence 为字符串列表，reasoning 和 result 为非空文字，'
                        'type 仅允许 content/no_content。'})
                    continue
                del messages[2:]
                event.update(accepted=True, message_count_after_cleanup=len(messages),
                             validated_output=output.model_dump())
                if output.type == 'no_content':
                    return {**_error('refine_content', 'no_content', output.result),
                            'has_content': False, 'refined_content': ''}
                return {'has_content': True, 'refined_content': output.result,
                        'error_code': None, 'failed_stage': None, 'hint': None}
    except Exception as exc:
        records.append({'stage': 'refine_content', 'error_type': type(exc).__name__})
        logger.exception('网页内容精炼请求失败')
        return _error('refine_content', 'refinement_request_failed', '网页内容精炼服务暂时不可用，请稍后重试。')
    return _error('refine_content', 'invalid_model_output', '网页内容精炼未能生成有效结果，请重试；不能据此判断页面没有有效信息。')


def route_after_stage(state: WebPageState) -> str:
    """每阶段失败均交给统一收束节点，跳过余下提取步骤。"""
    return 'finalize' if state.get('error_code') else 'continue'


async def finalize(state: WebPageState) -> dict:
    """仅发布最终正文或明确错误，半成品 HTML 和提取文本不向外暴露。"""
    request = WebPageRequest.model_validate(state['request'])
    code, stage, hint = state.get('error_code'), state.get('failed_stage'), state.get('hint')
    content = (state.get('refined_content') or '').strip()
    if not code and (state.get('has_content') is not True or not content):
        code, stage, hint = 'no_content', 'finalize', '网页未取得可用正文，请选择其他来源。'
    return {'result': WebPageResult(
        status='error' if code else 'success', url=str(request.url),
        final_url=state.get('final_url'), fetched_at=state.get('fetched_at'),
        content=None if code else content, hint=hint, error_code=code, failed_stage=stage)}
