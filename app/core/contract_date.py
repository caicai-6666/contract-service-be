"""合同日期的统一规范化规则。"""

from __future__ import annotations

import re
from datetime import date
from typing import Final


_CONTRACT_DATE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(?P<year>[0-9]{4})-(?P<month>[0-9]{1,2})-(?P<day>[0-9]{1,2})"
    ),
    re.compile(
        r"(?P<year>[0-9]{4})/(?P<month>[0-9]{1,2})/(?P<day>[0-9]{1,2})"
    ),
    re.compile(
        r"(?P<year>[0-9]{4})\.(?P<month>[0-9]{1,2})\.(?P<day>[0-9]{1,2})"
    ),
    re.compile(
        r"(?P<year>[0-9]{4})\s*年\s*"
        r"(?P<month>[0-9]{1,2})\s*月\s*"
        r"(?P<day>[0-9]{1,2})\s*日"
    ),
)


def normalize_contract_date(value: str) -> str:
    """将可确定的完整签约日期规范为 `YYYY-MM-DD`。"""
    normalized = value.strip()
    for pattern in _CONTRACT_DATE_PATTERNS:
        match = pattern.fullmatch(normalized)
        if match is None:
            continue
        try:
            return date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            ).isoformat()
        except ValueError as exc:
            raise ValueError(f"签约日期不是合法日期：{value}") from exc
    raise ValueError(
        "签约日期必须是完整的年月日，"
        "例如 2026-08-20 或 2026年8月20日"
    )


__all__ = ["normalize_contract_date"]
