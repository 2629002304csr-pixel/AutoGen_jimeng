"""DEPRECATED: v1 legacy. Use src/ (v2) for new work.

M16: 用户级模型配置（前端可编辑，存本地 JSON）

设计：
- 默认从 src.config.PROVIDER_PRESETS + 环境变量派生
- 通过 apply_user_config() 覆盖 src.config 模块级变量（运行时切换）
- 保存到 runs/_user_config.json（用户机器本地，plaintext key 可接受）

不在本文件做的事：
- 不直接调 LLM / 不读 src.config 顶层常量（避免循环导入）
- 不写 .env（.env 是开发者配置，不是用户配置）
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class UserConfig:
    """前端可配的模型设置

    字段留空 = 用 provider 预设的默认值；只有显式填了才覆盖。
    """
    provider: str = "deepseek"      # 预设名（deepseek/qwen/openai/custom）
    base_url: str = ""               # 覆盖 provider 默认 base_url
    light_model: str = ""            # 覆盖 provider 默认 light model（Host 用）
    main_model: str = ""             # 覆盖 provider 默认 main model（其他角色）
    api_key: str = ""                # 用户自己的 key（明文本地存）
    timeout: float = 60.0
    max_retries: int = 3
    # 高级（一般不暴露给前端简单模式）
    model_info_overrides: dict = field(default_factory=dict)


def _user_config_path(runs_dir: Path = Path("runs")) -> Path:
    """用户配置文件路径（runs/_user_config.json）

    放在 runs/ 下而不是项目根，因为：
    1. 用户的工作目录可能不是项目根（EXE 场景下没有"项目根"概念）
    2. runs/ 已经是一个明确的"运行时数据"目录，加一个隐藏文件合理
    """
    return runs_dir / "_user_config.json"


def load_user_config(runs_dir: Path = Path("runs")) -> UserConfig:
    """从 runs/_user_config.json 加载（无文件 → 返回默认 UserConfig）

    容错：
    - 文件不存在 → 默认
    - JSON 损坏 → 默认（不抛异常，让前端能正常工作）
    - 未知字段 → 过滤掉（防止新版写老字段时崩）
    """
    path = _user_config_path(runs_dir)
    if not path.exists():
        return UserConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return UserConfig()
    if not isinstance(data, dict):
        return UserConfig()
    # 过滤未知字段
    known = {
        k: v for k, v in data.items()
        if k in UserConfig.__dataclass_fields__
    }
    try:
        return UserConfig(**known)
    except TypeError:
        return UserConfig()


def save_user_config(config: UserConfig, runs_dir: Path = Path("runs")) -> Path:
    """保存到 runs/_user_config.json（原子写：先 .tmp 再 rename）

    原子写是为了防止保存中途崩溃时文件半损坏（下次 load 会 fallback 到默认）。
    """
    path = _user_config_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def mask_api_key(key: str) -> str:
    """前端展示用：sk-1234abcd → sk-****abcd

    设计：
    - 空字符串 → ""（前端用此判断"未填"）
    - ≤8 字符 → "****"（短 key 全遮）
    - 长 key → 前 3 + **** + 后 4
    """
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:3]}****{key[-4:]}"


def is_masked_key(s: str) -> bool:
    """检测字符串是不是 mask 后的 key（用于"前端传回 masked 值时识别为'未改'"）

    形态：
    - 长度 ≤ 4 + 全是 * → mask
    - 中间含 **** → mask
    """
    if not s:
        return False
    if set(s) == {"*"}:
        return True
    return "****" in s