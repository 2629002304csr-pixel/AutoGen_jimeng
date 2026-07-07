"""v2 FastAPI 入口（M21 + M22 扩展）

路由：
- GET  /                              静态首页
- GET  /api/v2/config                 读 runs/_user_config.json
- POST /api/v2/config                 保存 model config
- POST /api/v2/config/test            测试 model（ping 1+1=?）
- POST /api/v2/run                    Step 1: Host + Writer（HTTP 同步版）
- POST /api/v2/continue               Step 2: Storyboard + DP + Director（HTTP 同步版）
- GET  /api/v2/sessions               列出会话
- GET  /api/v2/sessions/{sid}         读取会话
- WS   /ws                            v2 WebSocket 流式入口（替代 v1_legacy，--web 用）

M22 Plan 2：把 WebSocket 接到 v2 WorkflowV2 上。
v2 架构修复（vs v1_legacy 的 SelectorGroupChat 含 5 agents）：
- RoundRobinGroupChat 物理上只放 [Host, Writer]
- Step 1 跑完 MaxMessageTermination(5) 强制停，绝不会"还没 review 就跑 Step 2"
"""
from __future__ import annotations

import json
import os
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import ModelConfig, PROVIDER_PRESETS, make_model_client
from .parser import ParseError, parse_user_input
from .session import Session
from .user_input import UserInput
from .workflow import WorkflowV2, build_task_message


app = FastAPI(title="AutoGen 即梦工作流 v2")

# ============== 路径常量 ==============
ROOT_DIR = Path(__file__).parent.parent
STATIC_DIR = ROOT_DIR / "static"
RUNS_DIR = Path("runs")
CONFIG_PATH = RUNS_DIR / "_user_config.json"


# ============== 工具函数 ==============
def _extract_episode_summary(writer_output: str) -> str:
    """从 writer_output 抽 ## 故事梗概 段前 200 字（M14 续集累积用）

    复刻 src/v1_legacy/web_server.py:_extract_episode_summary 的实现，
    避免 v2 import v1_legacy 私有函数。无 ## 故事梗概 段时返回空串。
    """
    import re
    m = re.search(r"##\s*故事梗概\s*\n+(.+?)(?=\n##|\Z)", writer_output, re.DOTALL)
    if not m:
        return ""
    return m.group(1).strip()[:200]


# ============== 启动钩子 ==============
@app.on_event("startup")
async def _startup() -> None:
    """确保 runs/ 和 static/ 目录存在"""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)


# ============== 静态文件 ==============
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root() -> FileResponse:
    """首页（如果存在 static/index.html）"""
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return FileResponse(ROOT_DIR / "README.md") if (ROOT_DIR / "README.md").exists() else _placeholder()


def _placeholder() -> FileResponse:
    """无静态文件时的占位页"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(
        "<h1>AutoGen 即梦工作流 v2</h1>"
        "<p>前端未配置。API 文档: <a href='/docs'>/docs</a></p>"
    )


# ============== 配置 ==============
@app.get("/api/v2/config")
async def get_config() -> dict:
    """读用户配置（per-request，无全局）"""
    if not CONFIG_PATH.exists():
        return ModelConfig.from_provider("deepseek").__dict__
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ModelConfig.from_provider("deepseek").__dict__


@app.post("/api/v2/config")
async def save_config(payload: dict) -> dict:
    """保存 model config

    Body 字段：
        provider: deepseek / qwen / openai（可选）
        base_url: 端点（可选，省略用 provider 默认）
        api_key: 必须
        main_model: 模型名（可选，省略用 provider 默认）
    """
    api_key = (payload.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key 不能为空")

    provider = (payload.get("provider") or "deepseek").lower()
    if provider not in PROVIDER_PRESETS:
        raise HTTPException(400, f"未知 provider: {provider}")
    preset = PROVIDER_PRESETS[provider]

    cfg = ModelConfig(
        base_url=(payload.get("base_url") or preset["base_url"]).strip(),
        api_key=api_key,
        main_model=(payload.get("main_model") or preset["main_model"]).strip(),
        model_info=dict(preset["model_info"]),
    )
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg.__dict__, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # 注意：不回写 api_key 到响应（避免泄露日志）
    safe = cfg.__dict__.copy()
    safe["api_key"] = "***" + cfg.api_key[-4:] if cfg.api_key else ""
    return {"ok": True, "config": safe}


@app.post("/api/v2/config/test")
async def test_config(payload: dict | None = None) -> dict:
    """用指定配置 ping 1+1=?

    Body 可省略 → 用文件中的 config；提供则覆盖文件 config。

    M27+：testing 走快速路径 —— max_tokens=5 + 8s 硬上限 + 临时短 timeout，
    避免 reasoning 模型（deepseek-r1/qwen3-thinking）首启冷启动拖到 30+s。
    """
    import asyncio
    cfg = _resolve_model_config(payload.get("model_config") if payload else None)
    if not cfg.api_key:
        return {"ok": False, "error": "未配置 api_key"}
    # 测试用短超时（不影响主流程 ModelConfig 默认 180s）
    test_cfg = ModelConfig(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        main_model=cfg.main_model,
        model_info=dict(cfg.model_info),
        timeout=15.0,
        max_retries=1,
    )
    try:
        client = make_model_client(test_cfg)
        from autogen_core.models import UserMessage
        # autogen-ext 的 create() 不接 max_tokens 直接传，要走 extra_create_args
        result = await asyncio.wait_for(
            client.create(
                [UserMessage(content="1+1=?", source="user")],
                extra_create_args={"max_tokens": 5},
            ),
            timeout=8.0,
        )
        text = str(getattr(result, "content", ""))[:100]
        return {"ok": True, "sample": text, "model": cfg.main_model}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "测试超时（>8s），可能网络慢或模型在冷启动；实际工作流 timeout=180s，请直接跑"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}


# ============== Step 1 ==============
@app.post("/api/v2/run")
async def run_step1(payload: dict) -> dict:
    """Step 1: Host + Writer（物理上不可能跑 Storyboard/DP/Director）

    Body 字段：
        raw_text: 用户输入（自然语言）
        model_config: 可选 ModelConfig dict（覆盖文件配置）
    """
    raw_text = (payload.get("raw_text") or "").strip()
    if not raw_text:
        raise HTTPException(400, "raw_text 不能为空")

    cfg = _resolve_model_config(payload.get("model_config"))

    # 解析用户输入
    try:
        user = await parse_user_input(raw_text, model_cfg=cfg)
    except ParseError as e:
        raise HTTPException(400, f"参数解析失败: {e}")

    # 跑 Step 1
    workflow = WorkflowV2(model_cfg=cfg)
    task = build_task_message(user, raw_text=raw_text)
    result = await workflow.step1(task)

    # 保存 session（create 自动生成 session_id）
    session = Session.create(user, runs_dir=RUNS_DIR)
    for m in result["messages"]:
        session.add_message(getattr(m, "source", "?"), _msg_text(m))
    # writer_output.md
    run_dir = RUNS_DIR / session.session_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "writer_output.md").write_text(
        result["writer_output"] or "(无 Writer 输出)",
        encoding="utf-8",
    )
    session.save(RUNS_DIR)

    return {
        "session_id": session.session_id,
        "writer_output": result["writer_output"],
        "messages": [_msg_dict(m) for m in result["messages"]],
        "user_input": _user_input_dict(user),
    }


# ============== Step 2 ==============
@app.post("/api/v2/continue")
async def run_step2(payload: dict) -> dict:
    """Step 2: Storyboard + DP + Director

    Body 字段：
        session_id: 必须
        user_addition: 可选（用户 review 时输入的反馈）
        model_config: 可选 ModelConfig dict
    """
    sid = (payload.get("session_id") or "").strip()
    if not sid:
        raise HTTPException(400, "session_id 不能为空")
    try:
        session = Session.load(sid, runs_dir=RUNS_DIR)
    except FileNotFoundError:
        raise HTTPException(404, f"session {sid} 不存在")

    cfg = _resolve_model_config(payload.get("model_config"))
    user_addition = (payload.get("user_addition") or "").strip()

    # 重建 history 为 TextMessage
    from autogen_agentchat.messages import TextMessage
    history = [
        TextMessage(content=m["content"], source=m["role"])
        for m in session.messages
        if m.get("content")
    ]

    # 跑 Step 2
    workflow = WorkflowV2(model_cfg=cfg)
    result = await workflow.step2(history, user_addition=user_addition)

    # 追加 Step 2 消息到 session（去重：跳过 history 里已有的）
    history_ids = {id(m) for m in history}
    for m in result["messages"]:
        if id(m) in history_ids:
            continue
        session.add_message(getattr(m, "source", "?"), _msg_text(m))
    session.save(RUNS_DIR)

    # 保存 jimeng_prompt.md
    run_dir = RUNS_DIR / sid
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "jimeng_prompt.md").write_text(
        result["jimeng_prompt"] or "(no prompt produced)",
        encoding="utf-8",
    )

    return {
        "session_id": sid,
        "jimeng_prompt": result["jimeng_prompt"],
        "messages": [_msg_dict(m) for m in result["messages"]],
    }


# ============== 会话 ==============
@app.get("/api/v2/sessions")
async def list_sessions() -> list[dict]:
    """列出所有 session（按 session_id 倒序，最新在前）"""
    return _list_sessions_impl()


@app.get("/api/sessions")
async def list_sessions_compat() -> list[dict]:
    """兼容老前端：同样列出 session，但带 title / parent_session_id / has_fact_sheet 字段"""
    return _list_sessions_impl(include_extras=True)


@app.get("/api/v2/sessions/{sid}")
async def get_session(sid: str) -> dict:
    """读取单个 session 详情"""
    try:
        session = Session.load(sid, runs_dir=RUNS_DIR)
    except FileNotFoundError:
        raise HTTPException(404, f"session {sid} 不存在")
    run_dir = RUNS_DIR / sid
    return {
        "session_id": sid,
        "current_step": session.current_step,
        "current_episode": session.current_episode,
        "user_input": session.user_input_dict,
        "messages": session.messages,
        "writer_output": _maybe_read(run_dir / "writer_output.md"),
        "jimeng_prompt": _maybe_read(run_dir / "jimeng_prompt.md"),
        "fact_sheet": session.load_fact_sheet(sid, runs_dir=RUNS_DIR),
    }


@app.get("/api/sessions/{sid}")
async def get_session_compat(sid: str) -> dict:
    """兼容老前端：返回 v1_legacy 风格的字段（prompts 列表 + jimeng_prompt + fact_sheet_dict 等）"""
    from fastapi import HTTPException
    state_path = RUNS_DIR / sid / "state.json"
    if not state_path.exists():
        raise HTTPException(404, f"session {sid} 不存在")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    run_dir = RUNS_DIR / sid

    writer_output = _maybe_read(run_dir / "writer_output.md")
    jimeng_prompt = _maybe_read(run_dir / "jimeng_prompt.md")
    fact_sheet = None
    fs_path = run_dir / "fact_sheet.json"
    if fs_path.exists():
        try:
            fact_sheet = json.loads(fs_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            fact_sheet = None

    return {
        "session_id": sid,
        "current_step": state.get("current_step", "start"),
        "current_episode": state.get("current_episode", 1),
        "current_inspiration": state.get("current_inspiration", ""),
        "user_input": state.get("user_input_dict", {}),
        "messages": state.get("messages", []),
        "writer_output": writer_output,
        "fact_sheet": fact_sheet,
        "prompts": _load_prompts_for_session(run_dir),
        "jimeng_prompt": jimeng_prompt,
        "parent_session_id": state.get("parent_session_id"),
        "created_at": state.get("created_at"),
    }


# ============== 删除 session ==============
_RUNNING_STATES = ("running_step1", "running_step2", "running_rewrite")


def _delete_session_impl(sid: str) -> dict:
    """删除 session 目录。共享逻辑：/api/v2/sessions/{sid} + /api/sessions/{sid}

    闸：
    - 不存在 → 404
    - state ∈ running_* → 409（防止删除正在跑的 session 导致状态错乱）
    - 否则 → rmtree + return ok
    """
    from src.session import Session
    run_dir = RUNS_DIR / sid
    if not run_dir.exists():
        raise HTTPException(404, f"session {sid} 不存在")
    # 闸：running 状态拒绝删除
    state_path = run_dir / "state.json"
    cur = ""
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            cur = state.get("current_step", "")
        except (json.JSONDecodeError, OSError):
            # 解析失败按"非 running"放过，避免阻塞用户清理
            cur = ""
    if cur in _RUNNING_STATES:
        raise HTTPException(
            409,
            f"session {sid} 正在运行（state={cur}），无法删除",
        )
    Session.delete(RUNS_DIR, sid)
    return {"ok": True, "deleted": sid}


@app.delete("/api/v2/sessions/{sid}")
async def delete_session(sid: str) -> dict:
    return _delete_session_impl(sid)


@app.delete("/api/sessions/{sid}")
async def delete_session_compat(sid: str) -> dict:
    return _delete_session_impl(sid)


# ============== WebSocket（v2 干净版，M22 Plan 2）==============
class _WSChannel:
    """包装 fastapi.WebSocket —— send() 捕获异常（断连时不让 workflow 崩）"""
    def __init__(self, websocket: WebSocket):
        self.ws = websocket
        self.session_id: str | None = None

    async def send(self, msg: dict) -> None:
        try:
            await self.ws.send_json(msg)
        except Exception:
            pass


async def _on_token_factory(ws: _WSChannel):
    """构造闭包：把 (role, content, is_token) 推成 {token|message} 消息"""
    async def on_event(role: str, content: str, is_token: bool) -> None:
        try:
            await ws.send({
                "type": "token" if is_token else "message",
                "role": role,
                "content": content,
            })
        except Exception:
            pass
    return on_event


async def _save_step1_artifacts(
    session: Session,
    messages: list,
    writer_output: str,
    runs_dir: Path,
) -> None:
    """Step 1 产物：writer_output.md + transcript.md + user_input.json"""
    run_dir = runs_dir / session.session_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "user_input.json").write_text(
        json.dumps(session.user_input_dict, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "writer_output.md").write_text(
        writer_output or "(无 Writer 输出)", encoding="utf-8"
    )
    transcript_parts = []
    for m in messages:
        src = getattr(m, "source", "?")
        text = _msg_text(m)
        transcript_parts.append(f"=== {src} ===\n{text}\n")
    (run_dir / "transcript.md").write_text(
        "\n".join(transcript_parts), encoding="utf-8"
    )


async def _save_step2_artifacts(
    session: Session,
    messages: list,
    prompt: str,
    runs_dir: Path,
    ep_id: int | None = None,
    inspiration: str = "",
) -> None:
    """Step 2 产物：jimeng_prompt.md + 更新 transcript + 按 ep_id 持久化 prompt

    M22+：每集的最终脚本必须保留，不能被新一集覆盖。
    新增 `prompts/<ep_id>.md` + `prompts_index.json`，让前端 tabs 能读到全集。
    """
    ep_id = ep_id if ep_id is not None else session.current_episode
    run_dir = runs_dir / session.session_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # M22+：jimeng_prompt.md 改为"本集最新"的 quick view（仍覆盖，保持向后兼容）
    # 但全集历史走 prompts/<ep_id>.md + prompts_index.json
    (run_dir / "jimeng_prompt.md").write_text(
        prompt or "(no prompt produced)", encoding="utf-8"
    )

    # M22+：本集 prompt 写到 prompts/<ep_id>.md
    prompts_dir = run_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / f"{ep_id:03d}.md").write_text(
        prompt or "(no prompt produced)", encoding="utf-8"
    )

    # M22+：追加本集条目到 prompts_index.json（保留全集列表）
    index_path = run_dir / "prompts_index.json"
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            index = {"episodes": []}
    else:
        index = {"episodes": []}
    # 替换同 ep_id（重写时）→ 否则追加
    index["episodes"] = [
        e for e in index.get("episodes", []) if e.get("ep_id") != ep_id
    ]
    index["episodes"].append({
        "ep_id": ep_id,
        "title": f"集 {ep_id}",
        "inspiration": inspiration[:80],
        "created_at": datetime.now().isoformat(),
    })
    index["episodes"].sort(key=lambda e: e.get("ep_id", 0))
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    transcript_parts = []
    for m in messages:
        src = getattr(m, "source", "?")
        text = _msg_text(m)
        transcript_parts.append(f"=== {src} ===\n{text}\n")
    (run_dir / "transcript.md").write_text(
        "\n".join(transcript_parts), encoding="utf-8"
    )


async def handle_start_ws(ws: _WSChannel, msg: dict[str, Any]) -> None:
    """{type:"start", raw_text, model_config?, session_id?, parent_session_id?} → 跑 Step 1

    M22+ 3 分支：
    1. session_id 续接（M13 同 session 续下一集）：复用同 session_id，
       load 后 current_step 必须是 'complete'，user 复用，session_id 不变
    2. parent_session_id fork（M12 旧版续集）：建新 session，parent 指向 parent
    3. 首集：建全新 session

    所有分支最后都把 status='after_writer' 的 session_id 发回去 ——
    续接路径必须 == target_sid（前端 activeSid 不切，流式 bubble 不被 wipe）。
    """
    raw_text = (msg.get("raw_text") or "").strip()
    if not raw_text:
        await ws.send({"type": "error", "message": "raw_text 不能为空"})
        return

    cfg = _resolve_model_config(msg.get("model_config"))
    target_sid: str | None = msg.get("session_id") or None
    parent_session_id: str | None = msg.get("parent_session_id") or None

    session: Session | None = None
    parent_fact_sheet: dict | None = None
    current_inspiration: str = ""
    user: UserInput

    # ===== 分支 1：target_sid 续接（M13 同 session 续下一集）=====
    if target_sid:
        try:
            session = Session.load(target_sid, runs_dir=RUNS_DIR)
        except FileNotFoundError:
            await ws.send({"type": "error", "message": f"session_id={target_sid} 不存在"})
            return
        # 闸：state 必须是 'complete'（step 2 跑完才能续下一集）
        if session.current_step != "complete":
            await ws.send({
                "type": "error",
                "message": (
                    f"session {target_sid} 当前状态 {session.current_step!r}，"
                    "仅 complete 状态可续接（先跑完上集 Step 2）"
                ),
            })
            return
        user = session.get_user_input()
        session.set_current_inspiration(raw_text)
        session.save(RUNS_DIR)  # 先存（万一 step1 出错，state.json 至少有 inspiration）
        parent_fact_sheet = session.fact_sheet_dict
        current_inspiration = raw_text

    # ===== 分支 2：parent_session_id fork（M12 旧版续集）=====
    elif parent_session_id:
        try:
            parent = Session.load(parent_session_id, runs_dir=RUNS_DIR)
        except FileNotFoundError:
            await ws.send({
                "type": "error",
                "message": f"parent_session_id={parent_session_id} 不存在",
            })
            return
        parent_fact_sheet = parent.fact_sheet_dict
        if not parent_fact_sheet:
            await ws.send({
                "type": "error",
                "message": (
                    f"parent_session_id={parent_session_id} 无 fact_sheet "
                    "（请先跑完父 session 的 step 2）"
                ),
            })
            return
        try:
            user = await parse_user_input(raw_text, model_cfg=cfg)
        except ParseError as e:
            await ws.send({"type": "error", "message": f"参数解析失败: {e}"})
            return
        current_inspiration = raw_text

    # ===== 分支 3：首集 =====
    else:
        try:
            user = await parse_user_input(raw_text, model_cfg=cfg)
        except ParseError as e:
            await ws.send({"type": "error", "message": f"参数解析失败: {e}"})
            return
        current_inspiration = raw_text

    # 跑 Step 1（流式）
    on_event = await _on_token_factory(ws)
    await ws.send({"type": "status", "state": "running_step1"})

    workflow = WorkflowV2(model_cfg=cfg)
    task = build_task_message(
        user,
        raw_text=raw_text,
        parent_fact_sheet=parent_fact_sheet,
        current_inspiration=current_inspiration,
    )
    result = await workflow.step1_stream(task, on_token=on_event, on_message=on_event)
    messages = result["messages"]
    writer_output = result["writer_output"]

    # 建/复用 session
    if session is None:
        # 首集 或 parent fork：新建
        session = Session.create(
            user,
            runs_dir=RUNS_DIR,
            parent_session_id=parent_session_id,
        )
        if current_inspiration:
            session.set_current_inspiration(current_inspiration)
    # else：target_sid 续接路径，session 已在上面 load 出来 → 沿用 session_id
    #      注：set_current_inspiration 已在分支 1 调过

    for m in messages:
        session.add_message(getattr(m, "source", "?"), _msg_text(m))
    session.current_step = "after_writer"
    session.save(RUNS_DIR)
    ws.session_id = session.session_id  # 续接时 == target_sid

    # 写产物（M22：writer_output 不空也要显式落盘——前端拿 ws 字段，文件给人工编辑用）
    await _save_step1_artifacts(session, messages, writer_output, RUNS_DIR)

    # M13：首集从 writer_output 抽 fact_sheet；续集 / fork 路径 parent_fact_sheet
    # 已注入 Writer 输入，Writer 输出本身就是续集上下文 → 同样 extract 一次 merge
    fs = session.save_fact_sheet(writer_output, RUNS_DIR)
    if fs is not None:
        session.save(RUNS_DIR)

    # 关键：session_id 必须是 session.session_id（同 session 续接时 == target_sid）
    await ws.send({
        "type": "status",
        "state": "after_writer",
        "session_id": session.session_id,
        "current_episode": session.current_episode,
        "writer_output": writer_output,
        "has_fact_sheet": fs is not None,
        "message_count": len(messages),
    })


async def handle_continue_ws(ws: _WSChannel, msg: dict[str, Any]) -> None:
    """{type:"continue", session_id} → 跑 Step 2

    M22 双闸（后端闸）：current_step != "after_writer" → 拒；
    Writer 输出不通过 validator → 拒。
    """
    target_sid = msg.get("session_id") or ws.session_id
    if not target_sid:
        await ws.send({"type": "error", "message": "no active session"})
        return

    try:
        session = Session.load(target_sid, runs_dir=RUNS_DIR)
    except FileNotFoundError:
        await ws.send({"type": "error", "message": f"session_id={target_sid} 不存在"})
        return

    user = session.get_user_input()

    # 闸一：state 必须 after_writer
    if session.current_step != "after_writer":
        await ws.send({
            "type": "error",
            "message": (
                f"session {target_sid} 当前状态 {session.current_step!r}，"
                "必须先跑完 Step 1（state=after_writer）才能继续 Step 2"
            ),
        })
        return

    # 闸二：Writer 输出必须有结构化内容
    from .validator import validate_output  # 复用 v1_legacy 的 validator
    writer_msgs = [
        m for m in session.messages
        if m.get("role") == "Writer"
        and m.get("ep_id") == session.current_episode
    ]
    if not writer_msgs:
        await ws.send({
            "type": "error",
            "message": "Writer 没有任何消息记录，请先跑 Step 1 让 Writer 输出内容",
        })
        return
    last_writer = writer_msgs[-1].get("content", "")
    ok, errs = validate_output("Writer", last_writer, user)
    if not ok:
        await ws.send({
            "type": "error",
            "message": (
                f"Writer 输出未通过结构校验：{'; '.join(errs[:3])}。"
                "请先让 Writer 重写完整内容"
            ),
            "validation_errors": errs,
        })
        return

    # 同步用户在文件系统中编辑的 writer_output.md（v1 行为）
    session.sync_writer_output_from_file(RUNS_DIR)

    # 重建 history → TextMessage
    from autogen_agentchat.messages import TextMessage
    history = [
        TextMessage(content=m["content"], source=m["role"])
        for m in session.messages
        if m.get("content") and m.get("ep_id") == session.current_episode
    ]

    on_event = await _on_token_factory(ws)
    await ws.send({"type": "status", "state": "running_step2", "max_rounds": 3})

    cfg = _resolve_model_config(msg.get("model_config"))
    workflow = WorkflowV2(model_cfg=cfg)
    result = await workflow.step2_with_revision(
        history, user_addition="",
        max_rounds=3,
        on_token=on_event, on_message=on_event,
    )
    messages = result["messages"]
    jimeng_prompt = result["jimeng_prompt"]
    rounds = result["rounds"]
    director_status = result["director_status"]

    # 追加 Step 2 的新消息到 session
    history_ids = {id(m) for m in history}
    for m in messages:
        if id(m) in history_ids:
            continue
        session.add_message(getattr(m, "source", "?"), _msg_text(m))
    session.current_step = "complete"

    # M13/M14：merge 上集 fact_sheet + 追加本集 story_arc
    # 必须在 advance_episode() 之前（保证 ep_id 用于 update 时是当前 ep）
    from .v1_legacy.fact_sheet import update_fact_sheet_after_episode
    episode_summary = _extract_episode_summary(last_writer)
    fs = update_fact_sheet_after_episode(
        old_fact_sheet=session.fact_sheet_dict,
        new_writer_output=last_writer,
        new_jimeng_prompt=jimeng_prompt,
        episode_id=session.current_episode,
        episode_summary=episode_summary,
    )
    session.save_fact_sheet(fact_sheet=fs, runs_dir=RUNS_DIR)

    session.advance_episode()
    session.save(RUNS_DIR)

    # 写产物（ep_id 必须用 advance 前的本集 ep_id，因为 advance 后 current_episode 已 +1）
    await _save_step2_artifacts(
        session, messages, jimeng_prompt, RUNS_DIR,
        ep_id=session.current_episode - 1,
        inspiration=session.current_inspiration,
    )

    await ws.send({
        "type": "result",
        "session_id": target_sid,
        "prompt": jimeng_prompt,
        "ep_id": session.current_episode - 1,
        "rounds": rounds,
        "director_status": director_status,
        "message_count": len(messages),
        "run_dir": str(RUNS_DIR / target_sid),
    })


async def handle_rewrite_ws(ws: _WSChannel, msg: dict[str, Any]) -> None:
    """{type:"rewrite", session_id, feedback} → 让 Writer 用反馈重写

    v2：复用 step1b_rewrite_stream（移除原 Writer 消息，只重跑 Writer 看到用户反馈）
    """
    feedback = (msg.get("feedback") or "").strip()
    target_sid = msg.get("session_id") or ws.session_id
    if not feedback:
        await ws.send({"type": "error", "message": "feedback 不能为空"})
        return
    if not target_sid:
        await ws.send({"type": "error", "message": "no active session"})
        return

    try:
        session = Session.load(target_sid, runs_dir=RUNS_DIR)
    except FileNotFoundError:
        await ws.send({"type": "error", "message": f"session_id={target_sid} 不存在"})
        return

    user = session.get_user_input()
    session.set_rewrite_feedback(feedback)
    session.save(RUNS_DIR)

    # 重建 history
    from autogen_agentchat.messages import TextMessage
    history = [
        TextMessage(content=m["content"], source=m["role"])
        for m in session.messages
        if m.get("content") and m.get("ep_id") == session.current_episode
    ]

    on_event = await _on_token_factory(ws)
    await ws.send({"type": "status", "state": "running_rewrite"})

    cfg = _resolve_model_config(msg.get("model_config"))
    workflow = WorkflowV2(model_cfg=cfg)
    result = await workflow.step1b_rewrite_stream(
        history, feedback, on_token=on_event, on_message=on_event
    )
    new_writer_output = result["writer_output"]

    if not new_writer_output:
        await ws.send({"type": "error", "message": "Writer 未返回新内容"})
        return

    # 替换本集 ep_id 的 Writer 消息（不替换其他集）
    replaced = False
    for i, m in enumerate(session.messages):
        if m["role"] == "Writer" and m.get("ep_id") == session.current_episode:
            session.messages[i]["content"] = new_writer_output
            replaced = True
            break
    if not replaced:
        session.add_message("Writer", new_writer_output)

    session.current_step = "after_writer"

    # M14：重写后用 update（merge + 替换 story_arc 同 ep_id 条目）而不是 extract
    from .v1_legacy.fact_sheet import update_fact_sheet_after_episode
    episode_summary = _extract_episode_summary(new_writer_output)
    fs = update_fact_sheet_after_episode(
        old_fact_sheet=session.fact_sheet_dict,
        new_writer_output=new_writer_output,
        new_jimeng_prompt="",
        episode_id=session.current_episode,
        episode_summary=episode_summary,
    )
    session.save_fact_sheet(fact_sheet=fs, runs_dir=RUNS_DIR)

    session.save(RUNS_DIR)

    # 落 writer_output.md（不重跑 transcript，避免把失败中间步骤也写进去）
    run_dir = RUNS_DIR / target_sid
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "writer_output.md").write_text(
        new_writer_output, encoding="utf-8"
    )

    await ws.send({
        "type": "status",
        "state": "after_writer",
        "session_id": target_sid,
        "current_episode": session.current_episode,
        "writer_output": new_writer_output,
        "rewritten": True,
        "has_fact_sheet": True,
        "message_count": len(result["messages"]),
    })


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """v2 WebSocket 入口 — 替代 v1_legacy/web_server.app

    协议（与 v1_legacy 完全一致，前端无需改）：
    客户端 → 服务端：
      {"type": "start",     "raw_text": "...", "model_config"?: {...}}
      {"type": "continue",  "session_id"?: "..."}
      {"type": "rewrite",   "session_id"?: "...", "feedback": "..."}

    服务端 → 客户端：
      {"type": "token",    "role": "...", "content": "..."}
      {"type": "message",  "role": "...", "content": "..."}
      {"type": "status",   "state": "running_step1"|"after_writer"|"running_step2"|"running_rewrite", ...}
      {"type": "result",   "session_id": "...", "prompt": "...", "ep_id": N, ...}
      {"type": "error",    "message": "..."}
    """
    await websocket.accept()
    ws = _WSChannel(websocket)
    try:
        async for raw in websocket.iter_text():
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send({"type": "error", "message": f"invalid JSON: {raw[:100]}"})
                continue
            t = payload.get("type")
            try:
                if t == "start":
                    await handle_start_ws(ws, payload)
                elif t == "continue":
                    await handle_continue_ws(ws, payload)
                elif t == "rewrite":
                    await handle_rewrite_ws(ws, payload)
                else:
                    await ws.send({"type": "error", "message": f"unknown type: {t}"})
            except FileNotFoundError as e:
                await ws.send({"type": "error", "message": str(e)})
            except Exception as e:
                await ws.send({
                    "type": "error",
                    "message": f"{type(e).__name__}: {e}",
                    "trace": traceback.format_exc(limit=3),
                })
    except WebSocketDisconnect:
        pass
    except RuntimeError as e:
        # starlette 在 accept() 之前或之后断开时可能抛 RuntimeError
        # （如 "WebSocket is not connected. Need to call accept first."）
        # 不要让这种客户端断连把 ASGI 整个崩了 — 静默退出
        if "not connected" in str(e) or "accept" in str(e):
            return
        raise


# ============== 兼容老前端的 routes（/api/config, /api/config/test, /api/sessions/*） ==============
# 老前端（static/index.html）的 M12-M16 代码假设这些老路径还在。
# v2 主路由是 /api/v2/*；这里补齐 /api/* 别名让前端不用改。

def _list_sessions_impl(include_extras: bool = False) -> list[dict]:
    """扫 RUNS_DIR 列出 session。

    Args:
        include_extras: True 时返回老前端字段（title / parent_session_id / has_fact_sheet），
                        v2 简洁版只返回 sid / step / episode / 时间。
    """
    if not RUNS_DIR.exists():
        return []
    items: list[dict] = []
    for run_dir in RUNS_DIR.iterdir():
        if not run_dir.is_dir():
            continue
        state_path = run_dir / "state.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        sid = state.get("session_id") or run_dir.name
        created_at = state.get("created_at", "")
        current_step = state.get("current_step", "start")
        current_episode = state.get("current_episode", 1)
        current_inspiration = state.get("current_inspiration", "")

        item: dict = {
            "session_id": sid,
            "current_step": current_step,
            "current_episode": current_episode,
            "created_at": created_at,
            "updated_at": state.get("updated_at", ""),
        }
        if include_extras:
            fs_path = run_dir / "fact_sheet.json"
            has_fact_sheet = fs_path.exists()
            # title 优先级：fact_sheet.title > user_input.inspiration[:20] > session_id
            title = sid
            if has_fact_sheet:
                try:
                    fs = json.loads(fs_path.read_text(encoding="utf-8"))
                    if fs.get("title"):
                        title = fs["title"]
                except (json.JSONDecodeError, OSError):
                    pass
            if title == sid:
                user_input = state.get("user_input_dict") or {}
                insp = user_input.get("inspiration", "")
                if isinstance(insp, str) and insp:
                    title = insp[:20] + ("…" if len(insp) > 20 else "")
            item.update({
                "title": title,
                "parent_session_id": state.get("parent_session_id"),
                "current_inspiration": current_inspiration,
                "has_fact_sheet": has_fact_sheet,
            })
        items.append(item)

    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items


def _load_prompts_for_session(run_dir: Path) -> list[dict]:
    """M13 兼容：读 session 的 prompts 列表，每集 1 个 tab。

    新格式：prompts_index.json + prompts/<ep_id>.md
    旧格式降级：jimeng_prompt.md → 1 项
    """
    index_path = run_dir / "prompts_index.json"
    prompts_dir = run_dir / "prompts"
    prompts: list[dict] = []
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            for ep in sorted(index.get("episodes", []), key=lambda e: e.get("ep_id", 0)):
                ep_id = ep.get("ep_id")
                ep_file = prompts_dir / f"{ep_id:03d}.md"
                if not ep_file.exists():
                    continue
                inspiration = ep.get("inspiration", "")
                prompts.append({
                    "ep_id": ep_id,
                    "title": ep.get("title") or f"集 {ep_id}",
                    "inspiration_preview": inspiration[:15],
                    "content": ep_file.read_text(encoding="utf-8"),
                    "created_at": ep.get("created_at"),
                })
        except (json.JSONDecodeError, OSError):
            pass

    if not prompts:
        legacy = run_dir / "jimeng_prompt.md"
        if legacy.exists():
            content = legacy.read_text(encoding="utf-8").strip()
            if content and not content.startswith("(no"):
                prompts.append({
                    "ep_id": 1,
                    "title": "集 1",
                    "inspiration_preview": "",
                    "content": content,
                    "created_at": None,
                })
    return prompts


# ============== /api/config（兼容老前端 M16） ==============
def _load_user_config() -> dict:
    """读 runs/_user_config.json，返字典（含 mask api_key）。

    容错：文件不存在/JSON 损坏 → 返回默认配置。
    """
    from dataclasses import asdict
    from .v1_legacy.user_config import UserConfig, mask_api_key, load_user_config
    cfg = load_user_config(RUNS_DIR)
    data = asdict(cfg)
    # mask api_key 给前端展示用
    data["api_key"] = mask_api_key(cfg.api_key)
    return data


def _save_user_config_from_payload(payload: dict) -> dict:
    """从 POST body 保存 UserConfig，保留未提供的 api_key。

    Returns:
        完整 dict（masked api_key）用于响应
    """
    from dataclasses import asdict
    from .v1_legacy.user_config import (
        UserConfig,
        is_masked_key,
        load_user_config,
        mask_api_key,
        save_user_config,
    )

    if not isinstance(payload, dict):
        raise HTTPException(400, "payload 必须是 JSON object")

    existing = load_user_config(RUNS_DIR)
    raw_key = (payload.get("api_key") or "").strip()
    if not raw_key or is_masked_key(raw_key):
        new_key = existing.api_key
    else:
        new_key = raw_key

    cfg = UserConfig(
        provider=(payload.get("provider") or existing.provider or "deepseek").lower(),
        base_url=(payload.get("base_url") or existing.base_url or "").strip(),
        light_model=(payload.get("light_model") or existing.light_model or "").strip(),
        main_model=(payload.get("main_model") or existing.main_model or "").strip(),
        api_key=new_key,
        timeout=float(payload.get("timeout") or existing.timeout or 60.0),
        max_retries=int(payload.get("max_retries") or existing.max_retries or 3),
        model_info_overrides=dict(existing.model_info_overrides or {}),
    )
    save_user_config(cfg, RUNS_DIR)
    data = asdict(cfg)
    data["api_key"] = mask_api_key(cfg.api_key)
    return data


@app.get("/api/config")
async def get_config_compat() -> dict:
    """M16 兼容：返回当前 UserConfig（masked）"""
    return _load_user_config()


@app.post("/api/config")
async def set_config_compat(payload: dict) -> dict:
    """M16 兼容：保存 UserConfig；空 api_key / mask api_key → 保留现有。"""
    data = _save_user_config_from_payload(payload)
    return {"ok": True, "config": data}


@app.post("/api/config/test")
async def test_config_compat(payload: dict | None = None) -> dict:
    """M16 兼容：用 payload 配置调 LLM 一次，返回 {ok, sample|error}。

    把 UserConfig 转 v2 ModelConfig（v2 已不用 light_model / timeout，但兼容调用）。
    """
    from .config import make_model_client
    from .v1_legacy.user_config import UserConfig, is_masked_key, load_user_config

    existing = load_user_config(RUNS_DIR)
    raw_key = ""
    if payload and isinstance(payload, dict):
        raw_key = (payload.get("api_key") or "").strip()
    new_key = (
        raw_key
        if (raw_key and not is_masked_key(raw_key))
        else existing.api_key
    )
    cfg = UserConfig(
        provider=(payload.get("provider") or existing.provider or "deepseek").lower() if payload else existing.provider,
        base_url=(payload.get("base_url") or existing.base_url or "") if payload else existing.base_url,
        light_model=(payload.get("light_model") or existing.light_model or "") if payload else existing.light_model,
        main_model=(payload.get("main_model") or existing.main_model or "") if payload else existing.main_model,
        api_key=new_key,
        timeout=30.0,
        max_retries=1,
    )

    if not cfg.api_key:
        return {
            "ok": False,
            "error": "未配置 API key，请在「⚙️ 模型设置」中填入",
            "config": _load_user_config(),
        }

    # v1 light_model 在 v2 是 main_model（v2 不区分大小模型）
    from dataclasses import asdict
    from .config import PROVIDER_PRESETS
    preset = PROVIDER_PRESETS.get(cfg.provider, PROVIDER_PRESETS["deepseek"])
    model_cfg = ModelConfig(
        base_url=cfg.base_url or preset["base_url"],
        api_key=cfg.api_key,
        main_model=cfg.main_model or cfg.light_model or preset["main_model"],
        model_info=dict(preset["model_info"]),
    )

    try:
        # M27+：testing 走快速路径 —— max_tokens=5 + 8s 硬上限 + 短 timeout
        import asyncio
        test_model_cfg = ModelConfig(
            base_url=model_cfg.base_url,
            api_key=model_cfg.api_key,
            main_model=model_cfg.main_model,
            model_info=dict(model_cfg.model_info),
            timeout=15.0,
            max_retries=1,
        )
        client = make_model_client(test_model_cfg)
        from autogen_core.models import UserMessage
        # autogen-ext 的 create() 不接 max_tokens 直接传，要走 extra_create_args
        result = await asyncio.wait_for(
            client.create(
                [UserMessage(content="1+1=?", source="user")],
                extra_create_args={"max_tokens": 5},
            ),
            timeout=8.0,
        )
        text = str(getattr(result, "content", ""))[:200]
        return {"ok": True, "sample": text, "config": _load_user_config()}
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": "测试超时（>8s），可能网络慢或模型在冷启动；实际工作流 timeout=180s，请直接跑",
            "config": _load_user_config(),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e)[:500],
            "config": _load_user_config(),
        }


# ============== helpers ==============
def _resolve_model_config(payload_cfg: dict | None) -> ModelConfig:
    """解析 model_config：body > env var > 文件 > 默认

    优先级（部署后的现代路径）：
    1. body（来自前端 WS 消息体 / HTTP payload；前端从浏览器 localStorage 读）
    2. env var（dev 模式：export DEEPSEEK_API_KEY=...；部署时留空）
    3. runs/_user_config.json（已废弃，保留以兼容老 session / 旧前端）
    4. 兜底：空 key 的 deepseek
    """
    if payload_cfg and isinstance(payload_cfg, dict):
        return ModelConfig(**{
            k: v for k, v in payload_cfg.items()
            if k in ModelConfig.__dataclass_fields__
        })
    # env var 优先（生产部署下 key 不进文件，靠用户浏览器 localStorage 提供）
    api_key = (
        os.getenv("OPENAI_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("DASHSCOPE_API_KEY")
        or ""
    )
    if api_key:
        # 用哪个 provider？优先看 env 提示
        provider = "deepseek"
        if os.getenv("OPENAI_API_KEY"):
            provider = "openai"
        elif os.getenv("DASHSCOPE_API_KEY"):
            provider = "qwen"
        return ModelConfig.from_provider(provider, api_key=api_key)
    # 兜底：runs/_user_config.json（已废弃，兼容用）
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return ModelConfig(**{
                k: v for k, v in data.items()
                if k in ModelConfig.__dataclass_fields__
            })
        except (json.JSONDecodeError, TypeError):
            pass
    return ModelConfig.from_provider("deepseek")


def _user_input_dict(user: UserInput) -> dict:
    """UserInput → dict（UserInput 已是 dataclass）"""
    from dataclasses import asdict, is_dataclass
    if is_dataclass(user):
        return asdict(user)
    # 测试 fallback：mock 对象
    return {f: getattr(user, f, None) for f in (
        "inspiration", "duration", "shot_count", "aspect_ratio", "style_hint",
        "quality", "color_tone", "texture", "frame_rate", "lighting_mood",
        "mood", "characters", "music_hint", "narration", "extra_constraints",
    )}


def _msg_text(msg: Any) -> str:
    """提取 AutoGen 消息对象的文本内容"""
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
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content
        )
    return str(content)


def _msg_dict(msg: Any) -> dict:
    return {"role": getattr(msg, "source", "?"), "content": _msg_text(msg)}


def _maybe_read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None