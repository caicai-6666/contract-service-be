"""压缩期间统一的容量反馈；展示百分比不参与阈值判断。"""


def build_compression_capacity_feedback(current_tokens: int, token_budget: int) -> str:
    """未达标强调目标，达标后建议按内容情况完成；不自动结束循环。"""
    if type(current_tokens) is not int or current_tokens < 0 or type(token_budget) is not int or token_budget <= 0:
        raise ValueError('工作区计数和预算无效')
    usage = f'当前使用量：{current_tokens}/{token_budget} token（{current_tokens / token_budget:.1%}）。'
    if current_tokens * 10 >= token_budget * 7:
        target = (token_budget * 7 - 1) // 10
        return usage + f'尚未达到压缩目标，请继续整理至低于 70%（最多 {target} token）；暂不能调用 finish_compression 完成。'
    return usage + '目前容量已经安全，已低于 70%。建议根据实际内容情况结束压缩：确认关键事实、来源和保护项保留后，调用 finish_compression；仅在确有必要时继续整理，不必为了更低使用率过度压缩。'
