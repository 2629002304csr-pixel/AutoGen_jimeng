"""v2 工作流（M21+）：Step 1 (Host+Writer) + Step 2 (Storyboard+DP+Director)

设计要点（对比 v1_legacy）：
- SelectorGroupChat + 硬编码 selector_func：参与者少，顺序就是规则；
  硬编码让 selector 不受 model "角色穿透" 干扰（model 哪怕发了 N 条越权消息，
  下一次仍按 step_order[i+1] 切换）
- 每个 agent system_message 前置 ROLE_BINDING_TEMPLATE：硬约束 role 边界
  （防 Writer 输出 ## Director 内容、Host 主动插话等 DeepSeek/Qwen reasoning
  model 常见穿透问题）
- TextMentionTermination 替代 `_is_structured_content` preamble 检测：
  模型用 FINAL_APPROVED 显式结束，终止条件直接匹配
- 不再用 max_turns budget hack：参与者固定，轮数可控
- 不再用 5 层 fallback：Director 是 Step 2 终态，必出结构化
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Awaitable, Callable, Optional

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TaskResult
from autogen_agentchat.conditions import (
    MaxMessageTermination,
    SourceMatchTermination,
    TextMentionTermination,
)
from autogen_agentchat.messages import (
    BaseChatMessage,
    ModelClientStreamingChunkEvent,
    TextMessage,
)
from autogen_agentchat.teams import SelectorGroupChat

from .config import ModelConfig, make_model_client
from .user_input import UserInput

# 流式回调签名（M22+）：async (role, content) -> None
# - 传 is_token=True 表示流式 chunk，False 表示整条消息定稿
TokenCallback = Callable[[str, str, bool], Awaitable[None]]

# M22+ 续集修补：流式层剔除 Step 2 角色的 preamble（思考独白）
# 推理模型（DeepSeek-V3 / Qwen3-thinking）在结构化段前必写 200+ 字思考独白；
# 前端 isThinkingMonologue 会在累积到一定长度时把整段 bubble 删除。
# 流式层找到第一个 `## ` 段标题，剔除前面所有内容，只把结构化段（及之后）发给前端。
# - Storyboard / DP / Director：必须剔除（写 200+ 字思考的常客）
# - Writer：不剔除（Step 1，且 ROLE_BINDING 提示已压制）
PREAMBLE_STRIP_ROLES = frozenset({"Storyboard", "DP", "Director"})


def _strip_preamble(content: str) -> str:
    """剔除第一个 `## ` 段标题之前的所有内容（preamble / 思考独白）

    推理模型在结构化输出前写 200+ 字中文思考独白，前端 isThinkingMonologue 会
    在累积到 200 字时整段删除 bubble。在流式层把 preamble 切掉，让前端只看到
    结构化段。

    Args:
        content: 模型完整输出

    Returns:
        从第一个 `## ` 行开始的剩余内容；如果找不到 `## ` 则原样返回
    """
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("## "):
            return "\n".join(lines[i:])
    return content


# M22+ 续集修补：推理模型常把整个输出包在 ```markdown ... ``` 代码块里
# （因为它"想"给读者看渲染后的 markdown，结果前端 marked 把它当成字面代码块渲染，
# 用户看到的是"Writer 气泡没渲染就放出来了"）。在流式层剥掉首尾的 markdown fence。
_MARKDOWN_FENCE_RE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*)\n```\s*$", re.DOTALL)


def _strip_markdown_fence(content: str) -> str:
    """如果 content 整体被 ```markdown ... ``` 包住，剥掉 fence

    仅剥**首尾成对**的 fence：开头 ```markdown 开头且结尾 ``` 结尾。
    中间的代码块不动（避免误剥）。

    Args:
        content: 模型完整输出

    Returns:
        剥掉 fence 的内容；如果不是 fence 包裹则原样返回
    """
    if not content:
        return content
    m = _MARKDOWN_FENCE_RE.match(content)
    if m:
        return m.group(1).rstrip()
    return content


# M23+ Director 修订循环：解析 Director ## 导演决策 / **状态**：xxx
# Director 输出模板（prompts/director.md）：
#   ## 导演决策
#   **状态**：通过   或   **状态**：修改
#   **轮次**：1/2
#   ...
# step2_with_revision 用这个判断要不要继续循环
_DIRECTOR_STATUS_RE = re.compile(r"\*\*状态\*\*\s*[：:]\s*(\S+)")
_DIRECTOR_TERMINATION_MARKERS = ("FINAL_APPROVED", "FORCE_OUTPUT")


def _parse_director_status(director_text: str) -> str:
    """解析 Director ## 导演决策 / **状态**：xxx

    Returns:
        "通过" | "修改" | "unknown"（找不到状态字段时原样返回）
    """
    if not director_text:
        return "unknown"
    m = _DIRECTOR_STATUS_RE.search(director_text)
    if not m:
        return "unknown"
    raw = m.group(1).strip()
    if "通过" in raw or "approve" in raw.lower():
        return "通过"
    if "修改" in raw or "modify" in raw.lower() or "revise" in raw.lower():
        return "修改"
    return raw


def _is_director_terminated(director_text: str) -> bool:
    """Director 输出是否含 FINAL_APPROVED / FORCE_OUTPUT 终止标记"""
    if not director_text:
        return False
    return any(marker in director_text for marker in _DIRECTOR_TERMINATION_MARKERS)


# ============== 角色 ==============
ROLE_PROMPTS: dict[str, str] = {
    "Host": "host.md",
    "Writer": "writer.md",
    "Storyboard": "storyboard.md",
    "DP": "dp.md",
    "Director": "director.md",
}

# Step 1 参与者 + 终止条件
STEP1_PARTICIPANTS = ["Host", "Writer"]
STEP1_MAX_MESSAGES = 5  # 兜底：1 user + 2 Host + 2 Writer；正常由 SourceMatchTermination 提前终止

# Step 2 参与者 + 终止条件
STEP2_PARTICIPANTS = ["Storyboard", "DP", "Director"]
STEP2_MAX_MESSAGES = 12  # 3 角色 × 3 消息 + 3 buffer（兜底）

# M22+ 防角色穿透：每个 role 在 system_message 顶部加硬约束前缀
# （不依赖 prompt .md 文件，跨模型一致）
#
# M22+ 续集修补：禁止词按 role 个性化（不是一刀切）：
# - Storyboard 的 prompt 需要 `## 镜头序列 / 景别 / 运镜`，禁用全部 = 自废武功
# - DP 的 prompt 需要 `### 镜头N`，禁用 `## 镜头` 也是误伤
# - Director 的 prompt 需要 `## 全局约束 / ## 最终脚本 / FINAL_APPROVED`
#
# 策略：只禁用**其他角色名 + FINAL_APPROVED / FORCE_OUTPUT 终止符**，
#       不再禁用角色专属关键词（让各角色在自己的领域内自由发挥）
ROLE_BINDING_TEMPLATE = """=== ROLE BINDING (最高优先级) ===
你的名字是 {name}。你**只能**以 {name} 身份发言。
**绝对禁止**冒充其他角色（{other_roles}）。
**绝对禁止**输出终止标记 FINAL_APPROVED / FORCE_OUTPUT（只有 {terminator} 才允许）。
如果你认为应转交其他角色，输出 [PASS] 让下一轮处理。
================================

"""


def _role_binding_for(name: str) -> str:
    """按角色生成专属 ROLE_BINDING 前缀

    - terminator: 只有 Director 允许写 FINAL_APPROVED / FORCE_OUTPUT
    - other_roles: 此角色不能冒充的其它角色名
    """
    all_roles = ["Host", "Writer", "Storyboard", "DP", "Director"]
    others = [r for r in all_roles if r != name]
    terminator = "Director"
    return ROLE_BINDING_TEMPLATE.format(
        name=name,
        other_roles=" / ".join(others),
        terminator=terminator,
    )

# description 字段（SelectorGroupChat 决策边界 + 防 LLM selector 误判）
ROLE_SCOPE_HINT: dict[str, str] = {
    "Host": "纯流程：参数解析 + 约束收集器呈现。不创作内容。",
    "Writer": "故事创作者：脚本标题 / 故事梗概 / 人物 / 场景 / 对白 / 整体情绪基调。绝不输出分镜 / 摄影参数 / Director 决策。",
    "Storyboard": "分镜师：镜头编号 / 景别 / 运镜。仅在 Step 2 活动。",
    "DP": "摄影指导：画质 / 色调 / 光影 / 焦段。仅在 Step 2 活动。",
    "Director": "流程终态：## 全局约束 + ## 最终脚本 + FINAL_APPROVED。仅在 Step 2 活动。",
}


# ============== Agent 构造 ==============
def _prompts_dir() -> Path:
    """prompts/ 目录（项目根）"""
    return Path(__file__).parent.parent / "prompts"


def build_agents(model_cfg: ModelConfig) -> dict[str, AssistantAgent]:
    """每请求独立构造 5 个 agent（不共享 client / 不共享实例）

    修复 v1_legacy 的 hidden coupling：
    v1 旧版 agents 在 build_all_agents() 时通过 light_client()/main_client()
    工厂**构造时**捕获 client。改 model 后 _runtime 变了但 agent 仍用旧 client。

    M27.2：环境变量 JIMENG_DISABLE_STREAM=1 强制切非流式。
    Docker on WSL2 / 严格 NAT / 公司代理可能 idle 杀长 SSE 流，
    部署到 Railway / VPS 通常没事。出错时切非流式 + 重试 2 次足够。
    v2 每次调用都从当前 model_cfg 构造新 client，零全局状态。

    M22+ 强化：
    - 每个 agent 的 system_message 前置 ROLE_BINDING_TEMPLATE（防 model 越权）
    - 加 description（喂给 SelectorGroupChat 的 selector 模型，兜底用）
    """
    client = make_model_client(model_cfg)
    prompts = _prompts_dir()
    agents: dict[str, AssistantAgent] = {}
    # M27.2：env var 强制切非流式（本地 Docker on WSL2 常用）
    use_stream = os.getenv("JIMENG_DISABLE_STREAM", "0") not in ("1", "true", "True")
    for name, fname in ROLE_PROMPTS.items():
        base_prompt = (prompts / fname).read_text(encoding="utf-8")
        bound_prompt = _role_binding_for(name) + base_prompt
        agents[name] = AssistantAgent(
            name=name,
            model_client=client,
            system_message=bound_prompt,
            description=f"{name}: {ROLE_SCOPE_HINT[name]}",
            model_client_stream=use_stream,
        )
    return agents


def selector_func_factory(step_order: list[str]):
    """返回 SelectorGroupChat 的 selector_func：硬编码顺序遍历 step_order。

    与 LLM selector 不同：本函数零 LLM 开销、严格 1-for-1 切换、不被 model
    "穿透角色" 干扰（model 哪怕发了 N 条越权消息，下一次仍然按 step_order[i+1]）。

    **关键：每个 step_order 角色只算一次，不数消息条数。**
    SelectorGroupChat 源码：selector_func 收到的是累计 thread（含 events + 消息）。
    推理模型（DeepSeek / Qwen-thinking）经常把同一轮输出拆成 2 条消息
    （思考独白 + 结构化段），或者主动穿透多发；如果按消息条数累加 `spoken`，
    同一个 role 会被数 2 次 → 直接跳到下一个角色，**DP 被跳过**就是这个 bug。

    修复：维护"每个 step_order 角色是否被叫到过"的 set，每个角色第一次出现
    才推进计数。

    **第二轮之后 pin 到最后一位 role，不再返回 None。**
    SelectorGroupChat 源码（_selector_group_chat.py:162-177）：selector_func
    返回 None 会触发 LLM selector 兜底；DeepSeek 推理模型在 Writer 输出"我作为
    导演收到"时倾向让 Writer 接着说 → 同一 role 反复发言。pin 到最后一位可以
    让 termination_condition（SourceMatchTermination）接管终止，整个循环不会
    把控制权交给 LLM。

    Args:
        step_order: e.g. ["Host", "Writer"] 或 ["Storyboard", "DP", "Director"]

    Returns:
        async def selector_func(messages) -> str
        返回值永远是 step_order 中的一个 role 名；走完一轮后始终返回最后一位。
    """
    cycle_len = len(step_order)
    last = step_order[-1]
    set_seen: set[str] = set()
    _started = False  # 第一次被调用时，把已有的 thread（step1 残留）也纳入 seen

    async def selector_func(messages):
        # 每次调用都扫一遍 thread，更新 set_seen
        # （不能用 _started 一次性快照：上一次返回 "Storyboard" 后，
        #  caller 把 Storyboard 的消息 append 进 thread，但 set_seen 还没更新；
        #  下一次调用必须能识别 Storyboard 已说过）
        for m in messages:
            src = getattr(m, "source", None)
            if src and src in step_order:
                set_seen.add(src)

        # 找下一个还没发言的 step_order 角色
        for role in step_order:
            if role not in set_seen:
                return role
        # 走完一轮 → pin 到最后一位
        return last

    return selector_func


# ============== 任务消息构造 ==============
def build_task_message(
    user: UserInput,
    raw_text: str = "",
    parent_fact_sheet: dict | None = None,
    current_inspiration: str = "",
) -> str:
    """构造 Step 1 初始任务消息（Host 看到）

    Args:
        user: 解析后的用户输入
        raw_text: 用户原始文本（保留以备 Host 参考）
        parent_fact_sheet: M14 续集专用 — 父 session 的 fact_sheet dict
            含 story_arc (list[str]) / characters / world_setting / ending_state
        current_inspiration: M14 续集专用 — 本集灵感（覆盖式）
    """
    parts: list[str] = []
    if raw_text:
        parts.append(f"[用户原始输入] {raw_text.strip()}\n")
    if current_inspiration:
        parts.append(f"[本集灵感] {current_inspiration.strip()}\n")
    parts.append(f"[用户灵感] {user.inspiration}\n")
    parts.append(
        f"[用户硬参数] 总时长 {user.duration}s / 画幅 {user.aspect_ratio} / "
        f"镜头数 {user.shot_count or '由分镜师决定'}\n"
    )
    if user.style_hint:
        parts.append(f"[风格偏好] {user.style_hint}\n")
    if user.characters:
        parts.append(f"[人物设定] {user.characters}\n")
    if user.extra_constraints:
        parts.append("[硬约束]\n")
        for c in user.extra_constraints:
            parts.append(f"- {c}\n")

    # ===== M14：父 fact_sheet 三层注入（续集 / fork 场景）=====
    if parent_fact_sheet:
        parts.append("\n[前情提要]\n")
        arc = parent_fact_sheet.get("story_arc")
        if isinstance(arc, list) and arc:
            parts.append("## 故事线（跨集沉淀）\n")
            for entry in arc:
                parts.append(f"- {entry}\n")
        if parent_fact_sheet.get("characters"):
            parts.append(f"\n## 人物（前情）\n{parent_fact_sheet['characters']}\n")
        if parent_fact_sheet.get("world_setting"):
            parts.append(f"\n## 世界观（前情）\n{parent_fact_sheet['world_setting']}\n")
        if parent_fact_sheet.get("ending_state"):
            parts.append(f"\n## 上集结尾状态\n{parent_fact_sheet['ending_state']}\n")
        parts.append("\n")

    parts.append("\n[流程] Host → Writer → 暂停等用户 review → Storyboard → DP → Director\n")
    return "".join(parts)


# ============== Workflow ==============
class WorkflowV2:
    """M21 干净版工作流

    用法：
        wf = WorkflowV2(model_cfg=cfg)
        result = await wf.step1(task_msg)            # Host + Writer
        result = await wf.step2(history, addition)   # Storyboard + DP + Director
    """

    def __init__(self, model_cfg: ModelConfig):
        self.model_cfg = model_cfg

    async def step1(self, task: str) -> dict:
        """Step 1: Host + Writer 跑一次后停（SourceMatchTermination 兜底 Writer 反复发言）

        Returns:
            {"messages": [...], "writer_output": str, "task_result": TaskResult}
        """
        agents = build_agents(self.model_cfg)
        client = make_model_client(self.model_cfg)
        team = SelectorGroupChat(
            [agents[name] for name in STEP1_PARTICIPANTS],
            model_client=client,
            selector_func=selector_func_factory(STEP1_PARTICIPANTS),
            termination_condition=(
                MaxMessageTermination(STEP1_MAX_MESSAGES)
                | SourceMatchTermination(["Writer"])
            ),
        )
        result: TaskResult = await team.run(task=task)
        msgs = list(result.messages)
        writer_output = _last_text(msgs, "Writer")
        return {
            "messages": msgs,
            "writer_output": writer_output,
            "task_result": result,
        }

    async def step2(self, history: list, user_addition: str = "") -> dict:
        """Step 2 非流式（兼容老调用方）。新代码请直接用 step2_stream。"""
        return await self.step2_stream(history, user_addition)

    async def step1_stream(
        self,
        task: str,
        *,
        on_token: Optional[TokenCallback] = None,
        on_message: Optional[TokenCallback] = None,
    ) -> dict:
        """Step 1 流式：边跑边通过回调推送 chunks / 整条消息。

        M22+ 防穿透：SelectorGroupChat + 硬编码 selector_func（Host→Writer 顺序）
        + participants 物理上只放 Host + Writer，selector 没办法挑其他人。
        配合 MaxMessageTermination(STEP1_MAX_MESSAGES=5)，**Writer 写完就停，强制等用户 review**。
        Step 2 完全由前端 {type:'continue'} 触发（不再是 v1 selector 内部自动跑）。

        回调：
        - on_token(role, chunk, is_token=True)：Model 流式 chunk
        - on_message(role, content, is_token=False)：完整消息定稿
        """
        agents = build_agents(self.model_cfg)
        client = make_model_client(self.model_cfg)
        team = SelectorGroupChat(
            [agents[name] for name in STEP1_PARTICIPANTS],
            model_client=client,
            selector_func=selector_func_factory(STEP1_PARTICIPANTS),
            termination_condition=(
                MaxMessageTermination(STEP1_MAX_MESSAGES)
                | SourceMatchTermination(["Writer"])
            ),
        )
        result, msgs = await _drive_stream(team, task, on_token=on_token, on_message=on_message)
        writer_output = _last_text(msgs, "Writer")
        return {
            "messages": msgs,
            "writer_output": writer_output,
            "task_result": result,
        }

    async def step2_stream(
        self,
        history: list,
        user_addition: str = "",
        *,
        on_token: Optional[TokenCallback] = None,
        on_message: Optional[TokenCallback] = None,
    ) -> dict:
        """Step 2 流式：Storyboard → DP → Director 跑到 FINAL_APPROVED 停"""
        agents = build_agents(self.model_cfg)
        messages: list = list(history)
        if user_addition and user_addition.strip():
            messages.append(TextMessage(content=user_addition.strip(), source="user"))

        director_term = (
            TextMentionTermination("FINAL_APPROVED", sources=["Director"])
            | TextMentionTermination("FORCE_OUTPUT", sources=["Director"])
        )
        client = make_model_client(self.model_cfg)
        team = SelectorGroupChat(
            [agents[name] for name in STEP2_PARTICIPANTS],
            model_client=client,
            selector_func=selector_func_factory(STEP2_PARTICIPANTS),
            termination_condition=director_term | MaxMessageTermination(STEP2_MAX_MESSAGES),
        )
        result, msgs = await _drive_stream(team, messages, on_token=on_token, on_message=on_message)
        jimeng_prompt = extract_jimeng_prompt(msgs)
        return {
            "messages": msgs,
            "jimeng_prompt": jimeng_prompt,
            "task_result": result,
        }

    async def step1b_rewrite_stream(
        self,
        history: list,
        feedback: str,
        *,
        on_token: Optional[TokenCallback] = None,
        on_message: Optional[TokenCallback] = None,
    ) -> dict:
        """M11 重写（M22 v2 流式版）：移除原 Writer 消息，只重跑 Writer 看到用户反馈。

        与 step1_stream 的区别：跳过 Host，只让 Writer 重新输出完整内容。
        """
        history_filtered = [
            m for m in history if getattr(m, "source", "") != "Writer"
        ]
        rewrite_request = (
            f"[用户反馈] {feedback}\n\n"
            "请根据以上反馈**重新输出完整的写作内容**（不是只改一处）。"
            "确保 ## 脚本标题 ## 故事梗概 ## 人物 ## 场景设定 等段齐全，"
            "并同步更新 ## 【本角色锁定的全局约束】 段。"
        )
        task = list(history_filtered) + [
            TextMessage(content=rewrite_request, source="user")
        ]
        agents = build_agents(self.model_cfg)
        client = make_model_client(self.model_cfg)
        team = SelectorGroupChat(
            [agents[name] for name in STEP1_PARTICIPANTS],
            model_client=client,
            selector_func=selector_func_factory(STEP1_PARTICIPANTS),
            termination_condition=(
                MaxMessageTermination(8)
                | SourceMatchTermination(["Writer"])
            ),
        )
        result, msgs = await _drive_stream(team, task, on_token=on_token, on_message=on_message)
        writer_output = _last_text(msgs, "Writer")
        return {
            "messages": msgs,
            "writer_output": writer_output,
            "task_result": result,
        }

    async def step2_with_revision(
        self,
        history: list,
        user_addition: str = "",
        *,
        max_rounds: int = 3,
        on_token: Optional[TokenCallback] = None,
        on_message: Optional[TokenCallback] = None,
    ) -> dict:
        """Step 2 带 Director 修订循环（M23+）：SB → DP → Director 反复到通过或达 max_rounds

        每轮独立跑一次 step2_stream（新的 SelectorGroupChat + 新的 selector_func
        state），历史从 history 累积到当前所有轮的 SB + DP + Director 输出。

        Args:
            history: Step 1 产出的消息列表（含 user + Host + Writer）
            user_addition: 用户 review 时输入的反馈（首轮注入）
            max_rounds: 最多跑几轮，默认 3
                - 第 1..max_rounds-1 轮：Director 自由裁决
                - 第 max_rounds 轮：注入"强制通过"信号，Director 必须出 FINAL_APPROVED
            on_token / on_message: 同 step2_stream

        Returns:
            {
                "messages": list[BaseChatMessage]  所有轮次的 SB + DP + Director 输出
                "jimeng_prompt": str                最后一轮 Director 的 ## 全局约束 + ## 最终脚本
                "rounds": int                       实际跑的轮数（1..max_rounds）
                "director_status": str              "通过" | "修改" | "unknown"
                "task_result": None                 兼容 step2_stream 字段；多轮无单一 result
            }
        """
        accumulated: list[BaseChatMessage] = []
        all_history: list = list(history)
        if user_addition and user_addition.strip():
            all_history.append(TextMessage(content=user_addition.strip(), source="user"))

        last_director_status = "unknown"
        rounds_run = 0
        last_jimeng_prompt = ""

        for round_num in range(1, max_rounds + 1):
            rounds_run = round_num
            is_last_round = round_num == max_rounds

            # 构造本轮的 task：累计 history + 强制通过信号（仅最后一轮）
            task_messages = list(all_history)
            if is_last_round:
                task_messages.append(TextMessage(
                    content=(
                        f"[系统信号] 第 {round_num}/{max_rounds} 轮（最终轮）。"
                        "无论本轮是否仍有冲突，必须在本次输出 ## 最终脚本 + "
                        "FINAL_APPROVED 结束流程。如需列出残余冲突可在 "
                        "## 不一致检测报告 中保留，但 ## 最终脚本 + "
                        "FINAL_APPROVED 必须输出。"
                    ),
                    source="user",
                ))

            # 跑一轮 SB → DP → Director（独立 SelectorGroupChat）
            result = await self.step2_stream(
                task_messages,
                on_token=on_token, on_message=on_message,
            )
            round_msgs = result["messages"]
            last_jimeng_prompt = result["jimeng_prompt"]

            # 解析 Director 本轮的裁决
            director_msgs = [
                m for m in round_msgs if getattr(m, "source", "") == "Director"
            ]
            if not director_msgs:
                # 极端兜底：Director 完全缺席（不应该发生）
                accumulated.extend(round_msgs)
                break

            director_text = _to_text(director_msgs[-1].content)
            last_director_status = _parse_director_status(director_text)

            # 累积本轮消息到 history 供下一轮用
            accumulated.extend(round_msgs)
            all_history = list(all_history) + round_msgs

            if _is_director_terminated(director_text):
                # 通过 / 强制通过 → 收尾
                break
            # 否则本轮没终止，进入下一轮（if 还有 round 可用）

        return {
            "messages": accumulated,
            "jimeng_prompt": last_jimeng_prompt,
            "rounds": rounds_run,
            "director_status": last_director_status,
            "task_result": None,
        }


# ============== 工具函数 ==============
async def _drive_stream(
    team: SelectorGroupChat,
    task,
    *,
    on_token: Optional[TokenCallback] = None,
    on_message: Optional[TokenCallback] = None,
) -> tuple:
    """驱动 team.run_stream()，分发改 token / 整条消息。

    AutoGen 0.7 流式协议：
      async for event in team.run_stream(task):
        - ModelClientStreamingChunkEvent: 一个 chunk，没有 source 字段
          （用上一个 BaseChatMessage 推断 source）
        - BaseChatMessage: 整条消息定稿，含 source + content
        - TaskResult: 流末尾的最终结果

    M22+ 续集修补：Step 2 角色（Storyboard/DP/Director）的消息会经过
    `_strip_preamble` 才发给前端 —— 避免思考独白触发前端 isThinkingMonologue
    整段删除 bubble。完整内容仍存在 messages 列表里（state.json / transcript.md
    仍记全 preamble 用于审计 / 调试）。

    Returns:
        (TaskResult, list[BaseChatMessage])
    """
    final_result = None
    messages: list[BaseChatMessage] = []
    current_source: Optional[str] = None

    async for event in team.run_stream(task=task):
        if isinstance(event, ModelClientStreamingChunkEvent):
            chunk = getattr(event, "content", "")
            # M22+ 修复：每个 chunk 都有自己的 source 字段，直接读
            # （之前用 current_source lazy 初始化会导致第一个角色的首个 chunk
            #  被丢掉，且下一个角色的 chunk 仍带旧 source 标签——前端 streaming 中断）
            chunk_source = getattr(event, "source", None) or current_source
            if chunk and on_token and chunk_source:
                current_source = chunk_source
                await on_token(chunk_source, chunk, True)
        elif isinstance(event, BaseChatMessage):
            messages.append(event)  # 完整内容保留（含 preamble / 含 markdown fence）
            current_source = getattr(event, "source", None) or current_source
            content = getattr(event, "content", "")
            text = _to_text(content)
            # M22+ 续集修补 1：剥掉 ```markdown ... ``` 包裹（推理模型常见）
            text = _strip_markdown_fence(text)
            # M22+ 续集修补 2：Step 2 角色剔除 preamble（前端只看结构化段）
            if current_source in PREAMBLE_STRIP_ROLES:
                text = _strip_preamble(text)
            if on_message and text:
                await on_message(current_source, text, False)
        else:
            # TaskResult 在流末尾到来
            final_result = event

    return final_result, messages


def _to_text(content) -> str:
    """把 message.content（str | list[dict] | 其他）规整成纯文本"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content
        )
    return str(content) if content is not None else ""


def _last_text(messages, source: str) -> str:
    """取指定 source 最后一条消息的文本内容"""
    for m in reversed(list(messages)):
        if getattr(m, "source", "") != source:
            continue
        content = getattr(m, "content", "")
        if isinstance(content, str):
            return content
        # content blocks（OpenAI 多模态格式）
        if isinstance(content, list):
            return "\n".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in content
            )
    return ""


def _parse_sections(text: str) -> dict[str, str]:
    """解析 Markdown 文本的 ## 段，返回 {段名: 段体}"""
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = line[3:].strip()
            lines = []
        else:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections


def extract_jimeng_prompt(messages) -> str:
    """从 Director 最后一条消息提取 `## 全局约束` + `## 最终脚本`

    v2 简化：Director 是 Step 2 终态，必出结构化。只取这两段；其他段（导演决策 /
    一致性检查）丢弃。

    截断策略：取到段体末尾的 FINAL_APPROVED / FORCE_OUTPUT 标记前为止。

    Returns:
        拼接后的 jimeng_prompt 文本；Director 缺席或没结构化则返回 ""
    """
    director_msgs = [m for m in messages if getattr(m, "source", "") == "Director"]
    if not director_msgs:
        return ""
    text = director_msgs[-1].content
    if not isinstance(text, str):
        return ""

    sections = _parse_sections(text)
    out: list[str] = []
    for sec_name, body in sections.items():
        if "全局约束" in sec_name or "最终脚本" in sec_name:
            clean = _truncate_termination_marker(body)
            out.append(f"## {sec_name}\n\n{clean}\n")
    return "\n".join(out) if out else ""


def _truncate_termination_marker(body: str) -> str:
    """截断 body 末尾的 FINAL_APPROVED / FORCE_OUTPUT 标记"""
    # Director 输出模板：
    #   ## 最终脚本
    #   <内容>
    #
    #   FINAL_APPROVED
    # 把 FINAL_APPROVED 之前的内容留下
    for marker in ("FINAL_APPROVED", "FORCE_OUTPUT"):
        idx = body.rfind(marker)
        if idx >= 0:
            body = body[:idx].rstrip()
            break
    return body