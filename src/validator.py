"""M22 v2 兼容：把 v1_legacy/validator 的纯函数 re-export。

避免 v2 (src/app.py) 直接 from src.v1_legacy 引入。
实现仍在 src/v1_legacy/validator.py（M3.4 的纯函数逻辑不变）。
"""
from .v1_legacy.validator import (  # noqa: F401
    REQUIRED_SECTIONS,
    validate_output,
    format_feedback,
    _count_storyboard_rows,  # noqa: F401
    _sum_storyboard_durations,  # noqa: F401
    _extract_section,  # noqa: F401
)
