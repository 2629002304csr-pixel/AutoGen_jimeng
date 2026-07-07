"""DEPRECATED: v1 legacy. Use src/ (v2) for new work.

M7 Web UI：FastAPI + WebSocket (M13: 同 session 多集)

启动：python main.py --web
打开浏览器：http://localhost:8000

架构：
- 单 HTML + React UMD（前端在 static/index.html）
- FastAPI + WebSocket /ws（JSON 协议）
- WorkflowOrchestrator 加 on_message 回调
- 流式事件经 on_message → WebSocket 推送到浏览器

WebSocket 协议（M13 简化 — 删 user_addition）：
客户端 → 服务端（3 种）：
- {"type": "start", "raw_text": "...", "session_id": "..."}  # session_id 续接同 session 写下一集；不传则新建
- {"type": "rewrite", "session_id": "...", "feedback": "..."}  # 重写本集
- {"type": "continue", "session_id": "..."}  # 跑 Step 2

旧字段（向后兼容）：
- {"type": "start", "parent_session_id": "..."}  # M12 fork 子 session（CLI / 旧前端兼容）
- {"type": "user_addition", "text": "..."}  # M12 旧入 user_additions list（新前端不用）

服务端 → 客户端（5 种）：
- {"type": "token", "role": "...", "content": "..."}        # LLM token
- {"type": "message", "role": "...", "content": "..."}      # 完整消息
- {"type": "status", "state": "...", "session_id": "...", ...}
- {"type": "result", "session_id": "...", "prompt": "...", "ep_id": N, "inspiration_preview": "..."}
- {"type": "error", "message": "..."}
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .orchestrator import WorkflowOrchestrator
from .parser import ParseError, parse_user_input
from ..session import Session, serialize_message
from .workflow import (
    build_task_message,
    extract_prompt,
)


# Windows 终端默认 cp1252/GBK，重配 stdout 为 UTF-8 让中文不乱码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


PROJECT_ROOT = Path(__file__).parent.parent.parent  # src/v1_legacy/web_server.py → 3 级父 = 项目根
STATIC_DIR = PROJECT_ROOT / "static"

app = FastAPI(title="AutoGen 即梦工作流 Web UI")


# ============== 静态文件 ==============
@app.get("/")
async def root():
    # 不缓存 HTML（避免改完后用户看到旧版）
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


if STATIC_DIR.exists():
    # M16 修复：加 no-cache 头 — 防止浏览器缓存旧版 index.html / vendor.js，
    # 导致新代码（modal / 端点）不生效
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response

    class NoCacheStaticMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response: Response = await call_next(request)
            if request.url.path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

    app.add_middleware(NoCacheStaticMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ============== M16: 启动时加载用户配置 ==============
@app.on_event("startup")
async def _startup_load_user_config() -> None:
    """M16：服务器启动时自动加载 runs/_user_config.json（如果存在）

    让用户上次在前端保存的配置立即生效（不必每次都重新配置）。
    """
    from .config import apply_user_config
    from .user_config import load_user_config

    cfg = load_user_config(RUNS_DIR_DEFAULT)
    apply_user_config(cfg)
    if cfg.api_key:
        print(f"[M16 启动] 已加载用户配置：{cfg.provider} / {cfg.main_model} (api_key 已设)")
    else:
        print(f"[M16 启动] 已加载默认配置：{cfg.provider} / {cfg.main_model}（未配 api_key）")


# ============== M12: 多会话管理 HTTP 端点 ==============
RUNS_DIR_DEFAULT = Path("runs")


# ============== M16: 模型配置 API ==============

@app.get("/api/config")
async def get_config():
    """M16: 返回当前配置快照（API key 已 mask）

    前端用此 GET 初始化"⚙️ 模型设置"面板。
    """
    from .config import get_runtime_snapshot
    return get_runtime_snapshot()


@app.post("/api/config")
async def set_config(payload: dict):
    """M16: 保存用户配置到 runs/_user_config.json + 立即应用到运行时

    Body 字段：
    - provider: str（deepseek/qwen/openai/custom）
    - base_url: str（可选，留空用 provider 默认）
    - light_model: str（可选，留空用 provider 默认）
    - main_model: str（可选，留空用 provider 默认）
    - api_key: str（可选；空字符串或 mask 字符串 = 保留现有）
    - timeout: float（可选，默认 60）
    - max_retries: int（可选，默认 3）

    重要：保存后**立即** apply_user_config，下次新构造的 client 用新值。
    正在跑的 step 用的是已构造的 client，跑完下次再换。
    """
    from .config import apply_user_config, get_runtime_snapshot
    from .user_config import (
        UserConfig,
        is_masked_key,
        load_user_config,
        save_user_config,
    )

    if not isinstance(payload, dict):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="payload 必须是 JSON object")

    # M16 debug：打印收到的 payload 到 server 日志
    print(f"[M16 POST /api/config] provider={payload.get('provider')!r}, "
          f"main_model={payload.get('main_model')!r}, "
          f"api_key_len={len(payload.get('api_key') or '')}")

    existing = load_user_config(RUNS_DIR_DEFAULT)

    # API key 特殊处理：
    # - 前端传来 "" / "****xxx****" 形态 → 保留现有
    # - 前端传来完整新 key → 用新 key
    raw_key = (payload.get("api_key") or "").strip()
    if not raw_key or is_masked_key(raw_key):
        new_key = existing.api_key  # 保持
    else:
        new_key = raw_key  # 用新值

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

    save_user_config(cfg, RUNS_DIR_DEFAULT)
    apply_user_config(cfg)
    print(f"[M16 POST /api/config] ✓ 已保存到 {RUNS_DIR_DEFAULT / '_user_config.json'}")

    return {"ok": True, "config": get_runtime_snapshot()}


@app.post("/api/config/test")
async def test_config(payload: dict | None = None):
    """M16: 用当前配置调一次最小 LLM（'1+1=?' 回答一个数字）确认 key 有效

    Body（可选）：如果传了，先用临时配置测试；不传则测当前 _runtime。
    """
    import asyncio

    from fastapi.concurrency import run_in_threadpool

    from .config import _runtime, apply_user_config, get_runtime_snapshot, light_client
    from .user_config import (
        UserConfig,
        is_masked_key,
        load_user_config,
    )

    # 可选：临时应用 payload 测试
    if payload and isinstance(payload, dict):
        existing = load_user_config(RUNS_DIR_DEFAULT)
        raw_key = (payload.get("api_key") or "").strip()
        new_key = existing.api_key if not raw_key or is_masked_key(raw_key) else raw_key
        test_cfg = UserConfig(
            provider=(payload.get("provider") or existing.provider or "deepseek").lower(),
            base_url=(payload.get("base_url") or existing.base_url or "").strip(),
            light_model=(payload.get("light_model") or existing.light_model or "").strip(),
            main_model=(payload.get("main_model") or existing.main_model or "").strip(),
            api_key=new_key,
            timeout=30.0,  # 测试用短超时
            max_retries=1,
        )
        apply_user_config(test_cfg)

    # 先看 _runtime 是否有 api_key（light_client 构造时会校验，避免抛异常）
    if not _runtime.api_key:
        print(f"[M16 POST /api/config/test] ✗ _runtime.api_key 为空（provider={_runtime.provider}）")
        return {"ok": False, "error": "未配置 API key，请在「⚙️ 模型设置」中填入", "config": get_runtime_snapshot()}

    print(f"[M16 POST /api/config/test] provider={_runtime.provider}, model={_runtime.light_model}, key={_runtime.api_key[:8]}...")

    try:
        client = light_client()

        async def _call():
            from autogen_core.models import UserMessage
            result = await client.create([UserMessage(content="1+1=?", source="user")])
            return result

        result = await run_in_threadpool(lambda: asyncio.run(_call()))
        text = str(getattr(result, "content", ""))[:200]
        print(f"[M16 POST /api/config/test] ✓ 成功，sample={text!r}")
        return {"ok": True, "sample": text, "config": get_runtime_snapshot()}
    except Exception as e:
        print(f"[M16 POST /api/config/test] ✗ 失败：{type(e).__name__}: {e}")
        return {"ok": False, "error": str(e)[:500], "config": get_runtime_snapshot()}


def _list_sessions_impl(runs_dir: Path) -> list[dict]:
    """扫 runs/ 列出所有 session（M13 内部用）

    返回字段：
    - session_id / title / created_at / parent_session_id
    - current_step: "running" | "after_writer" | "complete" | "start"
    - current_episode: int  （M13：本会话已写完几集）
    - current_inspiration: str  （M13：本集灵感）
    - has_fact_sheet: bool
    """
    if not runs_dir.exists():
        return []
    items: list[dict] = []
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        state_path = run_dir / "state.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        session_id = state.get("session_id") or run_dir.name
        created_at = state.get("created_at", "")
        parent_session_id = state.get("parent_session_id")
        current_step = state.get("current_step", "start")
        current_episode = state.get("current_episode", 1)
        current_inspiration = state.get("current_inspiration", "")
        fs_path = run_dir / "fact_sheet.json"
        has_fact_sheet = fs_path.exists()
        # title 优先级：fact_sheet.title > user_input.inspiration[:20] > session_id
        title = session_id
        if has_fact_sheet:
            try:
                fs = json.loads(fs_path.read_text(encoding="utf-8"))
                if fs.get("title"):
                    title = fs["title"]
            except (json.JSONDecodeError, OSError):
                pass
        if title == session_id:
            user_input = state.get("user_input_dict") or {}
            insp = user_input.get("inspiration", "")
            if isinstance(insp, str) and insp:
                title = insp[:20] + ("…" if len(insp) > 20 else "")
        items.append({
            "session_id": session_id,
            "title": title,
            "created_at": created_at,
            "parent_session_id": parent_session_id,
            "current_step": current_step,
            "current_episode": current_episode,
            "current_inspiration": current_inspiration,
            "has_fact_sheet": has_fact_sheet,
        })
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items


@app.get("/api/sessions")
async def list_sessions():
    """GET /api/sessions — 列出所有 session（按 created_at 倒序）

    用于 Web 左侧栏渲染。
    """
    return _list_sessions_impl(RUNS_DIR_DEFAULT)


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """GET /api/sessions/{sid} — 加载某个 session 的完整状态

    返回 messages + writer_output + fact_sheet + prompts 列表。
    用于点开侧栏 session 时重载聊天区。

    M13 新字段：
    - prompts: [{ep_id, title, inspiration_preview, content, created_at}, ...]
        新格式 session：每个 episode 1 个 tab 项
        旧格式 session（无 prompts_index.json）：降级为 [集 1]（读 jimeng_prompt.md）
    - current_episode: int（本会话当前集数）
    - fact_sheet_dict: dict | None（缓存的最新 fact_sheet）
    """
    state_path = RUNS_DIR_DEFAULT / session_id / "state.json"
    if not state_path.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    run_dir = RUNS_DIR_DEFAULT / session_id

    def _maybe_read(p: Path) -> str | None:
        return p.read_text(encoding="utf-8") if p.exists() else None

    writer_output = _maybe_read(run_dir / "writer_output.md")
    fact_sheet = None
    fs_path = run_dir / "fact_sheet.json"
    if fs_path.exists():
        try:
            fact_sheet = json.loads(fs_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            fact_sheet = None

    # M13：读 prompts 列表（新格式 + 旧格式降级）
    prompts = _load_prompts_for_session(run_dir)

    return {
        "session_id": session_id,
        "current_step": state.get("current_step", "start"),
        "current_episode": state.get("current_episode", 1),
        "current_inspiration": state.get("current_inspiration", ""),
        "user_input": state.get("user_input_dict", {}),
        "messages": state.get("messages", []),
        "writer_output": writer_output,
        "fact_sheet": fact_sheet,
        "prompts": prompts,
        "jimeng_prompt": _maybe_read(run_dir / "jimeng_prompt.md"),
        "parent_session_id": state.get("parent_session_id"),
        "created_at": state.get("created_at"),
    }


def _load_prompts_for_session(run_dir: Path) -> list[dict]:
    """读 session 的 prompts 列表（M13：每集 1 个 tab）

    新格式：prompts_index.json + prompts/00X.md
    旧格式降级：jimeng_prompt.md → 1 项（无 inspiration_preview）

    Returns:
        [{ep_id, title, inspiration_preview, content, created_at}, ...]
        按 ep_id 升序
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
        # 旧格式降级：单文件当集 1
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


# ============== helpers（从 main.py 复用） ==============
def _save_step1_artifacts(
    session: Session,
    messages: list,
    writer_output: str,
    runs_dir: Path,
) -> None:
    run_dir = runs_dir / session.session_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "user_input.json").write_text(
        json.dumps(session.user_input_dict, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "writer_output.md").write_text(
        writer_output or "(无 Writer 输出)", encoding="utf-8"
    )
    from .workflow import _msg_to_text
    transcript = []
    for m in messages:
        src = getattr(m, "source", "?")
        text = _msg_to_text(m)
        transcript.append(f"=== {src} ===\n{text}\n")
    (run_dir / "transcript.md").write_text("\n".join(transcript), encoding="utf-8")


def _save_step2_artifacts(
    session: Session,
    messages: list,
    script: str,
    prompt: str,
    runs_dir: Path,
) -> None:
    """Step 2 结束保存产物（M13：每集 1 个 prompt 文件 + prompts_index.json）

    旧版只保存一份 jimeng_prompt.md（M12 之前）。
    M13 改成：
    - 每集写到 prompts/<ep_id:03d>.md（如 001.md / 002.md / ...）
    - prompts_index.json 维护 [ep_id, created_at, inspiration, ...] 列表
    - 重写本集时**覆盖**当前 ep_id 的文件 + 索引项

    向后兼容：仍保存一份 jimeng_prompt.md = 本集最新一份（最后一次跑）的 prompt，
    方便旧前端 / CLI 直接读单文件。

    transcript.md 仍存最新一份全部角色原文（含 Step 1 + Step 2）。
    """
    run_dir = runs_dir / session.session_id
    run_dir.mkdir(parents=True, exist_ok=True)

    ep_id = session.current_episode
    prompts_dir = run_dir / "prompts"
    prompts_dir.mkdir(exist_ok=True)

    # 1. 写当集文件（覆盖式 — 重写本集也保留同样 ep_id）
    (prompts_dir / f"{ep_id:03d}.md").write_text(
        prompt or "(no prompt produced)", encoding="utf-8"
    )

    # 2. 写索引（追加 / 替换本集）
    index_path = run_dir / "prompts_index.json"
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            index = {"episodes": []}
    else:
        index = {"episodes": []}
    ep_meta = {
        "ep_id": ep_id,
        "created_at": datetime.now().isoformat(),
        "inspiration": session.current_inspiration or "",
        "title": f"集 {ep_id}",
    }
    # 重写时先移除已有同 ep_id 条目，再追加（防止重复）
    index["episodes"] = [e for e in index["episodes"] if e.get("ep_id") != ep_id]
    index["episodes"].append(ep_meta)
    index["episodes"].sort(key=lambda e: e.get("ep_id", 0))
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 3. 旧版兼容：jimeng_prompt.md = 本集最新 prompt
    (run_dir / "jimeng_prompt.md").write_text(
        prompt or "(no prompt produced)", encoding="utf-8"
    )

    # 4. transcript 更新
    from .workflow import _msg_to_text
    transcript = []
    for m in messages:
        src = getattr(m, "source", "?")
        text = _msg_to_text(m)
        transcript.append(f"=== {src} ===\n{text}\n")
    (run_dir / "transcript.md").write_text("\n".join(transcript), encoding="utf-8")


# ============== WebSocket handler ==============
class WorkflowSession:
    """一个 WebSocket 连接 = 一个 WorkflowSession（跨多轮交互）

    state: idle → running_step1 → after_writer → running_step2 → complete
                    ↘ running_rewrite ↗
                    ↘ running_step2 (after user_addition)
    """
    def __init__(self, websocket: WebSocket, runs_dir: Path):
        self.ws = websocket
        self.runs_dir = runs_dir
        self.session_id: str | None = None
        self.state: str = "idle"

    async def send(self, msg: dict) -> None:
        """发送 JSON 消息到浏览器"""
        await self.ws.send_json(msg)


async def _on_message_factory(ws: WorkflowSession):
    """构造一个闭包：把 (role, content, is_token) 推到 WebSocket"""
    async def on_message(role: str, content: str, is_token: bool) -> None:
        try:
            await ws.send({
                "type": "token" if is_token else "message",
                "role": role,
                "content": content,
            })
        except Exception:
            # WebSocket 已断开，吞掉异常避免影响 workflow
            pass
    return on_message


async def handle_start(ws: WorkflowSession, msg: dict[str, Any]) -> None:
    """启动 Step 1（Host + Writer）

    M13 行为分支：
    1. msg.session_id 提供 → 加载现有 session，set current_inspiration=raw_text
       跑新一集（不新建 session，同 session 续接）
    2. msg.parent_session_id 提供（M12 旧版兼容） → fork 子 session
    3. 都没提供 → 新建 session（首集）
    """
    raw_text: str = (msg.get("raw_text") or "").strip()
    if not raw_text:
        await ws.send({"type": "error", "message": "raw_text 不能为空"})
        return

    target_sid: str | None = msg.get("session_id") or None
    parent_session_id: str | None = msg.get("parent_session_id") or None

    session: Session | None = None
    fact_sheet = None

    if target_sid:
        # M13 主路径：续接现有 session 写下一集
        try:
            session = Session.load(target_sid, runs_dir=ws.runs_dir)
        except FileNotFoundError:
            await ws.send({"type": "error", "message": f"session_id={target_sid} 不存在"})
            return
        user = session.get_user_input()
        # 设置本集灵感（覆盖式）
        session.set_current_inspiration(raw_text)
        session.save(ws.runs_dir)
        # M13：自动用会话级最新 fact_sheet（含所有跨集沉淀）
        fact_sheet = session.fact_sheet_dict
    elif parent_session_id:
        # M12 兼容路径：fork 子 session（CLI / 旧前端）
        fact_sheet = Session.load_fact_sheet(parent_session_id, runs_dir=ws.runs_dir)
        if not fact_sheet:
            await ws.send({
                "type": "error",
                "message": f"parent_session_id={parent_session_id} 找不到 fact_sheet",
            })
            return
        try:
            user = await parse_user_input(raw_text)
        except ParseError as e:
            await ws.send({"type": "error", "message": f"解析失败: {e}"})
            return
    else:
        # 新建 session（首集）
        try:
            user = await parse_user_input(raw_text)
        except ParseError as e:
            await ws.send({"type": "error", "message": f"解析失败: {e}"})
            return

    orch = WorkflowOrchestrator(user=user, prompts_dir=Path("prompts"))
    # M13：续接时把 raw_text 当本集灵感传入（每集 1 段）
    task = build_task_message(
        user,
        fact_sheet=fact_sheet,
        current_inspiration=raw_text if target_sid else "",
    )

    on_message = await _on_message_factory(ws)
    await ws.send({"type": "status", "state": "running_step1"})

    result = await orch.step1_host_writer(task, on_message=on_message)
    messages = result["messages"]
    writer_output = result["writer_output"]

    if session is None:
        # 新建 Session（首集 / parent_session_id fork）
        session = Session.create(
            user, runs_dir=ws.runs_dir, parent_session_id=parent_session_id
        )
        if not target_sid and not parent_session_id:
            # 首集：把 raw_text 也存为本集灵感（首集 inspiration）
            session.set_current_inspiration(raw_text)
    # else：续接路径，session 已在上面 load + save 过

    session.current_step = "after_writer"
    for m in messages:
        sm = serialize_message(m)
        session.add_message(sm["role"], sm["content"])
    session.save(ws.runs_dir)
    ws.session_id = session.session_id

    # M13：fact_sheet 保存
    # - 续接路径：update_fact_sheet_after_episode（merge 上集 + 本集新 extract）
    # - 首集 / fork：直接 extract_fact_sheet
    if target_sid and session.fact_sheet_dict:
        from .fact_sheet import update_fact_sheet_after_episode
        # M14：从本集 writer_output 提取 story_arc 条目
        episode_summary = _extract_episode_summary(writer_output)
        fs = update_fact_sheet_after_episode(
            old_fact_sheet=session.fact_sheet_dict,
            new_writer_output=writer_output,
            new_jimeng_prompt="",  # 还没跑 Step 2
            episode_id=session.current_episode,
            episode_summary=episode_summary,
        )
        fs_saved = session.save_fact_sheet(fact_sheet=fs, runs_dir=ws.runs_dir)
    else:
        fs_saved = session.save_fact_sheet(writer_output, ws.runs_dir)

    _save_step1_artifacts(session, messages, writer_output, ws.runs_dir)

    await ws.send({
        "type": "status",
        "state": "after_writer",
        "session_id": session.session_id,
        "current_episode": session.current_episode,
        "writer_output": writer_output,
        "fact_sheet_saved": bool(fs_saved),
        "message_count": result["message_count"],
        "parent_session_id": parent_session_id,
    })


async def handle_step2(ws: WorkflowSession, user_addition: str = "", target_sid: str | None = None) -> None:
    """Step 2: 跑 Storyboard → DP → Director

    M13 变化：
    - user_addition 参数保留但不再使用（M13 不再累积 user_additions list）
    - 跑完 Step 2 调 update_fact_sheet_after_episode + advance_episode
    - 通过 ep_id / inspiration_preview 让前端能区分多个 tab

    Args:
        target_sid: 要操作的 session_id（来自 msg.session_id）。
            默认用 ws.session_id（M7 旧行为：只能操作自己刚跑的那个 session）。
            M12 允许前端从侧栏切到老 session 接着跑。
    """
    sid = target_sid or ws.session_id
    if not sid:
        await ws.send({"type": "error", "message": "no active session"})
        return
    session = Session.load(sid, runs_dir=ws.runs_dir)
    user = session.get_user_input()

    # M22 双闸（强化版）：当前端闸只查 writerOutput 字符串非空，
    # 但 deepseek-reasoner / qwen3-thinking 会把"好的我想想..."这种 preamble
    # 当成非空内容过闸，Step 2 跑在垃圾上。
    # 这里改成：既要 after_writer 状态，又要 Writer 真输出结构化内容。
    # （用户原话："没有 review 不给跑"）
    if session.current_step != "after_writer":
        await ws.send({
            "type": "error",
            "message": (
                f"session {sid} 当前状态 {session.current_step!r}，"
                "必须先跑完 Step 1（state=after_writer）才能继续 Step 2"
            ),
        })
        return

    # 闸二：Writer 没产出有效内容 → 拒绝（不能用空 / preamble / 纯 markdown 段名当内容）
    from .validator import REQUIRED_SECTIONS, validate_output
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
    # 取 Writer 最后一条
    last_writer = writer_msgs[-1].get("content", "")
    ok, errs = validate_output("Writer", last_writer, user)
    if not ok:
        err_summary = "; ".join(errs[:3])
        await ws.send({
            "type": "error",
            "message": (
                f"Writer 输出未通过结构校验（当前内容不算 review 完成的产物）：{err_summary}。"
                "请先让 Writer 重写完整内容（重写按钮或编辑 runs/<sid>/writer_output.md），再继续 Step 2"
            ),
            "validation_errors": errs,
        })
        return

    # 用户编辑了 writer_output.md？自动同步
    session.sync_writer_output_from_file(ws.runs_dir)

    # 用户编辑了 writer_output.md？自动同步
    session.sync_writer_output_from_file(ws.runs_dir)

    # 重建 history messages
    # M15：按 ep_id 切片，只取当前集的 Step 1 消息
    # 避免 selector first_pass 看到上集所有 5 个角色 → 跳过所有角色 → 立即结束
    from autogen_agentchat.messages import TextMessage
    history = [
        TextMessage(content=m["content"], source=m["role"])
        for m in session.messages
        if m.get("ep_id") == session.current_episode
    ]

    orch = WorkflowOrchestrator(user=user, prompts_dir=Path("prompts"))
    on_message = await _on_message_factory(ws)
    await ws.send({"type": "status", "state": "running_step2"})

    # M13：user_additions 不再累积，但 orchestrator 签名还接受；传空即可
    result = await orch.step2_continue(
        history, user_addition="", on_message=on_message
    )
    messages = result["messages"]

    # 更新 Session
    session.current_step = "complete"
    for m in result.get("new_messages", []):
        sm = serialize_message(m)
        session.add_message(sm["role"], sm["content"])
    session.save(ws.runs_dir)

    # 提取最终输出（M7：只存 jimeng_prompt，不再存 script.md）
    prompt = extract_prompt(messages)

    # M17 debug：诊断 jimeng_prompt 为空的原因
    # - Director 是否输出了 FINAL_APPROVED/FORCE_OUTPUT？
    # - 是否有 `## 最终脚本` 段？
    # - 总消息数 / 各角色消息数
    from .workflow import _msg_to_text, _find_director_decision, _parse_sections, _find_section
    director_text, is_force = _find_director_decision(messages)
    if director_text:
        d_sec = _parse_sections(director_text)
        per_shot = _find_section(d_sec, "最终脚本")
        has_think = "<think>" in director_text
        per_shot_len = len(per_shot) if per_shot else 0
        prompt_len = len(prompt) if prompt else 0
        role_counts: dict[str, int] = {}
        for mm in messages:
            src = getattr(mm, "source", "?")
            role_counts[src] = role_counts.get(src, 0) + 1
        print(f"\n[M17 debug] Step 2 结束：")
        print(f"  - 总消息数={len(messages)}, 各角色={role_counts}")
        print(f"  - Director has FINAL_APPROVED/FORCE_OUTPUT: True")
        print(f"  - Director 文本总长={len(director_text)}, 含 <think>: {has_think}")
        print(f"  - Director 各段：{list(d_sec.keys())}")
        print(f"  - '## 最终脚本' 段长度={per_shot_len}")
        print(f"  - extract_prompt 返回长度={prompt_len}")
        if not per_shot:
            print(f"  ⚠️  警告：Director 没输出 '## 最终脚本' 段 → jimeng_prompt.md 会是空")
            print(f"  - Director 文本前 500 字：\n{director_text[:500]}")
    else:
        print(f"\n[M17 debug] Step 2 结束：⚠️  没找到 Director 的决策消息（FINAL_APPROVED/FORCE_OUTPUT）")
        role_counts = {}
        for mm in messages:
            src = getattr(mm, "source", "?")
            role_counts[src] = role_counts.get(src, 0) + 1
        print(f"  - 总消息数={len(messages)}, 各角色={role_counts}")

    _save_step2_artifacts(session, messages, "", prompt, ws.runs_dir)

    # M13：每集跑完都更新 fact_sheet（merge 上集 + 本集新 extract）
    from .fact_sheet import update_fact_sheet_after_episode
    writer_output_md = _find_latest_writer_md(session, ws.runs_dir)
    # M14：从 writer_output 提取故事梗概，追加到 story_arc（不覆盖）
    episode_summary = _extract_episode_summary(writer_output_md)
    fs = update_fact_sheet_after_episode(
        old_fact_sheet=session.fact_sheet_dict,
        new_writer_output=writer_output_md,
        new_jimeng_prompt=prompt,
        episode_id=session.current_episode,
        episode_summary=episode_summary,
    )
    session.save_fact_sheet(fact_sheet=fs, runs_dir=ws.runs_dir)
    # advance_episode: current_episode += 1；清空本集灵感 / 重写反馈
    session.advance_episode()
    session.save(ws.runs_dir)

    # M13：返回 ep_id + inspiration_preview 让前端能加 tab
    inspiration_preview = (session.current_inspiration if hasattr(session, "current_inspiration") else "") or ""
    await ws.send({
        "type": "result",
        "session_id": sid,
        "prompt": prompt,
        "ep_id": session.current_episode - 1,  # 已 advance → 用上一集
        "inspiration_preview": inspiration_preview[:15],
        "message_count": result["message_count"],
        "run_dir": str(ws.runs_dir / sid),
    })


def _find_latest_writer_md(session: Session, runs_dir: Path) -> str:
    """从 session.messages 拿本集 Writer 最后一条；fallback 读文件

    M15：按 ep_id 切片，只取当前集的 Writer（避免续集场景下返回上集 Writer）
    """
    from .workflow import _msg_to_text
    for m in reversed(session.messages):
        if m.get("role") == "Writer" and m.get("ep_id") == session.current_episode:
            text = m.get("content", "")
            if text:
                # 已是 str
                return text
    # fallback
    path = runs_dir / session.session_id / "writer_output.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _extract_episode_summary(writer_output: str) -> str:
    """M14：从 Writer 输出提取 "## 故事梗概" 段，作为本集剧情摘要

    用作 fact_sheet.story_arc 的本集条目（追加，不覆盖）。

    规则：
    - 取 "## 故事梗概" 段内容
    - 截取前 200 字（避免注入时 context 爆）
    - 段不存在 → 返回空字符串（update_fact_sheet_after_episode 会跳过）

    Args:
        writer_output: Writer 完整 markdown 输出

    Returns:
        本集剧情摘要（≤200 字）；无 "故事梗概" 段时返回 ""
    """
    if not writer_output:
        return ""
    # 用与 fact_sheet.py 相同的 section parser
    from .fact_sheet import _parse_sections
    sections = _parse_sections(writer_output)
    summary = sections.get("故事梗概", "")
    if not summary:
        return ""
    # 取第一段（去掉多余空行），截前 200 字
    summary = summary.strip().split("\n\n")[0].strip()
    return summary[:200]


async def handle_rewrite(ws: WorkflowSession, msg: dict[str, Any]) -> None:
    """M11 模式：Writer 重写（M13：用 current_rewrite_feedback 而非 user_additions）

    target_sid 从 msg.session_id 读取（M12），允许 viewing 态切到老 session 后也能 rewrite。
    """
    feedback: str = (msg.get("feedback") or "").strip()
    if not feedback:
        await ws.send({"type": "error", "message": "feedback 不能为空"})
        return
    target_sid = msg.get("session_id") or ws.session_id
    if not target_sid:
        await ws.send({"type": "error", "message": "no active session"})
        return

    session = Session.load(target_sid, runs_dir=ws.runs_dir)
    user = session.get_user_input()

    # 编辑过文件先同步
    session.sync_writer_output_from_file(ws.runs_dir)

    # M13：feedback 存到单字段（不再入 user_additions list）
    session.set_rewrite_feedback(feedback)
    session.save(ws.runs_dir)

    # 重建 history
    # M15：按 ep_id 切片，只取当前集的消息（避免续集场景下 selector 看到上集角色）
    from autogen_agentchat.messages import TextMessage
    history = [
        TextMessage(content=m["content"], source=m["role"])
        for m in session.messages
        if m.get("ep_id") == session.current_episode
    ]

    orch = WorkflowOrchestrator(user=user, prompts_dir=Path("prompts"))
    on_message = await _on_message_factory(ws)
    await ws.send({"type": "status", "state": "running_rewrite"})

    rw = await orch.step1b_rewrite_writer(
        history, feedback, on_message=on_message
    )
    new_writer_output = rw["new_writer_output"]

    if not new_writer_output:
        await ws.send({"type": "error", "message": "Writer 未返回新内容"})
        return

    # 替换 session.messages 中的 Writer 消息
    # M15：续集场景下 `break` 会替换首个 Writer（可能是上集的）
    # 改为只替换本集 ep_id 的 Writer
    replaced = False
    for i, m in enumerate(session.messages):
        if m["role"] == "Writer" and m.get("ep_id") == session.current_episode:
            session.messages[i]["content"] = new_writer_output
            replaced = True
            break
    if not replaced:
        session.add_message("Writer", new_writer_output)

    # 同步 writer_output.md + fact_sheet
    run_dir = ws.runs_dir / target_sid
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "writer_output.md").write_text(
        new_writer_output, encoding="utf-8"
    )
    # M14：用 update_fact_sheet_after_episode（merge + 替换 story_arc 同 ep_id 条目）
    # 而不是 extract_fact_sheet（会丢失 characters 继承 + story_arc 历史）
    from .fact_sheet import update_fact_sheet_after_episode
    episode_summary = _extract_episode_summary(new_writer_output)
    fs = update_fact_sheet_after_episode(
        old_fact_sheet=session.fact_sheet_dict,
        new_writer_output=new_writer_output,
        new_jimeng_prompt="",  # 还没跑 Step 2
        episode_id=session.current_episode,
        episode_summary=episode_summary,
    )
    fs_saved = session.save_fact_sheet(fact_sheet=fs, runs_dir=ws.runs_dir)

    # 状态保持 after_writer（重写不递增 current_episode）
    session.current_step = "after_writer"
    session.save(ws.runs_dir)

    # 更新 transcript
    transcript_path = run_dir / "transcript.md"
    if transcript_path.exists():
        existing = transcript_path.read_text(encoding="utf-8")
    else:
        existing = ""
    rewrite_count_marker = "重写" if not session.current_rewrite_feedback else "重写"
    new_transcript = (
        existing
        + f"\n=== Writer ({rewrite_count_marker} 本集) ===\n{new_writer_output}\n"
    )
    transcript_path.write_text(new_transcript, encoding="utf-8")

    await ws.send({
        "type": "status",
        "state": "after_writer",
        "session_id": target_sid,
        "current_episode": session.current_episode,
        "writer_output": new_writer_output,
        "fact_sheet_saved": bool(fs_saved),
        "rewritten": True,
    })


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """WebSocket 入口：分派 4 种客户端消息"""
    await websocket.accept()
    ws = WorkflowSession(websocket, runs_dir=Path("runs"))
    try:
        async for raw in websocket.iter_text():
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send({"type": "error", "message": f"invalid JSON: {raw[:100]}"})
                continue

            t = msg.get("type")
            try:
                if t == "start":
                    await handle_start(ws, msg)
                elif t == "rewrite":
                    await handle_rewrite(ws, msg)
                elif t == "user_addition":
                    await handle_step2(ws, msg.get("text", ""), msg.get("session_id"))
                elif t == "continue":
                    await handle_step2(ws, "", msg.get("session_id"))
                else:
                    await ws.send({"type": "error", "message": f"unknown type: {t}"})
            except Exception as e:
                # 把 workflow 异常推回浏览器，方便调试
                import traceback
                await ws.send({
                    "type": "error",
                    "message": f"{type(e).__name__}: {e}",
                    "trace": traceback.format_exc(limit=3),
                })
    except WebSocketDisconnect:
        pass
