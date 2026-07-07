"""DEPRECATED: v1 legacy. Use src/ (v2) for new work.

格式校验：检查角色输出是否包含必要 Markdown 段 + 关键结构（M3.4）

校验在 `team.run()` 完成后做，不打断 GroupChat 内部流程。
失败时由 `run_with_validation()` 决定重试或 fallback。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import UserInput


# 每个角色必须输出的 Markdown 段（精确匹配 `## xxx` 子串）
REQUIRED_SECTIONS: dict[str, list[str]] = {
    "Writer": [
        "## 故事梗概",
        "## 场景列表",
        "## 人物",
        "## 场景设定",
        "## 【本角色锁定的全局约束】",
    ],
    "Storyboard": [
        "## 镜头序列",
        "## 详细说明",
        "## 【本角色锁定的全局约束】",
    ],
    "DP": [
        "## 统一规格",
        "## 摄影参数（逐镜头）",
        "## 【本角色锁定的全局约束】",
        "## 【本角色声明的禁止项】",
    ],
    "Director": [
        "## 导演决策",
        "## 一致性检查报告",
        "## 最终脚本（按镜头组织）",
    ],
}


def validate_output(agent_name: str, text: str, user: "UserInput | None") -> tuple[bool, list[str]]:
    """检查角色输出是否符合格式要求

    Args:
        agent_name: 角色名（Writer/Storyboard/DP/Director）
        text: 角色输出文本
        user: 用户输入（用于结构校验；None 时跳过结构校验，只做段落存在性检查）

    Returns:
        (是否通过, 错误列表)。错误列表为空时表示通过。
    """
    errors: list[str] = []

    # 1. 段落存在性
    for sec in REQUIRED_SECTIONS.get(agent_name, []):
        if sec not in text:
            errors.append(f"缺少段落：{sec}")

    # 2. 结构校验（Storyboard：镜头数 = 用户指定）— 需要 user
    if agent_name == "Storyboard" and user is not None and user.shot_count:
        rows = _count_storyboard_rows(text)
        if rows != user.shot_count:
            errors.append(
                f"镜头表行数 {rows} ≠ 用户指定 {user.shot_count}"
            )

    # 3. 结构校验（Storyboard：时长加总 = 用户指定）— 需要 user
    if agent_name == "Storyboard" and user is not None and user.duration:
        total = _sum_storyboard_durations(text)
        if total > 0 and abs(total - user.duration) > 0.5:
            errors.append(
                f"镜头时长加总 {total}s ≠ 用户指定 {user.duration}s"
            )

    return len(errors) == 0, errors


def _count_storyboard_rows(text: str) -> int:
    """统计镜头表的行数（`| 1 | 0-5秒 | ...` 这种数据行，不含表头/分隔）

    从 `## 镜头序列` 段开始统计；用 `\|.*\|.*\|` 匹配至少 3 列的行（数据行），
    排除表头（通常含"镜头ID"/"时长"/"景别"等中文）和分隔行（`|---|---|`）。
    """
    sec = _extract_section(text, "## 镜头序列")
    if not sec:
        return 0
    count = 0
    for line in sec.split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        if "---" in line:  # 分隔行
            continue
        if "镜头ID" in line or "时长" in line:  # 表头
            continue
        # 数据行：至少有 2 个 | 分隔（即 3 列）
        if line.count("|") >= 3:
            count += 1
    return count


def _sum_storyboard_durations(text: str) -> float:
    """统计镜头表中的时长加总（秒）

    - 匹配 "0-5秒" / "5-10秒" 区间：duration = end - start
    - 退路：单数字 "5秒"（逐镜头时长行）
    """
    sec = _extract_section(text, "## 镜头序列")
    if not sec:
        return 0.0
    total = 0.0
    has_range = False
    # 匹配 "X-Y秒" 区间：duration = end - start
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*[s秒]", sec):
        start, end = float(m.group(1)), float(m.group(2))
        total += end - start
        has_range = True
    if has_range:
        return total
    # 退路：单数字 "5秒"（逐镜头时长行）
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[s秒]", sec):
        total += float(m.group(1))
    return total


def _extract_section(text: str, header: str) -> str:
    """提取 `## xxx` 段的 body（不含标题行）"""
    idx = text.find(header)
    if idx < 0:
        return ""
    # 从 header 之后开始，到下一个 ## 之前
    rest = text[idx + len(header):]
    # 找到下一个 ## 开头
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    if nxt:
        return rest[: nxt.start()]
    return rest


def format_feedback(failures: list[tuple[str, list[str]]]) -> str:
    """把校验失败列表格式化为 feedback 消息（注入到 task message 头部）

    Args:
        failures: [(角色名, 错误列表), ...]

    Returns:
        feedback 字符串（首行带 `[系统]` 前缀）
    """
    lines = ["[系统] 上次运行存在以下问题，请本次严格按格式修正：\n"]
    lines.append("要求：只补缺失/错误的字段，不要重写其他内容。\n")
    for src, errs in failures:
        lines.append(f"\n### {src}")
        for e in errs:
            lines.append(f"- {e}")
    return "\n".join(lines) + "\n\n---\n\n"
