"""DEPRECATED: v1 legacy. Use src/ (v2) for new work.

M8+M14: fact sheet 提取 + 上下文格式化

从 Writer 输出中提取关键信息，作为续集的"前情提要"基础。

Fact sheet JSON Schema:
{
    "session_id": str,
    "parent_session_id": str | None,
    "extracted_at": str (ISO),
    "title": str,
    "characters": [
        {"name": "K", "外貌": "...", "服装": "...", "性格": "...", "动机": "...", "弧光": "..."}
    ],
    "relationship_graph": "K ↔ 幽灵（追踪/反追踪）→ 可能成为盟友",
    "world": {"地点": "...", "时间": "...", "天气": "...", "时代": "...", "整体氛围": "..."},
    "core_props": ["全息蝴蝶", "AR 眼镜"],
    "tone": "紧张",
    "ending_state": "K 在雨中转身，发现数据幽灵已消失",
    "story_arc": ["S1: K 接到追踪任务发现妹妹线索", "S2: K 在 AR 眼镜中找到妹妹声纹...", ...]
}

设计原则：
1. **容错**：Writer 不一定写齐所有字段；缺失时返回空字符串/空列表，不抛异常
2. **复用**：解析 ## 段的逻辑与 workflow.py 一致
3. **可读**：format_fact_sheet_as_context 输出 Markdown 段落，直接注入 Writer prompt

M14 三层上下文（注入 Writer 时）：
- 继承层：characters / world / tone / relationship_graph / core_props（人设/世界观承袭）
- 累积层：story_arc（每集剧情摘要累积，不覆盖）
- 快照层：ending_state（最近一集结尾）
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any


# ============== 主入口 ==============

def extract_fact_sheet(
    writer_output: str,
    session_id: str,
    parent_session_id: str | None = None,
) -> dict[str, Any]:
    """从 Writer 输出中提取 fact sheet

    Args:
        writer_output: Writer 的完整 Markdown 输出（含【本角色锁定的全局约束】段）
        session_id: 当前 session_id
        parent_session_id: 父 session_id（续集时指向上一集；首集为 None）

    Returns:
        fact_sheet dict（缺字段时为空字符串/空列表，不抛异常）
    """
    sections = _parse_sections(writer_output)
    locked_section = sections.get("【本角色锁定的全局约束】", "")
    bullets = _parse_bullets(locked_section)
    title = _extract_title(sections)

    return {
        "session_id": session_id,
        "parent_session_id": parent_session_id,
        "extracted_at": datetime.now().isoformat(),
        "title": title,
        "characters": _parse_characters(bullets),
        "relationship_graph": bullets.get("角色关系", ""),
        "world": _parse_world(bullets),
        "core_props": _parse_core_props(bullets),
        "tone": bullets.get("核心情绪基调", ""),
        "ending_state": bullets.get("结局状态", ""),
        # M14: story_arc 由 update_fact_sheet_after_episode 累积追加，
        # extract_fact_sheet 单次调用返回空 list（首集）
        "story_arc": [],
    }


def format_fact_sheet_as_context(fact_sheet: dict[str, Any]) -> str:
    """把 fact_sheet 格式化为 ## 前情提要 markdown 段

    注入 Writer prompt 时使用。M14 三层格式：
    ```
    ## 前情提要

    **继承层**（人设/世界观 — 续集必须保持一致）
    ### 角色
    - K：...
    ### 角色关系
    K ↔ 幽灵：...
    ### 世界观
    - 地点：...
    ### 核心道具
    全息蝴蝶、AR 眼镜
    ### 整体调性
    紧张

    **累积层**（故事线 — 前 N 集关键事件累积，按时间顺序）
    - S1: K 接到追踪幽灵的任务...
    - S2: K 找到妹妹声纹但被幽灵抢先...
    - S3: K 与幽灵达成临时合作...

    **快照层**（当前态 — 最近一集结尾状态）
    ### 上集结局
    K 在雨中与幽灵握手
    ```

    没上一集时返回空字符串（不注入段）。

    Args:
        fact_sheet: extract_fact_sheet() 的输出

    Returns:
        Markdown 字符串（已含换行）；空 fact_sheet 返回 ""
    """
    if not fact_sheet:
        return ""

    parts: list[str] = ["## 前情提要"]
    title = fact_sheet.get("title", "")
    sid = fact_sheet.get("session_id", "?")
    parts.append(f"**上一集**：{title or '(无标题)'}（session_id: `{sid}`）")

    # ============== 继承层：人设/世界观承袭 ==============
    has_inherited = False
    inherited_lines: list[str] = ["\n### 继承层（人设/世界观 — 续集必须保持一致）"]

    chars = fact_sheet.get("characters", [])
    if chars:
        inherited_lines.append("\n#### 角色")
        for c in chars:
            inherited_lines.append(
                f"- **{c.get('name', '?')}**：外貌 {c.get('外貌', '?')}；"
                f"服装 {c.get('服装', '?')}；性格 {c.get('性格', '?')}；"
                f"动机 {c.get('动机', '?')}；弧光 {c.get('弧光', '?')}"
            )
        has_inherited = True

    rel = fact_sheet.get("relationship_graph", "")
    if rel:
        inherited_lines.append(f"\n#### 角色关系\n{rel}")
        has_inherited = True

    world = fact_sheet.get("world", {})
    if world:
        world_lines = [f"- **{k}**：{v}" for k, v in world.items() if v]
        if world_lines:
            inherited_lines.append("\n#### 世界观")
            inherited_lines.extend(world_lines)
            has_inherited = True

    props = fact_sheet.get("core_props", [])
    if props:
        inherited_lines.append(f"\n#### 核心道具\n{'、'.join(props)}")
        has_inherited = True

    tone = fact_sheet.get("tone", "")
    if tone:
        inherited_lines.append(f"\n#### 整体调性\n{tone}")
        has_inherited = True

    if has_inherited:
        parts.extend(inherited_lines)

    # ============== 累积层：故事线（M14 新增） ==============
    story_arc = fact_sheet.get("story_arc", [])
    if story_arc:
        parts.append("\n### 累积层（故事线 — 前 N 集关键事件累积，按时间顺序）")
        for i, entry in enumerate(story_arc, 1):
            # entry 格式: "S<N>: <摘要>" 或 "<摘要>"（兼容无前缀）
            parts.append(f"- {entry}")

    # ============== 快照层：当前态 ==============
    ending = fact_sheet.get("ending_state", "")
    if ending:
        parts.append(f"\n### 快照层（当前态 — 最近一集结尾）\n{ending}")

    return "\n".join(parts) + "\n"


# ============== M13：每集更新 fact_sheet ==============

# 跨集承袭字段：人设/世界观/调性。这些**不**被新集覆盖。
_INHERITED_FIELDS = [
    "characters",
    "world",
    "tone",
    "relationship_graph",
    "core_props",
    "title",
]


def update_fact_sheet_after_episode(
    old_fact_sheet: dict[str, Any] | None,
    new_writer_output: str,
    new_jimeng_prompt: str,
    episode_id: int,
    episode_summary: str = "",
) -> dict[str, Any]:
    """M13+M14：每集跑完 Step 2 后调，重新提取 fact_sheet 并更新。

    M13 关键不变量：
    - characters / world / tone / relationship_graph / core_props / title:
        **优先保留**旧 fact_sheet 的值（人设/世界观承袭），新 extract 没覆盖则沿用旧的
    - ending_state: 用本集新 writer_output 的结尾（剧情推进）
    - episode_id: 当前集数
    - extracted_at: 当前时间戳

    M14 新增：
    - story_arc: 累积追加本集 episode_summary（不覆盖）。如果传空字符串则不追加
        （保留旧的 story_arc，便于兼容 web_server 中可选传 summary）

    Args:
        old_fact_sheet: 上一集的 fact_sheet（None 表示首集）
        new_writer_output: 本集 Writer 产出的完整 markdown
        new_jimeng_prompt: 本集最终 jimeng prompt（M13 可选，备用）
        episode_id: 本集是第几集（1-indexed）
        episode_summary: M14 本集剧情摘要（~100 字），用于累积 story_arc

    Returns:
        更新后的 fact_sheet dict（兼容 extract_fact_sheet 字段 + story_arc）
    """
    # 1. 重新从本集 writer_output 提取（可能有新角色/新设定）
    new_fs = extract_fact_sheet(
        new_writer_output,
        session_id=str(episode_id),
        parent_session_id=str(episode_id - 1) if episode_id > 1 else None,
    )

    # 2. 跨集承袭字段：旧 fact_sheet 的非空值优先（人设承袭）
    if old_fact_sheet:
        for key in _INHERITED_FIELDS:
            old_val = old_fact_sheet.get(key)
            if _is_meaningful(old_val):
                new_fs[key] = old_val

    # 3. 强制更新集数 + 时间戳
    new_fs["episode_id"] = episode_id
    new_fs["extracted_at"] = datetime.now().isoformat()
    # ending_state 已由 extract_fact_sheet 从本集 writer_output 提取（最新的）

    # 4. M14: story_arc 累积追加
    new_fs["story_arc"] = _append_story_arc(
        old_fact_sheet.get("story_arc", []) if old_fact_sheet else [],
        episode_id=episode_id,
        episode_summary=episode_summary,
    )
    return new_fs


def _append_story_arc(
    old_story_arc: list[str],
    episode_id: int,
    episode_summary: str,
) -> list[str]:
    """M14 累积 story_arc：旧条目保留 + 本集摘要追加（带集数前缀）

    设计：
    - 每条格式："S{ep_id}: {summary}"（统一前缀，方便后续按集数定位）
    - 不覆盖：即使本集 summary 为空也保留旧条目
    - 自动去重：同 ep_id 的旧条目会被新条目替换（避免重写本集时重复）

    Args:
        old_story_arc: 旧的 story_arc list
        episode_id: 本集集数
        episode_summary: 本集剧情摘要（空字符串 → 不追加）

    Returns:
        新的 story_arc list（旧条目 + 本集追加）
    """
    if not episode_summary or not episode_summary.strip():
        return list(old_story_arc)  # 空 summary → 不追加，保持原状

    # 去掉属于本 ep_id 的旧条目（重写本集场景）+ 追加新条目
    cleaned = [e for e in old_story_arc if not _is_episode_entry(e, episode_id)]
    cleaned.append(f"S{episode_id}: {episode_summary.strip()}")
    return cleaned


def _is_episode_entry(entry: str, episode_id: int) -> bool:
    """判断 entry 是否是指定 ep_id 的旧条目（如 'S3: ...'）"""
    if not entry:
        return False
    return entry.startswith(f"S{episode_id}:")


def _is_meaningful(value: Any) -> bool:
    """判断值是否'非空'——用于跨集承袭判断"""
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    if isinstance(value, (list, dict)) and len(value) == 0:
        return False
    return True


# ============== 内部辅助：复用 workflow.py 的 section parser ==============

def _parse_sections(text: str) -> dict[str, str]:
    """解析 Markdown 文本的 ## 段。返回 {段名: 段体}"""
    sections: dict[str, str] = {}
    current_name: str | None = None
    current_lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            if current_name is not None:
                sections[current_name] = "\n".join(current_lines).strip()
            current_name = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_name is not None:
        sections[current_name] = "\n".join(current_lines).strip()
    return sections


def _extract_title(sections: dict[str, str]) -> str:
    """从 ## 脚本标题 段取首行非空内容"""
    title_section = sections.get("脚本标题", "")
    for line in title_section.split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


# ============== 内部辅助：bullet 解析 ==============

def _parse_bullets(locked_section: str) -> dict[str, str]:
    """解析【本角色锁定的全局约束】的 "- 字段名：值" 形式

    输入示例：
        - 角色外貌：K：短发...；幽灵：剪影...
        - 关键道具一致性：全息蝴蝶、AR 眼镜
        - 关键道具一致性（续）：AR 数据轨迹

    输出：{"角色外貌": "K：短发...；幽灵：剪影...", "关键道具一致性": "全息蝴蝶、AR 眼镜\n关键道具一致性（续）：AR 数据轨迹"}

    注意：同名 bullet 的后续行会合并到 value（用于"续"行）
    """
    bullets: dict[str, str] = {}
    current_key: str | None = None
    for line in locked_section.split("\n"):
        # 匹配 "- 字段名：值..."
        m = re.match(r"^\s*-\s+([^：:]+)[：:]\s*(.*)", line)
        if m:
            current_key = m.group(1).strip()
            bullets[current_key] = m.group(2).strip()
        elif line.strip().startswith("-") and current_key:
            # 续行（与上一行同 key，累积）
            bullets[current_key] += "\n" + line.strip()
    return bullets


def _parse_characters(bullets: dict[str, str]) -> list[dict[str, str]]:
    """从 bullets 拆出 characters 列表

    输入 bullets 格式（两种都支持）：
    - "K：短发..." （全角冒号，多用于外貌/服装）
    - "K 短发..."   （空格分隔，多用于性格/动机/弧光）

    示例输入：
    - 角色外貌：K：短发...；幽灵：剪影...
    - 角色性格：K 冷静...；幽灵 神秘...

    输出：[{"name": "K", "外貌": "短发", "服装": "黑色长风衣", ...}, ...]
    """
    fields = ("外貌", "服装", "性格", "动机", "弧光")
    char_data: dict[str, dict[str, str]] = {}
    for field in fields:
        bullet_key = f"角色{field}"
        if bullet_key not in bullets:
            continue
        entries = _split_by_semicolon(bullets[bullet_key])
        for entry in entries:
            name, value = _split_name_value(entry)
            if not name:
                continue
            if name not in char_data:
                char_data[name] = {}
            char_data[name][field] = value
    return [{"name": name, **data} for name, data in char_data.items()]


def _split_name_value(entry: str) -> tuple[str, str]:
    """从 "K：短发..." 或 "K 短发..." 拆出 (name, value)

    优先级：
    1. 全角冒号 "："（用于外貌/服装）
    2. 半角冒号 ":"
    3. 第一个空格（用于性格/动机/弧光）
    """
    if "：" in entry:
        name, value = entry.split("：", 1)
        return name.strip(), value.strip()
    if ":" in entry:
        name, value = entry.split(":", 1)
        return name.strip(), value.strip()
    # 空格分隔（性格/动机/弧光）：第一个空格
    if " " in entry:
        name, value = entry.split(" ", 1)
        return name.strip(), value.strip()
    return "", entry.strip()


def _split_by_semicolon(text: str) -> list[str]:
    """按 ； 或 ; 切分"""
    parts = re.split(r"[；;]", text)
    return [p.strip() for p in parts if p.strip()]


def _parse_world(bullets: dict[str, str]) -> dict[str, str]:
    """从 bullets 提取 world 字段（缺字段也保留 key，value 为 ""）

    映射：
    - 场景地点 → 地点
    - 场景时间 → 时间
    - 场景天气 → 天气
    - 时代 → 时代
    - 整体氛围 → 整体氛围
    """
    keys_map = {
        "场景地点": "地点",
        "场景时间": "时间",
        "场景天气": "天气",
        "时代": "时代",
        "整体氛围": "整体氛围",
    }
    world: dict[str, str] = {}
    for bullet_key, world_key in keys_map.items():
        world[world_key] = bullets.get(bullet_key, "")
    return world


def _parse_core_props(bullets: dict[str, str]) -> list[str]:
    """提取核心道具（去重保序）

    来源 bullet：
    - "关键道具一致性：全息蝴蝶、霓虹反射、AR 眼镜"
    - "核心道具：全息蝴蝶、AR 数据轨迹"

    按 "、""/""，""、""和"" 切分
    """
    raw_items: list[str] = []
    for key in ("关键道具一致性", "核心道具"):
        if key in bullets and bullets[key]:
            items = re.split(r"[、/，,]", bullets[key])
            for item in items:
                item = item.strip()
                if item and not item.startswith("（"):
                    raw_items.append(item)
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for p in raw_items:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique