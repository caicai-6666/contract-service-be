"""审核文件等级的严格整数校验及无损 SQLite 编解码。"""


def validate_file_level(value: int | None) -> int | None:
    if value is not None and type(value) is not int:
        raise ValueError('文件等级必须为严格整数，历史未知等级可为空')
    return value


def encode_file_level(value: int | None) -> str | None:
    # 上游不限制整数范围；以十进制 TEXT 保存，避免 SQLite 64 位整数溢出或转 REAL 丢精度。
    value = validate_file_level(value)
    return None if value is None else str(value)


def decode_file_level(value: str | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('已保存的文件等级不是十进制文本')
    parsed = int(value)
    if str(parsed) != value:
        raise ValueError('已保存的文件等级格式不规范')
    return parsed
