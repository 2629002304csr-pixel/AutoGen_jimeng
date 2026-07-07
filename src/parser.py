"""自然语言用户输入解析器（M21 v2）

把一段用户文字解析为结构化 UserInput。失败抛 ParseError。

v2 改动（vs v1_legacy.parser）：
- 不再依赖 `from .config import LIGHT_MODEL, _client_kwargs, _get_api_key`
- 接收 `ModelConfig` 参数（per-request，无全局）
- 仍用 AsyncOpenAI 直接调用（parser 是简单的 chat completion，
  用 AutoGen client 反而复杂）
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from openai import AsyncOpenAI

from .config import ModelConfig
from .user_input import UserInput


# ============== Prompt 加载 ==============
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_PARSER_PROMPT: str = (_PROMPTS_DIR / "parser.md").read_text(encoding="utf-8")


# ============== 枚举白名单 ==============
_VALID_ASPECT_RATIOS = {"16:9", "9:16", "1:1", "2.39:1", "1.85:1"}
_VALID_QUALITY = {"4K", "6K", "8K"}
_VALID_COLOR_TONE = {"冷", "暖", "冷暖对比", "单色", "中性"}
_VALID_TEXTURE = {"胶片", "数字", "复古", "水彩"}
_VALID_FRAME_RATE = {24, 30, 60}
_VALID_LIGHTING_MOOD = {"暗调", "高调", "体积光", "逆光", "侧光"}
_VALID_MOOD = {"紧张", "温馨", "孤独", "治愈", "史诗", "悬疑"}
_VALID_NARRATION = {"无", "旁白", "对白"}


class ParseError(Exception):
    """解析失败时抛出，由 main.py / app.py 捕获并返回 4xx"""


# ============== JSON 兜底解析 ==============
def _safe_json_loads(text: str) -> dict:
    """从 LLM 输出中尽力提取 JSON 对象，失败抛 ParseError"""
    if not text:
        raise ParseError("LLM 返回为空")
    s = text.strip()

    # 1) 直接解析
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # 2) 去掉 markdown 包裹 ```json ... ``` / ``` ... ```
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass

    # 3) 提取第一个 {...} 块
    brace = re.search(r"\{.*\}", s, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass

    raise ParseError(f"无法从 LLM 输出解析 JSON: {s[:200]!r}")


# ============== 校验 ==============
def _validate(d: dict) -> UserInput:
    """校验 LLM 解析结果，构造 UserInput。失败抛 ParseError。"""
    if not isinstance(d, dict):
        raise ParseError(f"LLM 输出不是对象: {type(d).__name__}")

    # inspiration
    insp = d.get("inspiration") or ""
    if not isinstance(insp, str) or len(insp.strip()) < 5:
        raise ParseError("灵感太短，请至少描述一句完整的话")

    # duration
    try:
        duration = int(d.get("duration", 15))
    except (TypeError, ValueError):
        raise ParseError(f"duration 不是整数: {d.get('duration')!r}")
    if not (1 <= duration <= 60):
        raise ParseError(f"duration {duration} 越界，应在 1-60")

    # shot_count
    sc = d.get("shot_count")
    if sc is not None:
        try:
            sc = int(sc)
        except (TypeError, ValueError):
            raise ParseError(f"shot_count 不是整数: {sc!r}")
        if not (1 <= sc <= 10):
            raise ParseError(f"shot_count {sc} 越界，应为 None 或 1-10")

    # aspect_ratio
    ar = d.get("aspect_ratio") or "16:9"
    if ar not in _VALID_ASPECT_RATIOS:
        raise ParseError(f"aspect_ratio {ar!r} 不支持（白名单：{sorted(_VALID_ASPECT_RATIOS)}）")

    # 枚举字段校验
    def _enum(value, whitelist, field):
        if value is None:
            return None
        if value not in whitelist:
            raise ParseError(f"{field} {value!r} 不在白名单 {sorted(whitelist) if isinstance(whitelist, set) else whitelist} 内")
        return value

    quality = _enum(d.get("quality"), _VALID_QUALITY, "quality")
    color_tone = _enum(d.get("color_tone"), _VALID_COLOR_TONE, "color_tone")
    texture = _enum(d.get("texture"), _VALID_TEXTURE, "texture")
    lighting_mood = _enum(d.get("lighting_mood"), _VALID_LIGHTING_MOOD, "lighting_mood")
    mood = _enum(d.get("mood"), _VALID_MOOD, "mood")
    narration = _enum(d.get("narration"), _VALID_NARRATION, "narration")

    # frame_rate 单独处理（整数集合）
    fr = d.get("frame_rate")
    if fr is not None:
        try:
            fr = int(fr)
        except (TypeError, ValueError):
            raise ParseError(f"frame_rate 不是整数: {fr!r}")
        if fr not in _VALID_FRAME_RATE:
            raise ParseError(f"frame_rate {fr} 不在白名单 {sorted(_VALID_FRAME_RATE)} 内")

    # extra_constraints
    ec = d.get("extra_constraints") or []
    if not isinstance(ec, list):
        raise ParseError(f"extra_constraints 不是列表: {type(ec).__name__}")
    ec = [str(x).strip() for x in ec if str(x).strip()]

    # 自由文本字段（允许任意 string 或 None）
    def _str_or_none(v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    return UserInput(
        inspiration=insp.strip(),
        duration=duration,
        shot_count=sc,
        aspect_ratio=ar,
        style_hint=_str_or_none(d.get("style_hint")),
        quality=quality,
        color_tone=color_tone,
        texture=texture,
        frame_rate=fr,
        lighting_mood=lighting_mood,
        mood=mood,
        characters=_str_or_none(d.get("characters")),
        music_hint=_str_or_none(d.get("music_hint")),
        narration=narration,
        extra_constraints=ec,
    )


# ============== 主入口 ==============
async def parse_user_input(
    raw_text: str,
    *,
    model_cfg: ModelConfig | None = None,
    client: AsyncOpenAI | None = None,
) -> UserInput:
    """把自然语言解析为 UserInput。失败抛 ParseError。

    Args:
        raw_text: 用户输入的自然语言
        model_cfg: v2 用 ModelConfig 构造 client（per-request，无全局）。
            默认 None → 用 deepseek 预设构造（要求 api_key）。
        client: 可选注入（测试用）。

    Raises:
        ParseError: 输入为空 / 灵感太短 / 字段越界 / JSON 损坏 / LLM 调用失败
    """
    if not raw_text or not raw_text.strip():
        raise ParseError("输入为空，请提供至少一句灵感")

    # 构造 / 注入 client
    owns_client = False
    if client is None:
        if model_cfg is None:
            model_cfg = ModelConfig.from_provider("deepseek")
        if not model_cfg.api_key:
            raise ParseError("未配置 api_key（POST /api/v2/config 或请求 body 传入）")
        client = AsyncOpenAI(
            api_key=model_cfg.api_key,
            base_url=model_cfg.base_url or None,
            timeout=model_cfg.timeout,
            max_retries=model_cfg.max_retries,
        )
        owns_client = True
        model_name = model_cfg.main_model
    else:
        # 注入 client 时，从 model_cfg 或 fallback 取模型名
        model_name = (model_cfg.main_model if model_cfg else "deepseek-chat")

    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": _PARSER_PROMPT},
                {"role": "user", "content": raw_text},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
    except Exception as e:
        raise ParseError(f"LLM 调用失败: {e}") from e
    finally:
        if owns_client:
            await client.close()

    parsed = _safe_json_loads(content)
    return _validate(parsed)