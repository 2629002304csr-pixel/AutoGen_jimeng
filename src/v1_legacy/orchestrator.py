"""DEPRECATED: v1 legacy. Use src/ (v2) for new work.

M4 工作流编排器：分步执行 + 用户介入

为什么需要 orchestrator？
- 当前 `team.run()` 一次跑完所有 5 个角色，无法在中间暂停
- M4 改造为分步执行：
    Step 1: Host + Writer（暂停，让用户 review）
    Step 2: Storyboard → DP → Director（完成）
- Step 1 完成后保存到 Session（`runs/<session_id>/state.json`）
- 用户追加灵感后通过 `--resume <session_id> --add "..."` 接着跑

实现思路（用 AutoGen 原生 API）：
- 不重写 GroupChat，而是用 `MaxMessageTermination(N)` 控制每步的轮数
- Step 1: `MaxMessageTermination(2)` → 跑 Host + Writer 后停
- Step 2: 重新构造 team（因为前一个 team 已结束），`MaxMessageTermination(7)` → 跑剩余 3 个角色
- Step 2 把历史消息作为 `task=[history_messages]` 传入，selector 函数会看到 Host+Writer 已发言
  → 第一次 `_spoke(messages, role)` 检查会跳过它们，直接返回 Storyboard
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from autogen_agentchat.conditions import (
    MaxMessageTermination,
    TextMentionTermination,
)
from autogen_agentchat.messages import TextMessage
from autogen_agentchat.teams import SelectorGroupChat

from .config import light_client
from .roles import build_all_agents
from .team import (
    APPROVED,
    FORCED,
    run_stream_with_validation,
    select_next_speaker,
)

if TYPE_CHECKING:
    from .config import UserInput


# 每步允许的最大消息数
#
# M18 调整：deepseek-v4-pro 等模型每次 LLM 调用会发 2 条消息（preamble + 结构化），
# 给每个角色预留 2 条消息的预算 + user 1 条 + buffer。
#
# STEP1_MAX_TURNS=8（M18）：
#   1 user + 2 Host (preamble+content) + 2 Writer (preamble+content) + 3 buffer
#   之前是 3：preamble 出现时 Writer 永远拿不到发言机会
#
# STEP2_MAX_TURNS=15（M18）：
#   4 preload + 2 Storyboard + 2 DP + 2 Director + 5 buffer
#   之前是 7：Director 永远拿不到发言机会 → jimeng_prompt.md 空
#
# STEP1B_MAX_TURNS=10（M18）：
#   2 preload (Host + user feedback) + 2 Writer (preamble+content) + 6 buffer
#   之前是 4
STEP1_MAX_TURNS = 8   # M18: user task + Host + Writer (含 preamble 余量)
STEP2_MAX_TURNS = 15  # M18: 4 preload + Storyboard + DP + Director (含 preamble 余量)
STEP1B_MAX_TURNS = 10 # M18: 2 preload + Writer (含 preamble 余量)


def build_step_team(agents, *, max_turns: int) -> SelectorGroupChat:
    """构造一个 SelectorGroupChat，用 MaxMessageTermination(max_turns) 控制步长

    与 `team.build_team()` 的区别：本函数可指定 max_turns（每步独立计）。

    M7 启用：model_client_streaming=True — 让 selector 的 LLM 也走流式
    （虽然 selector 用的是确定性函数 select_next_speaker，
    但 AutoGen 内部仍会用 LLM 做一次"提醒哪些角色可以发言"的判断，
    这部分也开流式可以减少 selector 阶段的等待）。

    终止条件：
    - 只保留 MaxMessageTermination 作为硬上限（保险丝）
    - 不再用 TextMentionTermination(APPROVED)——
      Director 是终态，APPROVE/FORCE 后 select_next_speaker 直接返回 None
    - FORCE_OUTPUT 仍然保留（Director 强制终止）

    这是 M3.4 一直存在的 bug：TextMentionTermination 触发时机太早，
    导致 Director 之后角色（曾为 Synthesizer）从未真正在真实 LLM 模式下跑过。
    """
    termination = (
        TextMentionTermination(FORCED, sources=["Director"])
        | MaxMessageTermination(max_messages=max_turns)
    )
    return SelectorGroupChat(
        participants=agents,
        model_client=light_client(),
        termination_condition=termination,
        selector_func=select_next_speaker,
        max_turns=max_turns,
        model_client_streaming=True,  # M7：selector 也开流式
    )


class WorkflowOrchestrator:
    """分步工作流编排器

    用法：
        orch = WorkflowOrchestrator(user=user_input, prompts_dir=Path("prompts"))
        result1 = await orch.step1_host_writer(task_message)
        # 此时 result1 包含 writer_output；保存到 Session；等用户输入
        result2 = await orch.step2_continue(
            history_messages=result1["messages"],
            user_addition="加一只橘猫",
        )

    Attributes:
        user: 用户输入（用于 M3.4 格式校验）
        agents: 6 个角色
    """

    def __init__(
        self,
        user: "UserInput",
        prompts_dir: Path = Path("prompts"),
    ):
        self.user = user
        self.prompts_dir = prompts_dir
        self.agents = build_all_agents(prompts_dir)

    async def step1_host_writer(self, task: str, *, on_message=None) -> dict:
        """Step 1: 跑 Host + Writer（暂停）

        终止条件：MaxMessageTermination(2) → 2 轮后停

        Args:
            task: 任务消息（自然语言 + 用户硬参数）
            on_message: M7 异步回调 (role, content, is_token)，
                None 时走原 team.run() 路径（向后兼容）。

        Returns:
            {
                "messages": list,  # 全部消息（含 Host + Writer）
                "writer_output": str,  # Writer 最后一条消息
                "stop_reason": str,  # 停止原因
                "message_count": int,
            }
        """
        team = build_step_team(self.agents, max_turns=STEP1_MAX_TURNS)
        if on_message is None:
            # 向后兼容：原 team.run() 路径
            result = await team.run(task=task)
        else:
            # M7 流式路径
            rv = await run_stream_with_validation(
                team, task, self.user,
                on_message=on_message,
                team_factory=lambda: build_step_team(self.agents, max_turns=STEP1_MAX_TURNS),
            )
            result = rv["result"]
        messages = list(result.messages)

        # 提取 Writer 的最后一条
        writer_output = _last_text(messages, "Writer")

        return {
            "messages": messages,
            "writer_output": writer_output,
            "stop_reason": result.stop_reason,
            "message_count": len(messages),
        }

    async def step1b_rewrite_writer(
        self,
        history_messages: list,
        user_feedback: str,
        *,
        on_message=None,
    ) -> dict:
        """Step 1b（M11）：跳过 Host，仅重跑 Writer（看到 feedback 后重写）

        关键技巧：从 history_messages 中**移除**所有 Writer 消息，
        让 selector_func 的 `_spoke()` 检查认为 Writer 还没发言 → 重选 Writer。
        Writer 看到：原 Host + 原其他角色上下文 + 用户反馈 → 输出新完整内容。

        Args:
            history_messages: Step 1 跑完后的消息列表（含 Host + Writer + Step 2 角色，如有）
            user_feedback: 用户反馈/修改意见
            on_message: M7 异步回调 (role, content, is_token)，
                None 时走原 team.run() 路径（向后兼容）。

        Returns:
            {
                "messages": list,  # 全部消息（filtered history + user + 新 Writer）
                "new_writer_output": str,  # Writer 重写后的输出
                "stop_reason": str,
                "message_count": int,
            }
        """
        # 移除所有 Writer 消息——让 selector 重选 Writer
        filtered_history = [
            m for m in history_messages
            if getattr(m, "source", "") != "Writer"
        ]

        rewrite_request = (
            f"[用户反馈/修改意见]\n{user_feedback}\n\n"
            "请根据以上反馈**重新输出完整的写作内容**（不只是改一处）。\n"
            "保留你认为好的部分，只改用户不满意的部分。\n"
            "确保 ## 脚本标题 ## 故事梗概 ## 人物 ## 场景设定 ## 对白 等段齐全，"
            "并同步更新 ## 【本角色锁定的全局约束】 段。"
        )
        task = filtered_history + [
            TextMessage(content=rewrite_request, source="user")
        ]

        team = build_step_team(self.agents, max_turns=STEP1B_MAX_TURNS)
        if on_message is None:
            result = await team.run(task=task)
        else:
            rv = await run_stream_with_validation(
                team, task, self.user,
                on_message=on_message,
                team_factory=lambda: build_step_team(self.agents, max_turns=STEP1B_MAX_TURNS),
            )
            result = rv["result"]
        messages = list(result.messages)
        new_writer_output = _last_text(messages, "Writer")

        return {
            "messages": messages,
            "new_writer_output": new_writer_output,
            "stop_reason": result.stop_reason,
            "message_count": len(messages),
        }

    async def step2_continue(
        self,
        history_messages: list,
        *,
        user_addition: str = "",
        max_retries: int = 1,
        on_message=None,
    ) -> dict:
        """Step 2: 跑 Storyboard → DP → Director（完成）

        Args:
            history_messages: Step 1 跑完后的消息列表
            user_addition: 用户追加的灵感（注入到消息流中）
            max_retries: 校验失败时的最大重试次数（默认 1，与 M3.4 一致）
            on_message: M7 异步回调 (role, content, is_token)，
                None 时走原 run_with_validation 路径（向后兼容）。

        Returns:
            {
                "messages": list,  # 全部消息（Step 1 + Step 2）
                "new_messages": list,  # Step 2 新产生的消息
                "stop_reason": str,
                "message_count": int,
                "retries_used": int,
                "validation_failures": list,
            }
        """
        # 构造本步的 task
        # 注意：不能用 UserMessage（不是 BaseChatMessage 子类），
        # 用 TextMessage(source="user") 代替——selector 函数只关心 ROLE_ORDER
        # 中的角色（Host/Writer/...），user 不影响状态机。
        task_for_step2: list = list(history_messages)
        if user_addition:
            task_for_step2.append(
                TextMessage(content=user_addition, source="user")
            )

        # 用一个新的 team（max_turns=7）跑剩余流程
        # 因为前一个 team 已因 MaxMessageTermination(2) 终止
        # selector 函数看到 Host+Writer 已发言，会自动从 Storyboard 开始
        team = build_step_team(self.agents, max_turns=STEP2_MAX_TURNS)

        # M7：on_message 不为 None 时走流式路径（不做 retry）
        if on_message is None:
            from .team import run_with_validation
            rv = await run_with_validation(
                team, task_for_step2, self.user, max_retries=max_retries
            )
        else:
            rv = await run_stream_with_validation(
                team, task_for_step2, self.user,
                on_message=on_message,
                team_factory=lambda: build_step_team(self.agents, max_turns=STEP2_MAX_TURNS),
            )
        result = rv["result"]

        # 计算 new_messages：result.messages 包含 history + new，
        # 用 id() 去重（AutoGen 会复用 task 中传入的消息对象）
        history_ids = {id(m) for m in history_messages}
        all_messages = list(result.messages)
        new_messages = [m for m in all_messages if id(m) not in history_ids]

        return {
            "messages": all_messages,
            "new_messages": new_messages,
            "stop_reason": result.stop_reason,
            "message_count": len(all_messages),
            "retries_used": rv["retries_used"],
            "validation_failures": rv["failures"],
        }


def _last_text(messages, source: str) -> str:
    """取指定 source 的最后一条消息文本"""
    for m in reversed(list(messages)):
        if getattr(m, "source", "") != source:
            continue
        content = getattr(m, "content", "")
        if isinstance(content, str):
            return content
        # content blocks
        try:
            return m.to_text() or ""
        except Exception:
            return str(content)
    return ""
