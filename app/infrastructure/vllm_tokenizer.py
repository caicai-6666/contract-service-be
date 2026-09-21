"""通过 vLLM 原生接口计数纯文本，不执行模型生成或下载 tokenizer。"""

from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.config import MLLMSettings
from app.infrastructure.model_concurrency import get_model_request_limiter


class TokenizationError(RuntimeError):
    """接口不可用或返回的 token 计数不可信。"""


def build_tokenize_url(base_url: str) -> str:
    """移除末尾 OpenAI /v1，保留反向代理前缀，不直接拼成 /v1/tokenize。"""
    parts = urlsplit(base_url)
    if parts.scheme not in {'http', 'https'} or not parts.netloc or parts.query or parts.fragment:
        raise ValueError('模型地址必须是不含查询参数和片段的 HTTP(S) 地址')
    path = parts.path.rstrip('/')
    if path.endswith('/v1'):
        path = path[:-3]
    return urlunsplit((parts.scheme, parts.netloc, path + '/tokenize', '', ''))


async def count_text_tokens(
    text: str, settings: MLLMSettings, *, transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """复用 MLLM 配置；连接按调用关闭，取消传播，不重试或回退字符估算。

    transport 仅供 HTTP 测试替换传输层。请求沿用全局 MLLM 并发额度，
    但不计入模型生成指标，避免将分词请求误报为推理。
    """
    return await _count_tokens({'prompt': text, 'add_special_tokens': False}, settings, transport=transport)


async def count_chat_tokens(messages, tools, settings: MLLMSettings, *, chat_template_kwargs=None, transport=None) -> int:
    """计数真正的聊天模板输入；工具布局和思考开关必须与生成请求一致。"""
    template_kwargs = dict(chat_template_kwargs or {})
    # 分词和生成必须使用同一档位，否则上下文容量计算会遗漏强度指令。
    template_kwargs.pop('reasoning_effort', None)
    template_kwargs.update(settings.thinking_template_kwargs(
        template_kwargs.get('enable_thinking', settings.generation.enable_thinking)))
    return await _count_tokens({'messages': messages, 'tools': tools,
        'add_generation_prompt': True, 'chat_template_kwargs': template_kwargs},
        settings, transport=transport)


async def _count_tokens(payload, settings, *, transport=None):
    if settings.provider != 'vllm':
        raise TokenizationError('当前模型服务不支持 vLLM 分词接口')
    url = build_tokenize_url(settings.base_url)
    headers = {'Authorization': f'Bearer {settings.api_key}'} if settings.api_key else {}
    try:
        async with get_model_request_limiter('mllm', settings.max_concurrent_requests):
            async with httpx.AsyncClient(
                timeout=settings.timeout_seconds, transport=transport, follow_redirects=False,
            ) as client:
                response = await client.post(url, headers=headers, json={'model': settings.model, **payload})
                response.raise_for_status()
                data = response.json()
        # bool、浮点和数字字符串均不接受；同时校验 token ID 数量以免误信异常响应。
        count = data.get('count') if isinstance(data, dict) else None
        tokens = data.get('tokens') if isinstance(data, dict) else None
        if (type(count) is not int or count < 0 or not isinstance(tokens, list)
                or len(tokens) != count or any(type(token) is not int or token < 0 for token in tokens)):
            raise TokenizationError('分词接口返回了无效的计数或 token 列表')
        return count
    except (httpx.HTTPError, ValueError) as exc:
        # 不向工具反馈传播服务响应正文、工作区原文、地址或认证信息。
        raise TokenizationError('vLLM 分词接口请求失败或响应无效') from exc
