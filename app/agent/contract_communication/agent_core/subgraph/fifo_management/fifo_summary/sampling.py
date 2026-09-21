"""两个摘要节点共用Qwen3.8-Flash-Next官方推理采样参数。"""


def thinking_sampling() -> dict:
    """返回独立参数副本；输出额度和seed仍由应用配置控制。"""
    return {
        'temperature': 1.0, 'top_p': 0.95, 'top_k': 20, 'min_p': 0.0,
        'presence_penalty': 0.0, 'repetition_penalty': 1.0,
    }
