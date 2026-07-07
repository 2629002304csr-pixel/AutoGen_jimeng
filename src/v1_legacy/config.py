"""DEPRECATED: v1 legacy. Use src/ (v2) for new work.

配置：用户输入参数 + 模型客户端工厂

支持国内主流 OpenAI 兼容 API（DeepSeek / Qwen），OpenAI 也保留作为备选。

M16：把模块级常量改为 `_RuntimeConfig` dataclass，前端 / 启动时可热切换：
- 旧：PROVIDER / API_BASE / LIGHT_MODEL / MAIN_MODEL 是模块级常量，导入后不可改
- 新：`_runtime` 是模块级 mutable dataclass，`apply_user_config()` 可替换
- 向后兼容：旧的顶层常量仍可用，但值在第一次 import 时从 env 派生（不再反映后续变更）

注意：
- `light_client()` / `main_client()` 每次调用都用当前 `_runtime` 的值 → 改完立即生效（对**新构造**的 client）
- 已在 agent 里的旧 client 不会自动重建（这是 M16 的设计：跑的 step 跑完，下个 step 用新 client）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from autogen_ext.models.openai import OpenAIChatCompletionClient

if TYPE_CHECKING:
    from .user_config import UserConfig


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


# ============== 模型供应商预设 ==============
# 思路：换 base_url + model + model_info 三件套就能切供应商
# - deepseek：默认推荐，便宜（V3 输入 1 元/百万 token），64K 上下文，支持 function calling
# - qwen：阿里 DashScope 兼容端点；qwen-plus 性价比最好
# - openai：保留作为对照/海外用户备选
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "light_model": "deepseek-chat",       # 轻量（Host）
        "main_model": "deepseek-chat",        # 主力（Writer / Storyboard / DP / Director）
        # 同一个模型 + 提示词工程分级，比切两档模型更稳
        "model_info": {
            "vision": False,
            "function_calling": True,
            "json_output": True,              # V3 支持 JSON mode
            "structured_output": False,       # V3 不支持 strict JSON schema
            "multiple_system_messages": False,
            "family": "deepseek",
        },
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "light_model": "qwen-turbo",
        "main_model": "qwen-plus",
        "model_info": {
            "vision": False,
            "function_calling": True,
            "json_output": True,
            "structured_output": True,        # qwen-plus/max 支持
            "multiple_system_messages": False,
            "family": "qwen",
        },
    },
    "openai": {
        "base_url": "",                        # 留空 = 官方端点
        "light_model": "gpt-4o-mini",
        "main_model": "gpt-4.1",
        "model_info": {
            "vision": False,
            "function_calling": True,
            "json_output": True,
            "structured_output": True,
            "multiple_system_messages": True,
            "family": "gpt-4",
        },
    },
}


# ============== API key 解析 ==============
# 优先 OPENAI_API_KEY（保持原项目命名），再读供应商专用变量
_PROVIDER_KEY_ENV = {
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _get_api_key() -> str | None:
    """从环境变量读 API key（向后兼容老 .env 用户）

    注意：M16 引入后，这个函数只在 `_runtime` 初始化时被调一次（推导默认 key）。
    用户在前端改了 key 之后，`_runtime.api_key` 直接被覆盖，env 不再参与。
    """
    # M16 起始 provider 用 "deepseek" 默认（如果环境变量没设）
    provider = os.getenv("MODEL_PROVIDER", "deepseek").lower()
    if provider not in PROVIDER_PRESETS:
        provider = "deepseek"
    return os.getenv("OPENAI_API_KEY") or os.getenv(_PROVIDER_KEY_ENV.get(provider, ""))


# ============== M16: 运行时可变状态 ==============

@dataclass
class _RuntimeConfig:
    """M16：模型客户端的运行时配置（可在 app 生命周期内被替换）

    字段语义同原模块级常量，但允许运行时被 `apply_user_config()` 整体替换。
    """
    provider: str = "deepseek"
    base_url: str = ""
    light_model: str = ""
    main_model: str = ""
    api_key: str | None = None
    timeout: float = 60.0
    max_retries: int = 3
    model_info: dict = field(default_factory=dict)


def _init_runtime_from_env() -> _RuntimeConfig:
    """从环境变量推导默认 _RuntimeConfig（向后兼容 .env 用户）

    - MODEL_PROVIDER 不识别 → fallback deepseek（不抛异常，避免 import 失败）
    - OPENAI_API_BASE / OPENAI_BASE_URL → 覆盖 provider 默认
    - LIGHT_MODEL / MAIN_MODEL → 覆盖 provider 默认
    - OPENAI_API_KEY / DEEPSEEK_API_KEY / DASHSCOPE_API_KEY → key
    """
    provider = os.getenv("MODEL_PROVIDER", "deepseek").lower()
    if provider not in PROVIDER_PRESETS:
        provider = "deepseek"
    preset = PROVIDER_PRESETS[provider]
    return _RuntimeConfig(
        provider=provider,
        base_url=os.getenv("OPENAI_API_BASE") or os.getenv("OPENAI_BASE_URL") or preset["base_url"],
        light_model=os.getenv("LIGHT_MODEL", preset["light_model"]),
        main_model=os.getenv("MAIN_MODEL", preset["main_model"]),
        api_key=_get_api_key(),
        timeout=float(os.getenv("OPENAI_API_TIMEOUT", "60")),
        max_retries=int(os.getenv("OPENAI_API_MAX_RETRIES", "3")),
        model_info=dict(preset["model_info"]),
    )


# 模块级单例（每次 apply_user_config 替换它）
_runtime: _RuntimeConfig = _init_runtime_from_env()


# ============== 向后兼容的模块级常量（冻结快照）==============
# 这些只在 import 时被赋值一次；如果用户在 Web UI 里改了配置，这些常量值不会跟着变
# 留它们是为了不破坏依赖这些常量的旧代码（tests / demo_run）
PROVIDER = _runtime.provider
API_BASE = _runtime.base_url
LIGHT_MODEL = _runtime.light_model
MAIN_MODEL = _runtime.main_model
API_TIMEOUT = _runtime.timeout
API_MAX_RETRIES = _runtime.max_retries


def apply_user_config(user_cfg: "UserConfig") -> None:
    """M16：把前端传来的 UserConfig 应用到 `_runtime`

    - provider 决定默认 base_url / model_info；用户显式值优先
    - api_key 用户没改时（空字符串）→ 保留当前 key（防止误删）
    - model_info_overrides 合并到 preset（高级用户用）

    调用时机：
    - web_server.py 启动时调一次（load 已有 _user_config.json）
    - 前端 POST /api/config 后调一次（应用新配置）
    """
    global _runtime, PROVIDER, API_BASE, LIGHT_MODEL, MAIN_MODEL, API_TIMEOUT, API_MAX_RETRIES

    provider = user_cfg.provider.lower() if user_cfg.provider else "deepseek"
    if provider not in PROVIDER_PRESETS:
        provider = "deepseek"  # 不抛异常，让前端显示 fallback
    preset = PROVIDER_PRESETS[provider]
    preset_model_info = dict(preset["model_info"])

    # model_info_overrides 合并到 preset
    if user_cfg.model_info_overrides:
        try:
            preset_model_info.update(dict(user_cfg.model_info_overrides))
        except (TypeError, ValueError):
            pass

    # api_key 优先级：用户值 > 当前 _runtime > env fallback
    new_key = (user_cfg.api_key or "").strip()
    if not new_key and _runtime.api_key:
        new_key = _runtime.api_key
    if not new_key:
        new_key = _get_api_key()  # 最后 fallback 到 env

    _runtime = _RuntimeConfig(
        provider=provider,
        base_url=(user_cfg.base_url or preset["base_url"]).strip(),
        light_model=(user_cfg.light_model or preset["light_model"]).strip(),
        main_model=(user_cfg.main_model or preset["main_model"]).strip(),
        api_key=new_key or None,
        timeout=user_cfg.timeout if user_cfg.timeout > 0 else 60.0,
        max_retries=user_cfg.max_retries if user_cfg.max_retries > 0 else 3,
        model_info=preset_model_info,
    )

    # 同步更新向后兼容常量（M16 仍然让它们反映最新值，便于单测检查）
    PROVIDER = _runtime.provider
    API_BASE = _runtime.base_url
    LIGHT_MODEL = _runtime.light_model
    MAIN_MODEL = _runtime.main_model
    API_TIMEOUT = _runtime.timeout
    API_MAX_RETRIES = _runtime.max_retries


def get_runtime_snapshot() -> dict:
    """M16：返回当前 _runtime 快照（API key 已 mask）—— 给前端 GET /api/config"""
    from .user_config import mask_api_key
    return {
        "provider": _runtime.provider,
        "base_url": _runtime.base_url,
        "light_model": _runtime.light_model,
        "main_model": _runtime.main_model,
        "api_key": mask_api_key(_runtime.api_key or ""),
        "api_key_set": bool(_runtime.api_key),
        "timeout": _runtime.timeout,
        "max_retries": _runtime.max_retries,
    }


# ============== 客户端工厂（M16：每次从 _runtime 读最新值）==============

def _client_kwargs() -> dict:
    """构造所有客户端共用的参数（每次调用读 _runtime 当前值）"""
    kw: dict[str, Any] = {
        "timeout": _runtime.timeout,
        "max_retries": _runtime.max_retries,
        "model_info": _runtime.model_info,
    }
    if _runtime.base_url:
        kw["base_url"] = _runtime.base_url
    if _runtime.api_key:
        kw["api_key"] = _runtime.api_key
    return kw


def light_client() -> OpenAIChatCompletionClient:
    """Host 使用的轻量模型客户端（每次调用读 _runtime）"""
    return OpenAIChatCompletionClient(
        model=_runtime.light_model,
        temperature=0.4,
        **_client_kwargs(),
    )


def main_client() -> OpenAIChatCompletionClient:
    """Writer / Storyboard / DP / Director 使用的主力模型客户端（每次调用读 _runtime）"""
    return OpenAIChatCompletionClient(
        model=_runtime.main_model,
        temperature=0.7,
        **_client_kwargs(),
    )