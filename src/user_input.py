"""UserInput：用户输入的 15 字段 dataclass

M21：UserInput 从 v1_legacy.config 拆出，作为独立模块。
不依赖 ModelConfig（用户输入与模型设置解耦）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UserInput:
    """用户输入参数（M3.0 扩到 15 字段）

    字段分组：
    - 必填：inspiration
    - 创意核心：duration / shot_count / aspect_ratio / style_hint
    - 视觉（DP 消费）：quality / color_tone / texture / frame_rate / lighting_mood
    - 故事（Writer 消费）：mood / characters
    - 音频：music_hint / narration
    - 硬约束：extra_constraints
    """

    # === 必填 ===
    inspiration: str

    # === 创意核心 ===
    duration: int = 15
    shot_count: int | None = None
    aspect_ratio: str = "16:9"
    style_hint: str | None = None

    # === 视觉 / DP 消费 ===
    quality: str | None = None                # "4K" / "6K" / "8K"
    color_tone: str | None = None             # "冷" / "暖" / "冷暖对比" / "单色" / "中性"
    texture: str | None = None                # "胶片" / "数字" / "复古" / "水彩"
    frame_rate: int | None = None             # 24 / 30 / 60
    lighting_mood: str | None = None          # "暗调" / "高调" / "体积光" / "逆光" / "侧光"

    # === 故事 / Writer 消费 ===
    mood: str | None = None                   # "紧张" / "温馨" / "孤独" / "治愈" / "史诗" / "悬疑"
    characters: str | None = None             # 自由描述

    # === 音频 ===
    music_hint: str | None = None             # 配乐风格描述
    narration: str | None = None              # "无" / "旁白" / "对白"

    # === 硬约束 ===
    extra_constraints: list[str] = field(default_factory=list)
