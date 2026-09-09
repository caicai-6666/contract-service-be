"""将程序给出的纠错方向包装为独立 user 消息，不负责生成指引。"""


def build_system_guidence_message(guidence: str) -> dict[str, str]:
    """仅接收程序生成的可信指引，不直接包装用户输入或历史内容。"""
    if not isinstance(guidence, str):
        raise TypeError("system_guidence 必须是字符串")
    if not guidence.strip():
        raise ValueError("system_guidence 不能为空")
    # 保留指引的原有文字和分行，边界只用于醒目展示，不赋予额外角色权限。
    return {
        "role": "user",
        "content": (
            "================ system_guidence ================\n"
            "【系统流程指引】\n"
            "请遵循已有任务与工具规则，按以下指引修正下一步操作：\n\n"
            f"{guidence}\n\n"
            "============== end system_guidence =============="
        ),
    }
