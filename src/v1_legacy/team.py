"""DEPRECATED: v1 legacy. Use src/ (v2) for new work.

GroupChat 团队：终止条件 + 确定性 selector_func

为什么用 selector_func 而不是 selector_prompt？
- 我们的工作流有**严格状态机**：Host → Writer → Storyboard → DP → Director
- 状态机里有"Director 说 MODIFY 后必须回退到被点名的角色"的逻辑
- 让 LLM（selector_prompt）来做这件事不可靠（可能选错）
- 用 Python 函数实现确定性状态机，更可控
"""
from __future__ import annotations

from collections.abc import Sequence

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import (
    MaxMessageTermination,
    TextMentionTermination,
)
from autogen_agentchat.messages import BaseAgentEvent, BaseChatMessage
from autogen_agentchat.teams import SelectorGroupChat

from .config import light_client


# 角色执行顺序（与策划案架构图一致）
ROLE_ORDER = ["Host", "Writer", "Storyboard", "DP", "Director"]

# Director 三种决策对应的停止词
APPROVED = "FINAL_APPROVED"
FORCED = "FORCE_OUTPUT"


def _msg_text(msg) -> str:
    """从消息中提取文本（统一处理）"""
    if hasattr(msg, "to_text"):
        try:
            return msg.to_text() or ""
        except Exception:
            pass
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                out.append(block["text"])
            elif isinstance(block, str):
                out.append(block)
        return "\n".join(out)
    return str(content or "")


def _is_structured_content(text: str) -> bool:
    """M18: 判断消息是否包含结构化内容（## 标题 / markdown 表格 / 代码块）

    背景：deepseek-v4-pro 等模型每次 LLM 调用会先发一段 prose 思考（preamble），
    再发正式结构化输出。这两段会作为两条独立消息进入对话流。
    selector 不能把"角色已发过 preamble"当作"已完成"——否则 Director 还没
    发言就被 max_messages 截断。
    """
    if not text or not text.strip():
        return False
    # 有 ## 或 ### 标题（Director/Writer/Storyboard/DP 都用 ## 段）
    if "## " in text or "### " in text:
        return True
    # 有 markdown 表格分隔行
    if "\n|" in text and "|---" in text:
        return True
    # 有代码块
    if "```" in text:
        return True
    return False


def _is_preamble(text: str) -> bool:
    """M18: 判断消息是否为 preamble（只有 prose 思考，无结构化内容）"""
    return not _is_structured_content(text)


def _spoke(messages: Sequence, role: str) -> bool:
    """M18: 某个角色是否已经发出过结构化内容（preamble 不算"完成"）

    之前用 `source == role` 判断角色是否发言——会把 preamble 当成"已发言"，
    导致 selector 立刻移到下一角色，而该角色还没输出真正的脚本。
    现在要求角色至少发过一条带 ## 标题/表格/代码块的消息才算完成。
    """
    for m in messages:
        if getattr(m, "source", "") == role:
            text = _msg_text(m)
            if _is_structured_content(text):
                return True
    return False


def _last_director_decision(messages: Sequence) -> str | None:
    """读取最近一次 Director 决策的状态：'APPROVE' | 'FORCE' | 'MODIFY' | None"""
    director_msgs = [m for m in messages if getattr(m, "source", "") == "Director"]
    if not director_msgs:
        return None
    text = _msg_text(director_msgs[-1])
    if APPROVED in text:
        return "APPROVE"
    if FORCED in text:
        return "FORCE"
    if "MODIFY" in text or "修改" in text:
        return "MODIFY"
    return None


def _extract_named_role(text: str) -> str | None:
    """从 Director 的修改指令中提取被点名的角色名"""
    # 优先匹配"请 X 修改"
    for role in ROLE_ORDER:
        if f"请 {role}" in text or f"请{role}" in text:
            return role
    return None


def select_next_speaker(
    messages: Sequence[BaseAgentEvent | BaseChatMessage],
) -> str | None:
    """确定性选人逻辑（状态机）

    状态转换：
    - 初始 → Host
    - Host 完成 → Writer → Storyboard → DP → Director
    - Director 决策：
        - MODIFY：选被点名的角色（让其修改）；修改后再选 Director（复审）
        - APPROVE / FORCE：结束（Director 是最后一个角色）
    """
    # 只看 ChatMessage（忽略事件、Task 等）。用 duck typing
    # （hasattr source）以便测试时用 mock 对象
    chat_msgs = [m for m in messages if hasattr(m, "source") and isinstance(getattr(m, "source", None), str)]

    # 调试：追踪 selector 被调用的次数和决策
    import os
    _dbg = os.environ.get("SELECTOR_DEBUG") == "1"

    # 1. 首轮顺序阶段：5 个创作角色
    first_pass = ["Host", "Writer", "Storyboard", "DP", "Director"]
    for role in first_pass:
        if not _spoke(chat_msgs, role):
            if _dbg: print(f"[selector] first_pass → {role}")
            return role

    # 2. 5 个角色都说过一次 → 进入决策阶段
    decision = _last_director_decision(chat_msgs)
    if _dbg: print(f"[selector] decision={decision!r}, msgs={[getattr(m,'source','?') for m in chat_msgs]}")
    if decision == "MODIFY":
        last_director = [m for m in chat_msgs if m.source == "Director"][-1]
        named = _extract_named_role(_msg_text(last_director))
        if named and not _spoke_after(chat_msgs, "Director", named):
            if _dbg: print(f"[selector] MODIFY → {named}")
            return named  # 让被点名的角色修改
        if _dbg: print(f"[selector] MODIFY → Director 复审")
        return "Director"  # 已被修改 → 复审

    # 3. APPROVE / FORCE / 兜底（Director 没明确关键词）：一律结束
    # 关键：兜底不能再返回 "Director"——否则 LLM 偶尔省略关键词时会触发 Director 二次生成
    if _dbg: print(f"[selector] → None (decision={decision})")
    return None


def _spoke_after(
    messages: Sequence, after_role: str, target_role: str
) -> bool:
    """M18: target_role 是否在 after_role 最近一次结构化发言之后发过言

    之前用 `source == role` 判断发言——会把 preamble 当成"已发言"。
    现在要求结构化（## 标题/表格/代码块）才算完成。
    """
    after_idx = None
    for i, m in enumerate(messages):
        if getattr(m, "source", "") == after_role:
            text = _msg_text(m)
            if _is_structured_content(text):
                after_idx = i
                break
    if after_idx is None:
        return False
    for m in messages[after_idx + 1:]:
        if getattr(m, "source", "") == target_role:
            text = _msg_text(m)
            if _is_structured_content(text):
                return True
    return False


def build_team(agents: list[AssistantAgent]) -> SelectorGroupChat:
    """构造 5 角色 GroupChat（策划案 7.3 / 7.4 节）

    M5 简化：Director 是终态，APPROVE/FORCE 后 select_next_speaker
    直接返回 None 让 chain 自然结束。

    M18: max_messages 9 → 20。deepseek-v4-pro 等模型每次 LLM 调用发
    2 条消息（preamble + 结构化），5 角色就需要 10 条 + user 1 条 + 余量。
    之前 9 条硬上限会让 Director 永远拿不到发言机会 → jimeng_prompt.md 空。
    """
    termination = (
        TextMentionTermination(FORCED, sources=["Director"])
        | MaxMessageTermination(max_messages=20)
    )
    return SelectorGroupChat(
        participants=agents,
        model_client=light_client(),  # 不再使用，但 API 要求必填
        termination_condition=termination,
        selector_func=select_next_speaker,  # 用确定性函数
        max_turns=20,
    )


async def run_with_validation(
    team: SelectorGroupChat,
    task: str,
    user,
    *,
    max_retries: int = 1,
) -> dict:
    """包装 team.run()：失败时重新跑并注入 feedback（M3.4）

    工作流：
    1. 第 1 次跑 `team.run(task)`
    2. 校验所有创作角色（Writer/Storyboard/DP/Director）的输出
    3. 如有失败且未达 max_retries → 注入 feedback 重新跑
    4. 超过 max_retries → 用最后一次结果（让 build_final_script fallback）

    Args:
        team: 已 build 的 SelectorGroupChat
        task: 初始 task 消息
        user: UserInput（用于结构校验）
        max_retries: 最多重试次数（默认 1）

    Returns:
        dict 含 result / retries / failures
    """
    from .validator import validate_output, format_feedback

    current_task = task
    last_result = None
    last_failures: list = []
    attempt = 0

    for attempt in range(max_retries + 1):
        result = await team.run(task=current_task)
        last_result = result
        messages = result.messages

        # 校验所有创作角色
        failures: list = []
        for msg in messages:
            src = getattr(msg, "source", "")
            if src in ("Writer", "Storyboard", "DP", "Director"):
                ok, errs = validate_output(src, _msg_text(msg), user)
                if not ok:
                    failures.append((src, errs))

        last_failures = failures
        if not failures or attempt >= max_retries:
            break

        # 注入 feedback，重新跑
        # 注意：task 可能是 str（M3 全流程）或 list[BaseChatMessage]（M4 Step 2）
        # - str：直接拼接 feedback + task（M3 行为）
        # - list：在头部插入一个 TextMessage(source="user") 携带 feedback（M4）
        if isinstance(task, str):
            current_task = format_feedback(failures) + task
        else:
            from autogen_agentchat.messages import TextMessage
            current_task = [TextMessage(
                content=format_feedback(failures),
                source="user",
            )] + list(task)
        attempt += 1  # noqa: F841 — 通过 for 循环自然递增

    return {
        "result": last_result,
        "retries_used": attempt,
        "max_retries": max_retries,
        "failures": last_failures,
    }


async def run_stream_with_validation(
    team: SelectorGroupChat,
    task,
    user,
    *,
    on_message=None,
    team_factory=None,
) -> dict:
    """流式版 team runner（M7）

    与 run_with_validation 的区别：
    - 用 team.run_stream() 替代 team.run()（async generator，不需 await）
    - 每次 yield BaseChatMessage → 调 on_message(role, content, is_token=False)
    - 每次 yield ModelClientStreamingChunkEvent → 调 on_message(role, content, is_token=True)
    - yield TaskResult → 提取所有 messages 返回

    不做格式校验、不做 retry（M7 后由用户反馈/重写兜底）。
    Director 是终态，输出一次就结束。

    Args:
        team: SelectorGroupChat（一次性使用）
        task: 初始 task（str 或 list[BaseChatMessage]）
        user: UserInput（保留参数签名；当前未使用）
        on_message: async callable(role, content, is_token) | None
        team_factory: 保留参数签名；当前未使用

    Returns:
        dict 含 result / retries_used=0 / failures=[]
    """
    from autogen_agentchat.base import TaskResult

    all_messages: list = []
    last_result = None
    async for item in team.run_stream(task=task):
        if isinstance(item, BaseChatMessage):
            all_messages.append(item)
            if on_message is not None:
                role = getattr(item, "source", "") or "?"
                content = _msg_text(item)
                await on_message(role, content, is_token=False)
        elif isinstance(item, TaskResult):
            last_result = item
            for m in item.messages:
                all_messages.append(m)
            break
        else:
            cls_name = type(item).__name__
            if on_message is not None and "StreamingChunk" in cls_name:
                content = getattr(item, "content", "")
                if isinstance(content, str) and content:
                    role = getattr(item, "source", "") or "?"
                    await on_message(role, content, is_token=True)

    return {
        "result": last_result,
        "retries_used": 0,
        "max_retries": 0,
        "failures": [],
    }
