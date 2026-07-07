"""DEPRECATED: v1 legacy. Use src/ (v2) for new work.

主工作流：构造 task 消息 + 跑 GroupChat + 提取结果"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from autogen_agentchat.teams import SelectorGroupChat
from autogen_agentchat.ui import Console

from .config import UserInput
from .roles import build_all_agents
from .team import build_team, run_with_validation


def build_task_message(
    user: UserInput,
    fact_sheet: dict | None = None,
    current_inspiration: str = "",
) -> str:
    """构造初始任务消息（Host 看到的）

    用户硬参数分四组展示：创意 / 视觉 / 故事 / 音频

    Args:
        user: 用户输入
        fact_sheet: M8 续集时传入上一集的 fact_sheet（如有，会在顶部注入 `## 前情提要` 段）
        current_inspiration: M13 当前集的灵感（每集覆盖式，不累积）
    """
    def _or(value, default: str) -> str:
        if value is None or value == "" or value == []:
            return default
        return str(value)

    parts: list[str] = []

    # M8: 续集前情提要（如有）
    if fact_sheet:
        from .fact_sheet import format_fact_sheet_as_context
        context_md = format_fact_sheet_as_context(fact_sheet)
        if context_md:
            parts.append(context_md.rstrip("\n"))
            parts.append("")

    # M13: 本集灵感（每集 1 段，覆盖式，不入 list）
    if current_inspiration:
        parts.append(f"[本集灵感] {current_inspiration}")
        parts.append("")

    parts.extend([
        f"[用户灵感] {user.inspiration}",
        "",
        "[用户硬参数 / 创意]",
        f"- 总时长：{user.duration} 秒",
        f"- 画幅：{user.aspect_ratio}",
        f"- 镜头数：{_or(user.shot_count, '由分镜师决定')}",
        f"- 风格偏好：{_or(user.style_hint, '由角色自由发挥')}",
        "",
        "[用户硬参数 / 视觉]",
        f"- 画质：{_or(user.quality, '由摄影指导决定')}",
        f"- 色调：{_or(user.color_tone, '由摄影指导决定')}",
        f"- 质感：{_or(user.texture, '由摄影指导决定')}",
        f"- 帧率：{_or(user.frame_rate, '由摄影指导决定')}",
        f"- 光影：{_or(user.lighting_mood, '由摄影指导决定')}",
        "",
        "[用户硬参数 / 故事]",
        f"- 情绪：{_or(user.mood, '由作家决定')}",
        f"- 人物：{_or(user.characters, '由作家决定')}",
        "",
        "[用户硬参数 / 音频]",
        f"- 配乐：{_or(user.music_hint, '由合成器决定')}",
        f"- 对白/旁白：{_or(user.narration, '由合成器决定')}",
        "",
        "[用户硬参数 / 硬约束]",
        f"- {_or(user.extra_constraints, '无')}",
        "",
        "[流程说明]",
        "请按 Host → Writer → Storyboard → DP → Director 顺序执行。",
        "Director 通过后请在输出末尾说 FINAL_APPROVED；2 轮后仍有问题请说 FORCE_OUTPUT。",
        "请 Host 先开始：解析参数并初始化全局约束收集器。",
    ])
    return "\n".join(parts)


def _msg_to_text(msg) -> str:
    """统一从消息中提取文本内容"""
    if hasattr(msg, "to_text"):
        try:
            return msg.to_text()
        except Exception:
            pass
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # content blocks（OpenAI 多模态格式）
        out = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                out.append(block["text"])
            elif isinstance(block, str):
                out.append(block)
        return "\n".join(out)
    return str(content)


def extract_script(messages, user: UserInput | None = None) -> str:
    """从消息流中拼装最终脚本（M3.2：Director 按镜头组织，代码包一层）

    顺序：
    1. 标题（Writer 的"## 脚本标题"） + 元数据行 + ⚠️ 警告（FORCE_OUTPUT）
    2. Director 的导演决策 / 一致性检查报告 / 全局约束
    3. DP 的统一规格 / 禁止项（跨镜头）
    4. Director 写的"## 最终脚本（按镜头组织）"——含整体设定 + 每镜头综合段

    fallback（M3.1）：Director 未输出 per-shot 段时，按角色机械拼装。

    Args:
        messages: GroupChat 消息列表
        user: 用户输入（用于元数据行；可选，不传则无 meta）

    Returns:
        完整最终脚本（Markdown），失败返回 ""
    """
    if user is None:
        user = UserInput(inspiration="未命名")
    return build_final_script(messages, user)


def _is_garbage_output(text: str) -> bool:
    """M19: Director 输出是否完全不可信

    判定为 garbage 的情况：
    - 空 / None
    - 只有 FINAL_APPROVED 或 FORCE_OUTPUT 一个词
    - 没有任何 `##` 二级标题（纯 prose preamble，无结构化输出）
    """
    if not text or not text.strip():
        return True
    stripped = text.strip()
    if stripped in ("FINAL_APPROVED", "FORCE_OUTPUT",
                    "FINAL_APPROVED.", "FORCE_OUTPUT.",
                    "FINAL_APPROVED\n", "FORCE_OUTPUT\n"):
        return True
    if "## " not in stripped:
        return True
    return False


_TEMPLATE_ECHO_PHRASES = (
    "合并所有角色",
    "合并所有",
    "同上",
    "150-250 字",
    "200-400 字",
    "100-200 字",
    "300-500 字",
    "由谁决定",
    "由角色决定",
    "由 X 决定",
)


def _is_template_echo(text: str) -> bool:
    """M19: 检测文本是不是 echo 了模板占位符（[xxx] 字面）且没填实际内容

    判定条件（任一）：
    1. ≥ 2 个 `[xxx]` 占位符 且 文本 < 300 字（占位符没被实际内容稀释）
    2. 含明显的模板短语（合并所有角色 / 同上 / 150-250 字 / 200-400 字 ...）
    3. 任一占位符内部含模板短语（如 `[合并所有]` 是 `合并所有角色` 的部分匹配）

    用户 deepseek-v4-pro 的 Director 输出 446 字符 + 6 个占位符 → 命中条件 1
    Director 的 `## 全局约束` 是 `[合并所有角色...]`（1 个占位符）→ 命中条件 2
    Writer 锁定约束是 `[合并所有]`（部分匹配）→ 命中条件 3
    正常 Director 输出 800+ 字符 + 0 占位符 → 都不命中
    """
    if not text or not text.strip():
        return False
    import re
    placeholders = re.findall(r"\[[^\[\]]+\]", text)
    if len(placeholders) >= 2 and len(text) < 300:
        return True
    for phrase in _TEMPLATE_ECHO_PHRASES:
        if phrase in text:
            return True
    # 占位符内部也查一遍（处理 `[合并所有]` 这种部分匹配）
    for p in placeholders:
        inner = p[1:-1].strip()
        for phrase in _TEMPLATE_ECHO_PHRASES:
            if phrase in inner or inner in phrase:
                return True
    return False


def _build_global_constraints_from_locked(messages) -> str:
    """M19 L4 fallback: 从 Writer/Storyboard/DP 的【本角色锁定的全局约束】段拼

    当 Director 的 `## 全局约束` 段是模板占位符时，从上游 3 角色锁定的全局约束
    拼一个真实版本。每个角色的锁定约束都过 `_is_template_echo` 检测。
    """
    parts: list[str] = []
    for source, label in (
        ("Writer", "作家"),
        ("Storyboard", "分镜师"),
        ("DP", "摄影指导"),
    ):
        text = _last_message_text(messages, source)
        if not text:
            continue
        sec = _parse_sections(text)
        locked = _find_section(sec, "锁定的全局约束")
        if locked and not _is_template_echo(locked):
            parts.append(f"### {label}锁定的全局约束\n\n{locked}\n")
    return "\n".join(parts) if parts else ""


def _build_minimal_prompt(messages) -> str:
    """M19 L5 fallback: 连 Storyboard/DP 都没 → 至少给个 metadata 提示

    不返回空字符串（避免 jimeng_prompt.md = "(no prompt produced)"）。
    返回的提示里包含用户硬参数 + 建议重跑，至少让用户知道发生了什么。
    """
    director_text, _ = _find_director_decision(messages)
    has_director = bool(director_text)
    director_status = (
        "输出不可信（仅决策令牌 / 模板占位符 / 纯 prose）"
        if has_director
        else "未发言（被 max_messages 截断）"
    )

    # 找一下用户输入
    user_meta = ""
    for m in messages:
        if getattr(m, "source", "") == "user":
            text = _msg_to_text(m)
            # 截前 200 字作为灵感摘要
            user_meta = text[:200].replace("\n", " ")
            break

    return (
        "## 最终脚本（按镜头组织）\n\n"
        "> ⚠️ 自动提取失败：Director " + director_status + "，"
        "Storyboard/DP 也未输出足够内容。\n"
        "> 建议手动重跑 Step 2 或检查上游角色输出。\n\n"
        f"> 用户灵感摘要：{user_meta}\n"
    )


def extract_prompt(messages) -> str:
    """5 层 fallback — 不论模型多烂都能产出结构完整的最终脚本（M19）

    取 Director 输出的两段：
    - `## 最终脚本（按镜头组织）`（per-shot 详细描述）
    - `## 全局约束`（跨镜头生效的硬约束）

    拼接顺序：先 全局约束 → 再 最终脚本（喂即梦时先看全局再看细节更自然）。

    fallback 链（M17 + M18 + M19）：
    - L1（M17）：Director 有决策但没 per-shot 段 → Storyboard+DP 拼装
    - L2（M18）：Director 完全缺席 → Storyboard+DP 拼装
    - L3（M19 新）：Director 输出但含占位符 echo → Storyboard+DP 拼装
    - L4（M19 新）：global_constr 也是占位符 → 从 Writer/Storyboard/DP 锁定约束拼
    - L5（M19 新）：连 Storyboard/DP 都没 → metadata 提示 + 用户硬参数摘要

    Returns:
        拼好的 prompt；最坏情况下也会返回 metadata 提示（非空）。
    """
    director_text, _ = _find_director_decision(messages)

    # L1/L3 候选：Director 有输出（但要检测是否 garbage / 含占位符）
    if director_text and not _is_garbage_output(director_text):
        d_sec = _parse_sections(director_text)
        global_constr = _find_section(d_sec, "全局约束")
        per_shot = _find_section(d_sec, "最终脚本")

        # L3（M19）：per-shot 是模板占位符 → 降级到 Storyboard+DP
        if per_shot and _is_template_echo(per_shot):
            per_shot = _assemble_per_shot_from_storyboard_dp(messages)
    else:
        # L2（M18）/ Director 输出是 garbage → 降级
        global_constr = ""
        per_shot = _assemble_per_shot_from_storyboard_dp(messages)

    # L4（M19）：global_constr 也是占位符 → 从锁定约束拼
    if global_constr and _is_template_echo(global_constr):
        global_constr = _build_global_constraints_from_locked(messages)

    # L5（M19）：还是没 per-shot → metadata 兜底
    if not per_shot:
        per_shot = _assemble_per_shot_from_storyboard_dp(messages)
        if not per_shot:
            return _build_minimal_prompt(messages)

    parts: list[str] = []
    if global_constr:
        parts.append(f"## 全局约束\n\n{global_constr}\n")
    parts.append(f"## 最终脚本（按镜头组织）\n\n{per_shot}\n")
    return "\n".join(parts)


def _assemble_per_shot_from_storyboard_dp(messages) -> str:
    """M17 fallback：从 Storyboard ## 详细说明 + DP ## 摄影参数 拼装 per-shot 段

    适用场景：reasoning 模型（deepseek-reasoner / qwen3-thinking）的 Director
    在 <think> 块里"决定通过"，但最终输出只有 FINAL_APPROVED 不带 ## 最终脚本。
    这种情况下 jimeng_prompt.md 会是空——本函数拼一个简化版 per-shot 兜底。

    输出格式：
        ### 整体设定
        [Storyboard ## 详细说明 之前的开场文字（如有）+ Writer 故事梗概]

        ### 镜头 N（...）
        **空间校准**：[Storyboard]
        **画面描述**：[Storyboard]
        **摄影参数**：[DP 焦段/光圈/运镜/构图]
        **光影**：[DP 光位/光效/色彩]
        **对白**：[Storyboard 对白]
        **转场**：[Storyboard 转场]

    Returns:
        拼好的 per-shot 文本；无 Storyboard/DP 输出时返回 ""。
    """
    storyboard_text = _last_message_text(messages, "Storyboard")
    dp_text = _last_message_text(messages, "DP")
    writer_text = _last_message_text(messages, "Writer")

    if not storyboard_text and not dp_text:
        return ""

    # 解析 Storyboard：取 "## 详细说明" 段（按 #### 镜头N 拆）
    sb_shots: dict[int, str] = {}  # N → 镜头段全文
    if storyboard_text:
        sb_sec = _parse_sections(storyboard_text)
        detail = _find_section(sb_sec, "详细说明", "详细分镜")
        if detail:
            sb_shots = _parse_shot_subsection(detail, marker="镜头")

    # 解析 DP：取 "## 摄影参数（逐镜头）" 段（按 ### 镜头N 拆）
    dp_shots: dict[int, str] = {}
    if dp_text:
        dp_sec = _parse_sections(dp_text)
        params = _find_section(dp_sec, "摄影参数")
        if params:
            dp_shots = _parse_shot_subsection(params, marker="镜头")

    # 找最大镜头编号
    all_n = set(sb_shots.keys()) | set(dp_shots.keys())
    if not all_n:
        return ""
    max_n = max(all_n)

    # 整体设定：Writer ## 故事梗概 + Storyboard 开场（如有）
    writer_sec = _parse_sections(writer_text) if writer_text else {}
    intro = _find_section(writer_sec, "故事梗概")

    parts: list[str] = []
    if intro:
        parts.append(f"### 整体设定\n\n{intro.strip()}\n")
    else:
        parts.append("### 整体设定\n\n[详见上方 Writer 故事梗概]\n")

    # 逐镜头拼装
    for n in sorted(all_n):
        sb_text = sb_shots.get(n, "")
        dp_text_n = dp_shots.get(n, "")
        header = _guess_shot_header(sb_text, dp_text_n, n)

        sb_fields = _parse_inline_fields(sb_text) if sb_text else {}
        dp_fields = _parse_inline_fields(dp_text_n) if dp_text_n else {}

        parts.append(f"### {header}")
        parts.append("")
        if sb_fields.get("空间校准"):
            parts.append(f"- **空间校准**：{sb_fields['空间校准']}")
        if sb_fields.get("画面描述"):
            parts.append(f"- **画面内容**：{sb_fields['画面描述']}")
        if dp_fields.get("焦段") or dp_fields.get("光圈") or dp_fields.get("构图") or dp_fields.get("摄影运动"):
            dp_line_parts = []
            for k in ("焦段", "光圈", "景深", "摄影运动", "构图"):
                v = dp_fields.get(k)
                if v:
                    dp_line_parts.append(f"{k}={v}")
            if dp_line_parts:
                parts.append(f"- **摄影参数**：{' / '.join(dp_line_parts)}")
        if dp_fields.get("光影"):
            parts.append(f"- **光影**：{dp_fields['光影']}")
        if sb_fields.get("对白"):
            dialogue = sb_fields["对白"].strip("「」").strip()
            parts.append(f"- **对白**：「{dialogue}」")
        elif sb_text and "（无）" not in sb_text and "无对白" not in sb_text:
            # 没解析出来，尝试直接搜
            pass
        if sb_fields.get("转场"):
            parts.append(f"- **转场**：{sb_fields['转场']}")
        parts.append("")

    return "\n".join(parts)


def _parse_shot_subsection(text: str, marker: str = "镜头") -> dict[int, str]:
    """把 "#### 镜头N：[标题]\n...content..." 或 "### 镜头N\n...content..." 拆成 dict

    Args:
        text: 含镜头小节的 markdown 文本
        marker: 标识（"镜头"）

    Returns:
        {N: 段全文（去掉镜头标题行）}，N 从 1 开始
    """
    import re
    shots: dict[int, str] = {}
    # 注意：[：:.] 只能匹配中英冒号或点，**不能**包含 \s——否则会跨行吞掉下一行内容
    pattern = re.compile(rf"^[#]+ {marker}\s*(\d+)[：:.]*(.*)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return shots
    for i, m in enumerate(matches):
        n = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        # chunk 从 m.end() 开始，已经跳过了标题行（如 "### 镜头1" 或 "#### 镜头1：雨夜穿行"）
        chunk = text[start:end].strip()
        shots[n] = chunk
    return shots


def _parse_inline_fields(text: str) -> dict[str, str]:
    """解析 "- **key**：value" 形式的字段"""
    import re
    fields: dict[str, str] = {}
    for line in text.split("\n"):
        m = re.match(r"^\s*[-*]\s*\*\*(.+?)\*\*\s*[：:]\s*(.*)$", line)
        if m:
            key = m.group(1).strip()
            val = m.group(2).strip()
            fields[key] = val
    return fields


def _guess_shot_header(sb_text: str, dp_text: str, n: int) -> str:
    """拼装镜头标题：'镜头 N（0-5秒，中景）'（从 Storyboard 提取时间和景别）"""
    import re
    text = sb_text or dp_text
    time_match = re.search(r"(\d+\s*[-~到]\s*\d+\s*秒)", text)
    time_str = time_match.group(1).replace(" ", "") if time_match else ""
    shot_pattern = r"(远景|全景|中景|近景|特写|大特写|中近景|中全景|大全景|大远景)"
    scene_match = re.search(shot_pattern, sb_text or "")
    if not scene_match:
        scene_match = re.search(shot_pattern, dp_text or "")
    scene_str = scene_match.group(1) if scene_match else ""
    extras = [s for s in (time_str, scene_str) if s]
    if extras:
        return f"镜头 {n}（{','.join(extras)}）"
    return f"镜头 {n}"


# ============== 脚本拼装（M3.2：Director 按镜头写综合段，代码包一层） ==============
def _parse_sections(text: str) -> dict[str, str]:
    """解析 Markdown 文本的 ## 段。返回 {段名: 段体}。

    只解析 ## (二级标题)——Storyboard 的 `### 镜头1` 等子段会保留在
    父段 (`## 摄影参数（逐镜头）`) 的 body 里，不会被提到顶层。
    因此 `prompts/storyboard.md` 必须把 `## 详细说明` 写成二级标题。
    """
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


def _find_section(sections: dict[str, str], *keywords: str) -> str:
    """按关键词（contains 匹配）找段。第一个命中即返回；找不到返回 ""。

    自动截断段体末尾的决策令牌（FINAL_APPROVED / FORCE_OUTPUT）：
    Director 的最终输出常包含 `## 全局约束\n[内容]\nFINAL_APPROVED`，
    不截断会把决策令牌当成约束内容送给即梦。
    """
    body = ""
    for sec_name, sec_body in sections.items():
        for kw in keywords:
            if kw in sec_name:
                body = sec_body
                break
        if body:
            break
    if not body:
        return ""
    # 在独占一行的 FINAL_APPROVED / FORCE_OUTPUT 处截断
    import re
    truncated = re.split(r"\n\s*(?:FINAL_APPROVED|FORCE_OUTPUT)\s*\n?|$", body, maxsplit=1)
    return truncated[0].strip() if truncated else body.strip()


def _last_message_text(messages, source: str) -> str:
    """取指定 source 的最后一条消息文本"""
    for msg in reversed(list(messages)):
        if getattr(msg, "source", "") == source:
            return _msg_to_text(msg)
    return ""


def _find_director_decision(messages) -> tuple[str, bool]:
    """找 Director 的最终决策（FINAL_APPROVED 或 FORCE_OUTPUT）。
    返回 (文本, 是否 FORCE)；没找到返回 ("", False)。"""
    for msg in reversed(list(messages)):
        if getattr(msg, "source", "") != "Director":
            continue
        text = _msg_to_text(msg)
        if "FINAL_APPROVED" in text or "FORCE_OUTPUT" in text:
            return text, "FORCE_OUTPUT" in text
    return "", False


def _get_title(messages, user: UserInput) -> str:
    """从 Writer 输出取脚本标题，fallback 到 user.inspiration"""
    writer_text = _last_message_text(messages, "Writer")
    w_sec = _parse_sections(writer_text)
    title = _find_section(w_sec, "脚本标题")
    title = title.split("\n")[0].strip() if title else ""
    if not title or title == "（待定）":
        return user.inspiration
    return title


def _build_meta(user: UserInput) -> str:
    """构造元数据行：总时长 | 镜头数 | 画幅 | 风格 | 生成时间"""
    meta_parts = [
        f"总时长 {user.duration}s",
        f"镜头数 {user.shot_count if user.shot_count else '由分镜师决定'}",
        f"画幅 {user.aspect_ratio}",
    ]
    if user.style_hint:
        meta_parts.append(f"风格 {user.style_hint}")
    meta_parts.append(f"生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return " | ".join(meta_parts)


def _line(value, default: str) -> str:
    """None/空值显示 default，否则显示 value"""
    if value is None or value == "" or value == []:
        return default
    return str(value)


def _build_user_constraints(user: UserInput) -> str:
    """把 15 字段 user_input 格式化为可读的'### 用户硬参数'子段"""
    lines = ["### 用户硬参数"]
    lines.append(f"- 灵感：{user.inspiration}")
    lines.append(f"- 总时长：{user.duration} 秒")
    lines.append(f"- 画幅：{user.aspect_ratio}")
    lines.append(f"- 镜头数：{_line(user.shot_count, '由分镜师决定')}")
    lines.append(f"- 风格偏好：{_line(user.style_hint, '由角色自由发挥')}")
    lines.append(f"- 画质：{_line(user.quality, '由摄影指导决定')}")
    lines.append(f"- 色调：{_line(user.color_tone, '由摄影指导决定')}")
    lines.append(f"- 质感：{_line(user.texture, '由摄影指导决定')}")
    lines.append(f"- 帧率：{_line(user.frame_rate, '由摄影指导决定')}")
    lines.append(f"- 光影：{_line(user.lighting_mood, '由摄影指导决定')}")
    lines.append(f"- 情绪：{_line(user.mood, '由作家决定')}")
    lines.append(f"- 人物：{_line(user.characters, '由作家决定')}")
    lines.append(f"- 配乐：{_line(user.music_hint, '由合成器决定')}")
    lines.append(f"- 对白/旁白：{_line(user.narration, '由合成器决定')}")
    if user.extra_constraints:
        lines.append("- 额外约束：")
        for c in user.extra_constraints:
            lines.append(f"  - {c}")
    else:
        lines.append("- 额外约束：无")
    return "\n".join(lines)


def _build_pre_constraints(messages, user: UserInput) -> str:
    """构造 '## 前置约束' 段：用户硬参数 + 3 角色锁定的全局约束

    让读者一开始就能看到所有前置约束（不丢信息）：
    - 用户硬参数（15 字段，含 None 时说明由谁决定）
    - 作家锁定的全局约束（Writer ## 【本角色锁定的全局约束】 原文）
    - 分镜师锁定的全局约束
    - 摄影指导锁定的全局约束
    """
    parts: list[str] = ["## 前置约束\n", _build_user_constraints(user)]
    for source, label in (("Writer", "作家"), ("Storyboard", "分镜师"), ("DP", "摄影指导")):
        text = _last_message_text(messages, source)
        if not text:
            continue
        sec = _parse_sections(text)
        locked = _find_section(sec, "锁定的全局约束")
        if locked:
            parts.append(f"### {label}锁定的全局约束\n\n{locked}\n")
    return "\n".join(parts)


def _build_warn(d_sec: dict[str, str]) -> str:
    """FORCE_OUTPUT 时的警告行（统计"## 未解决"段的 - 项数）"""
    unres = _find_section(d_sec, "未解决")
    unresolved_count = len(re.findall(r"^\s*-\s+", unres, re.MULTILINE)) if unres else 0
    warn = "⚠️ 本脚本存在"
    if unresolved_count > 0:
        warn += f" {unresolved_count} 处未解决问题"
    warn += "，建议人工复核后再使用。"
    return f"> {warn}"


def _stitch_by_role(
    messages, user: UserInput, director_text: str, is_force: bool, d_sec: dict[str, str]
) -> str:
    """M3.1 fallback：按角色机械拼装（Director 没输出 per-shot 段时使用）

    顺序：标题 + 元数据 + 警告（FORCE）→ 前置约束 → Director 段 → Writer 段 → Storyboard 段 → DP 段
    """
    writer_text = _last_message_text(messages, "Writer")
    storyboard_text = _last_message_text(messages, "Storyboard")
    dp_text = _last_message_text(messages, "DP")
    w_sec = _parse_sections(writer_text)
    s_sec = _parse_sections(storyboard_text)
    p_sec = _parse_sections(dp_text)

    parts: list[str] = [
        f"# {_get_title(messages, user)}",
        "",
        f"> {_build_meta(user)}",
        "",
    ]
    if is_force:
        parts.append(f"{_build_warn(d_sec)}\n")

    # M3.3：前置约束（用户硬参数 + 3 角色锁定的全局约束）
    parts.append(f"{_build_pre_constraints(messages, user)}\n")

    # Director 段
    decision = _find_section(d_sec, "导演决策")
    if decision:
        parts.append(f"## 导演决策\n\n{decision}\n")
    consistency = _find_section(d_sec, "一致性检查")
    if consistency:
        parts.append(f"## 一致性检查报告\n\n{consistency}\n")
    global_constr = _find_section(d_sec, "全局约束")
    if global_constr:
        parts.append(f"## 全局约束\n\n{global_constr}\n")

    # Writer 段（按顺序）
    for sec_name in ("故事梗概", "人物", "场景设定", "对白", "整体情绪基调"):
        body = _find_section(w_sec, sec_name)
        if body:
            parts.append(f"## {sec_name}\n\n{body}\n")
    writer_locked = _find_section(w_sec, "锁定的全局约束")
    if writer_locked:
        parts.append(f"## 【作家锁定的全局约束】\n\n{writer_locked}\n")

    # Storyboard 段
    storyboard_table = _find_section(s_sec, "镜头序列", "镜头表")
    if storyboard_table:
        parts.append(f"## 镜头表\n\n{storyboard_table}\n")
    storyboard_detail = _find_section(s_sec, "详细说明", "详细分镜")
    if storyboard_detail:
        parts.append(f"## 详细分镜\n\n{storyboard_detail}\n")
    storyboard_locked = _find_section(s_sec, "锁定的全局约束")
    if storyboard_locked:
        parts.append(f"## 【分镜师锁定的全局约束】\n\n{storyboard_locked}\n")

    # DP 段
    dp_spec = _find_section(p_sec, "统一规格")
    if dp_spec:
        parts.append(f"## 统一规格\n\n{dp_spec}\n")
    dp_params = _find_section(p_sec, "摄影参数")
    if dp_params:
        parts.append(f"## 摄影参数（逐镜头）\n\n{dp_params}\n")
    dp_locked = _find_section(p_sec, "锁定的全局约束")
    if dp_locked:
        parts.append(f"## 【摄影指导锁定的全局约束】\n\n{dp_locked}\n")
    dp_bans = _find_section(p_sec, "禁止项")
    if dp_bans:
        parts.append(f"## 禁止项\n\n{dp_bans}\n")

    return "\n".join(parts)


def build_final_script(messages, user: UserInput) -> str:
    """拼装最终脚本（M3.2：优先用 Director 按镜头写的综合段，fallback 到 M3.1 按角色拼装）

    M3.2 主路径：
    - Director 在 FINAL_APPROVED/FORCE_OUTPUT 同一条消息中输出 `## 最终脚本（按镜头组织）`
    - 此函数包一层：标题 + 元数据 + 警告（FORCE）+ Director 判断段 + 跨镜头规格 + Director 脚本

    M3.1 fallback（Director 未输出 per-shot 段时）：
    - 按角色机械拼装

    M19：per-shot 段如果是模板占位符 echo → 走 _stitch_by_role fallback（按角色机械拼装）
    """
    director_text, is_force = _find_director_decision(messages)
    if not director_text:
        return ""

    d_sec = _parse_sections(director_text)

    # M3.2：优先用 Director 写的 "## 最终脚本（按镜头组织）" 段
    per_shot_script = _find_section(d_sec, "最终脚本")
    # M19：检测到是模板占位符 echo → 降级
    if per_shot_script and not _is_template_echo(per_shot_script):
        return _assemble_with_per_shot(messages, user, d_sec, is_force, per_shot_script)

    # M3.1 fallback
    return _stitch_by_role(messages, user, director_text, is_force, d_sec)


def _assemble_with_per_shot(
    messages, user: UserInput, d_sec: dict[str, str], is_force: bool, per_shot_script: str
) -> str:
    """M3.2 主路径：Director 已按镜头写好综合段，包一层：标题+元数据+警告+前置约束+判断段+跨镜头规格+per-shot 段

    M3.3：在判断段前插入 "## 前置约束" 段（用户硬参数 + 3 角色锁定的全局约束），
    让读者在最开头就能看到所有前置约束，避免遗漏。
    """
    parts: list[str] = [
        f"# {_get_title(messages, user)}",
        "",
        f"> {_build_meta(user)}",
        "",
    ]
    if is_force:
        parts.append(f"{_build_warn(d_sec)}\n")

    # M3.3：前置约束（用户硬参数 + 3 角色锁定的全局约束）
    parts.append(f"{_build_pre_constraints(messages, user)}\n")

    # Director 判断段（决策+一致性检查+全局约束）
    for sec_name in ("导演决策", "一致性检查", "全局约束"):
        body = _find_section(d_sec, sec_name)
        if body:
            parts.append(f"## {sec_name}\n\n{body}\n")

    # 跨镜头规格（DP 统一规格 / 禁止项）
    dp_text = _last_message_text(messages, "DP")
    p_sec = _parse_sections(dp_text)
    dp_spec = _find_section(p_sec, "统一规格")
    if dp_spec:
        parts.append(f"## 统一规格\n\n{dp_spec}\n")
    dp_bans = _find_section(p_sec, "禁止项")
    if dp_bans:
        parts.append(f"## 禁止项\n\n{dp_bans}\n")

    # Director 写的 per-shot 脚本（含"### 整体设定" + "### 镜头 N" 段）
    parts.append(f"## 最终脚本（按镜头组织）\n\n{per_shot_script}\n")

    return "\n".join(parts)


def save_run(user: UserInput, messages, script: str, prompt: str, out_dir: Path) -> Path:
    """把整次运行写到 runs/<timestamp>/ 目录"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = out_dir / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "user_input.json").write_text(
        json.dumps(user.__dict__, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "script.md").write_text(script or "(no script produced)", encoding="utf-8")
    (run_dir / "jimeng_prompt.md").write_text(
        prompt or "(no prompt produced)", encoding="utf-8"
    )
    # 完整对话流
    transcript = []
    for m in messages:
        src = getattr(m, "source", "?")
        text = _msg_to_text(m)
        transcript.append(f"=== {src} ===\n{text}\n")
    (run_dir / "transcript.md").write_text("\n".join(transcript), encoding="utf-8")
    return run_dir


async def run_workflow(
    user: UserInput | None = None,
    *,
    raw_text: str | None = None,
    prompts_dir: Path = Path("prompts"),
    runs_dir: Path = Path("runs"),
    stream: bool = True,
) -> dict:
    """跑一次完整工作流，返回结构化结果

    二选一入口：
    - user: 已经是结构化 UserInput，跳过解析
    - raw_text: 自然语言，先调 parse_user_input() 再跑
    """
    if raw_text is not None and user is not None:
        raise ValueError("user 和 raw_text 互斥，只能传一个")
    if raw_text is not None:
        from .parser import parse_user_input
        user = await parse_user_input(raw_text)
        # 打印解析摘要作为日志
        filled = [
            f"{k}={v}"
            for k, v in user.__dict__.items()
            if v not in (None, [], "") and k not in ("inspiration", "duration", "aspect_ratio")
        ]
        summary = ", ".join(filled) if filled else "(无额外参数)"
        print(f"\n[Intaker] 解析完成：{user.inspiration} ({user.duration}s)")
        print(f"[Intaker] 用户额外指定：{summary}\n")
    if user is None:
        raise ValueError("必须传 user 或 raw_text")

    agents = build_all_agents(prompts_dir)
    team = build_team(agents)
    task = build_task_message(user)

    if stream:
        await Console(team.run_stream(task=task))
    # M3.4：失败时自动重试（最多 1 次）
    rv = await run_with_validation(team, task, user, max_retries=1)
    result = rv["result"]

    script = extract_script(result.messages)
    prompt = extract_prompt(result.messages)
    run_dir = save_run(user, result.messages, script, prompt, runs_dir)

    return {
        "stop_reason": result.stop_reason,
        "script": script,
        "prompt": prompt,
        "message_count": len(result.messages),
        "run_dir": run_dir,
        "retries_used": rv["retries_used"],
        "validation_failures": rv["failures"],
    }
