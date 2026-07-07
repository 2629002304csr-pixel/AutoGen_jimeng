"""M21 v2 配置：ModelConfig + 客户端工厂（per-request，无全局状态）

为什么不再用 _runtime？
- 旧版用模块级 mutable `_runtime` dataclass + `light_client()` / `main_client()` 工厂
- 工厂每次从 `_runtime` 读最新值，但 agents 在 `build_all_agents()` 时通过工厂捕获 client
- 用户改 model 后 _runtime 变了，但**已经在内存里的 agents** 仍用旧 client
- v2：ModelConfig 是 immutable dataclass，make_model_client 是纯函数 → 每请求独立
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from autogen_ext.models.openai import OpenAIChatCompletionClient


# ============== 供应商预设 ==============
# 思路：换 base_url + main_model + model_info 三件套就能切供应商
# - deepseek：默认推荐，便宜（V3 输入 1 元/百万 token），64K 上下文
# - qwen：阿里 DashScope 兼容端点；qwen-plus 性价比最好
# - openai：保留作为对照/海外用户备选
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "main_model": "deepseek-chat",
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


@dataclass
class ModelConfig:
    """模型设置（per-request，无全局状态）

    用户改 model：前端 POST /api/v2/config 保存到 runs/_user_config.json
    下次 /api/v2/run 请求从该文件读 ModelConfig（也可请求 body 覆盖）
    每次请求：make_model_client(cfg) 创建新 client，build_agents() 创建新 agents
    """
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    main_model: str = "deepseek-chat"
    temperature: float = 0.7
    timeout: float = 180.0
    max_retries: int = 2
    model_info: dict = field(default_factory=lambda: dict(PROVIDER_PRESETS["deepseek"]["model_info"]))

    @classmethod
    def from_provider(cls, provider: str, api_key: str = "") -> "ModelConfig":
        """从 provider 预设构造 ModelConfig"""
        preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["deepseek"])
        return cls(
            base_url=preset["base_url"],
            main_model=preset["main_model"],
            api_key=api_key,
            model_info=dict(preset["model_info"]),
        )


def make_model_client(cfg: ModelConfig) -> OpenAIChatCompletionClient:
    """每请求独立构造 client，零全局状态

    重要：这个函数是纯函数（除了构造 client 对象外无副作用）。
    同一 cfg 多次调用会得到不同的 client 实例（OpenAIChatCompletionClient 内部有连接池等）。
    """
    kw: dict[str, Any] = {
        "model": cfg.main_model,
        "temperature": cfg.temperature,
        "timeout": cfg.timeout,
        "max_retries": cfg.max_retries,
        "model_info": cfg.model_info,
    }
    if cfg.api_key:
        kw["api_key"] = cfg.api_key
    if cfg.base_url:
        kw["base_url"] = cfg.base_url
    return OpenAIChatCompletionClient(**kw)
