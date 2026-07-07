"""DEPRECATED: v1 legacy. Use src/ (v2) for new work.

角色定义：5 个 AssistantAgent 工厂"""
from __future__ import annotations

from pathlib import Path

from autogen_agentchat.agents import AssistantAgent

from .config import light_client, main_client


# 角色清单（按策划案第三节）
# (名称, prompt 文件名, 模型档位, 给 GroupChat selector 看的简介)
ROLE_SPECS: list[tuple[str, str, str, str]] = [
    (
        "Host",
        "host.md",
        "light",
        "主持人：解析用户参数、初始化全局约束收集器、串联流程，"
        "严格不产生创意内容。",
    ),
    (
        "Writer",
        "writer.md",
        "main",
        "作家：将 1-3 句灵感扩写为完整故事框架，输出时必须包含"
        "【本角色锁定的全局约束】段。",
    ),
    (
        "Storyboard",
        "storyboard.md",
        "main",
        "分镜师：将故事拆解为镜头序列（按用户指定镜头数或按时长推断），"
        "建立空间坐标系，输出镜头表+详细分镜。",
    ),
    (
        "DP",
        "dp.md",
        "main",
        "摄影指导：为每个镜头添加焦段/光圈/运镜/光影等摄影参数，"
        "并可选声明禁止项；输出统一规格段+逐镜头参数段。",
    ),
    (
        "Director",
        "director.md",
        "main",
        "导演：检测跨角色约束冲突，按 DP > Storyboard > Writer 优先级裁决；"
        "通过时输出 FINAL_APPROVED，2 轮后强制输出时输出 FORCE_OUTPUT。"
        "Director 同时负责产出'## 最终脚本（按镜头组织）' + '## 全局约束'，"
        "这两段直接喂 jimeng_prompt.md（不再过 Synthesizer）。",
    ),
]


def build_agent(name: str, prompts_dir: Path, tier: str) -> AssistantAgent:
    """根据角色名构造 AssistantAgent

    M7 启用：model_client_stream=True — 让角色输出走流式（token 一边产生一边推送），
    否则即使 team.run_stream() 调起来，角色 agent 仍以非流式一次性返回。
    """
    spec = next(s for s in ROLE_SPECS if s[0] == name)
    prompt_file = prompts_dir / spec[1]
    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_file}")
    system_message = prompt_file.read_text(encoding="utf-8")
    client = light_client() if tier == "light" else main_client()
    return AssistantAgent(
        name=spec[0],
        system_message=system_message,
        model_client=client,
        description=spec[3],
        model_client_stream=True,  # M7：流式输出
    )


def build_all_agents(prompts_dir: Path) -> list[AssistantAgent]:
    """按 ROLE_SPECS 顺序构造全部 5 个角色"""
    return [build_agent(name, prompts_dir, tier) for name, _, tier, _ in ROLE_SPECS]
