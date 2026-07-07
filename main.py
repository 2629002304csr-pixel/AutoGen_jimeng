"""v1 legacy CLI 入口（M4 分步工作流）

DEPRECATED: 推荐用 `uvicorn src.app:app` (M21 v2)。
本入口仅用于向后兼容，跑的是 src/v1_legacy/ 里的旧实现。

两种模式：

A. 启动新会话（Step 1）：
   python main.py --raw "15秒赛博朋克女黑客短片，4K画质"
   → 输出 session_id + runs/<session_id>/writer_output.md
   → 等用户 review 后再 --resume --add 接着跑

B. 恢复会话（Step 2）：
   python main.py --resume <session_id> --add "加一只橘猫"
   → 加载 Session，把用户追加注入到 Storyboard 上下文
   → 跑完剩余 4 个角色，输出 script.md + jimeng_prompt.md

C. 不带 --resume 的单次模式（向后兼容）：
   python main.py --raw "..."
   → 仍然一次性跑完所有 6 个角色（不暂停）
"""
from __future__ import annotations

# Windows 终端默认 cp1252/GBK，重配 stdout 为 UTF-8 让中文不乱码
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import asyncio
import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from src.v1_legacy.config import UserInput
from src.v1_legacy.orchestrator import WorkflowOrchestrator
from src.parser import ParseError
from src.session import Session
from src.v1_legacy.workflow import (
    build_task_message,
    extract_script,
    extract_prompt,
    save_run,
)


# 默认示例
DEFAULT_EXAMPLE = UserInput(
    inspiration="一个赛博朋克女黑客在雨夜的霓虹东京街头追踪数据幽灵",
    duration=15,
    shot_count=3,
    style_hint="赛博朋克 / 银翼杀手 / 雨夜霓虹",
    aspect_ratio="16:9",
    quality="4K",
    color_tone="冷暖对比",
    texture="胶片",
    mood="紧张",
    characters="女黑客 K",
    music_hint="电子",
    extra_constraints=["不能出现清晰人脸", "必须有雨"],
)


def _save_step1_artifacts(
    session: Session,
    messages: list,
    writer_output: str,
    runs_dir: Path,
) -> None:
    """Step 1 完成后，把 writer_output + user_input 写到 runs/<session_id>/"""
    run_dir = runs_dir / session.session_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # user_input.json
    (run_dir / "user_input.json").write_text(
        json.dumps(session.user_input_dict, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # writer_output.md
    (run_dir / "writer_output.md").write_text(
        writer_output or "(无 Writer 输出)", encoding="utf-8"
    )

    # transcript.md (到目前为止的对话流)
    from datetime import datetime
    from src.v1_legacy.workflow import _msg_to_text
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
    """Step 2 完成后，把 script + jimeng_prompt 写到 runs/<session_id>/"""
    run_dir = runs_dir / session.session_id
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "script.md").write_text(
        script or "(no script produced)", encoding="utf-8"
    )
    (run_dir / "jimeng_prompt.md").write_text(
        prompt or "(no prompt produced)", encoding="utf-8"
    )

    # 更新 transcript.md
    from src.v1_legacy.workflow import _msg_to_text
    transcript = []
    for m in messages:
        src = getattr(m, "source", "?")
        text = _msg_to_text(m)
        transcript.append(f"=== {src} ===\n{text}\n")
    (run_dir / "transcript.md").write_text("\n".join(transcript), encoding="utf-8")


async def run_step1(
    user: UserInput,
    runs_dir: Path,
    parent_session_id: str | None = None,
) -> dict:
    """Step 1: 跑 Host + Writer，保存 Session 和 writer_output.md

    Args:
        user: 用户输入
        runs_dir: 产物目录
        parent_session_id: M8 续集时传入上一集 session_id（None = 首集）
    """
    orch = WorkflowOrchestrator(user=user, prompts_dir=Path("prompts"))

    # M8: 续集时加载上一集 fact_sheet 注入任务消息
    fact_sheet = None
    if parent_session_id:
        fact_sheet = Session.load_fact_sheet(parent_session_id, runs_dir=runs_dir)
        if fact_sheet:
            print(f"\n[M8 续集] 注入上一集 fact_sheet ({parent_session_id})")
        else:
            print(f"[警告] parent_session_id={parent_session_id} 但找不到 fact_sheet，跳过续集模式")
            parent_session_id = None

    task = build_task_message(user, fact_sheet=fact_sheet)

    mode_label = f"续接 {parent_session_id}" if parent_session_id else "新会话"
    print(f"\n[Step 1] 启动{mode_label}：{user.inspiration}")
    result = await orch.step1_host_writer(task)
    writer_output = result["writer_output"]

    # 创建 Session 并保存（含 parent_session_id）
    session = Session.create(user, runs_dir=runs_dir, parent_session_id=parent_session_id)
    session.current_step = "after_writer"
    for m in result["messages"]:
        from src.session import serialize_message
        session.add_message(serialize_message(m)["role"], serialize_message(m)["content"])
    session.save(runs_dir)

    # M8: 从 Writer 输出提取 fact_sheet（如果 user 编辑过 writer_output.md，sync 后再提取）
    fact_sheet_saved = session.save_fact_sheet(writer_output, runs_dir)
    _save_step1_artifacts(session, result["messages"], writer_output, runs_dir)

    return {
        "session_id": session.session_id,
        "writer_output": writer_output,
        "message_count": result["message_count"],
        "run_dir": runs_dir / session.session_id,
        "parent_session_id": parent_session_id,
        "fact_sheet_saved": bool(fact_sheet_saved),
    }


async def run_step2(
    session_id: str,
    user_addition: str,
    runs_dir: Path,
    *,
    rewrite: bool = False,
) -> dict:
    """Step 2: 加载 Session，注入用户追加，跑剩余 4 个角色

    Args:
        session_id: 恢复的 session_id
        user_addition: 用户追加的灵感/反馈
        runs_dir: 产物目录
        rewrite: M11 模式——True 时触发 Writer 重写而非 Step 2
    """
    session = Session.load(session_id, runs_dir=runs_dir)
    user = session.get_user_input()

    # 检查用户是否编辑了 writer_output.md；如有，替换 session 中的 Writer 消息
    if session.sync_writer_output_from_file(runs_dir):
        print(f"[Info] 检测到 writer_output.md 被修改")

    # M11: --rewrite 模式：重跑 Writer 后暂停，不跑 Step 2
    if rewrite:
        if not user_addition:
            print(f"[错误] --rewrite 必须传 feedback；用法：--rewrite \"你的反馈\"")
            return {
                "session_id": session_id,
                "rewritten": False,
                "run_dir": runs_dir / session_id,
            }
        # M13：feedback 存单字段（不再累加到 list）
        session.set_rewrite_feedback(user_addition)
        session.save(runs_dir)

        # 重建 history
        # M15：按 ep_id 切片，只取当前集的消息（避免续集场景下 selector 看到上集角色）
        history = []
        from autogen_agentchat.messages import TextMessage
        for m in session.messages:
            if m.get("ep_id") == session.current_episode:
                history.append(
                    TextMessage(content=m["content"], source=m["role"])
                )

        orch = WorkflowOrchestrator(user=user, prompts_dir=Path("prompts"))
        print(f"\n[M11 Rewrite] 根据反馈重写 Writer...")
        print(f"[Feedback] {user_addition}")
        rw = await orch.step1b_rewrite_writer(history, user_addition)
        new_writer_output = rw["new_writer_output"]

        if not new_writer_output:
            print(f"[错误] Writer 未返回新内容，跳过重写")
            return {
                "session_id": session_id,
                "rewritten": False,
                "run_dir": runs_dir / session_id,
            }

        # 替换 session.messages 中的 Writer 消息
        # M15：续集场景下只替换本集 ep_id 的 Writer（避免误改上集）
        replaced = False
        for i, m in enumerate(session.messages):
            if m["role"] == "Writer" and m.get("ep_id") == session.current_episode:
                session.messages[i]["content"] = new_writer_output
                replaced = True
                break
        if not replaced:
            # 兜底：session 里没本集 Writer 消息，追加一条
            session.add_message("Writer", new_writer_output)

        # 同步 writer_output.md
        run_dir = runs_dir / session_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "writer_output.md").write_text(
            new_writer_output, encoding="utf-8"
        )

        # 重新提取 fact_sheet（保证 M8 续集一致性）
        fs_saved = session.save_fact_sheet(new_writer_output, runs_dir)

        # 状态保持 after_writer（让用户继续 review / 再重写 / --add）
        session.current_step = "after_writer"
        session.save(runs_dir)

        # 更新 transcript.md（追加新 Writer 消息）
        transcript_path = run_dir / "transcript.md"
        transcript_lines = []
        if transcript_path.exists():
            transcript_lines.append(transcript_path.read_text(encoding="utf-8"))
        transcript_lines.append(
            f"\n=== Writer (重写本集) ===\n{new_writer_output}\n"
        )
        transcript_path.write_text("\n".join(transcript_lines), encoding="utf-8")

        print(f"\n[Writer 重写] {len(new_writer_output)} 字")
        if fs_saved:
            print(f"[fact_sheet] 已重新生成")
        print()
        print(f"  继续：")
        print(f"    再重写: python main.py --resume {session_id} --rewrite \"下一轮反馈\"")
        print(f"    追加灵感: python main.py --resume {session_id} --add \"加一只猫\"")
        print(f"    跑 Step 2: python main.py --resume {session_id}  (无 --add)")

        return {
            "session_id": session_id,
            "writer_output": new_writer_output,
            "rewritten": True,
            "fact_sheet_saved": bool(fs_saved),
            "run_dir": runs_dir / session_id,
        }

    # 原 --add 流程（M13：累加改为单字段 current_inspiration）
    if user_addition:
        session.set_current_inspiration(user_addition)
        session.save(runs_dir)

    # 重建 history messages
    # M15：按 ep_id 切片，只取当前集的消息
    history = []
    from autogen_agentchat.messages import TextMessage
    for m in session.messages:
        if m.get("ep_id") == session.current_episode:
            history.append(
                TextMessage(content=m["content"], source=m["role"])
            )

    # 跑 Step 2
    orch = WorkflowOrchestrator(user=user, prompts_dir=Path("prompts"))
    print(f"\n[Step 2] 恢复会话 {session_id}")
    if user_addition:
        print(f"[本集灵感] {user_addition}")
    result = await orch.step2_continue(history, user_addition="")
    messages = result["messages"]

    # 更新 Session
    session.current_step = "complete"
    # 记录新产生的消息
    for m in result.get("new_messages", []):
        from src.session import serialize_message
        sm = serialize_message(m)
        session.add_message(sm["role"], sm["content"])
    session.save(runs_dir)

    # 提取最终输出
    script = extract_script(messages, user=user)
    prompt = extract_prompt(messages)
    _save_step2_artifacts(session, messages, script, prompt, runs_dir)

    return {
        "session_id": session_id,
        "script": script,
        "prompt": prompt,
        "message_count": result["message_count"],
        "retries_used": result["retries_used"],
        "validation_failures": result["validation_failures"],
        "run_dir": runs_dir / session_id,
    }


async def run_full(
    user: UserInput,
    runs_dir: Path,
    parent_session_id: str | None = None,
) -> dict:
    """向后兼容：不暂停，一次跑完所有 6 个角色（M3.4 行为）

    Args:
        user: 用户输入
        runs_dir: 产物目录
        parent_session_id: M8 续集时传入上一集 session_id
    """
    orch = WorkflowOrchestrator(user=user, prompts_dir=Path("prompts"))

    # M8: 续集时加载上一集 fact_sheet
    fact_sheet = None
    if parent_session_id:
        fact_sheet = Session.load_fact_sheet(parent_session_id, runs_dir=runs_dir)
        if fact_sheet:
            print(f"\n[M8 续集] 注入上一集 fact_sheet ({parent_session_id})")
        else:
            parent_session_id = None

    task = build_task_message(user, fact_sheet=fact_sheet)

    mode_label = f"续接 {parent_session_id}" if parent_session_id else "一次性"
    print(f"\n[Full] 跑完{mode_label}：{user.inspiration}")
    r1 = await orch.step1_host_writer(task)
    history = r1["messages"]
    r2 = await orch.step2_continue(history, user_addition="")
    messages = r2["messages"]

    script = extract_script(messages, user=user)
    prompt = extract_prompt(messages)

    # 保存到时间戳目录
    run_dir = save_run(user, messages, script, prompt, runs_dir)

    # M8: 写 fact_sheet.json 到同目录（让 --resume / 续集能找到）
    writer_output = r1["writer_output"]
    fs_path = run_dir / "fact_sheet.json"
    if writer_output and not writer_output.startswith("(无"):
        from src.v1_legacy.fact_sheet import extract_fact_sheet
        fs = extract_fact_sheet(
            writer_output,
            session_id=run_dir.name,
            parent_session_id=parent_session_id,
        )
        fs_path.write_text(
            json.dumps(fs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return {
        "script": script,
        "prompt": prompt,
        "message_count": r2["message_count"],
        "retries_used": r2["retries_used"],
        "validation_failures": r2["validation_failures"],
        "run_dir": run_dir,
        "parent_session_id": parent_session_id,
    }


async def _async_main(args):
    """原 async main 逻辑：CLI 模式（Step 1 / Step 2 / Rewrite / Full）"""
    print("=" * 60)
    print("AutoGen 即梦视频脚本工作流 v0.4.0 (M4 交互式)")
    print("=" * 60)

    try:
        # M11: --rewrite 与 --add 互斥校验
        if args.rewrite and args.add:
            print(f"[错误] --rewrite 和 --add 互斥，请只用一个", file=sys.stderr)
            sys.exit(2)

        # Mode B: 恢复会话
        if args.resume:
            if args.rewrite:
                print(f"恢复会话: {args.resume}")
                print(f"M11 Writer 重写模式")
                print(f"用户反馈: {args.rewrite}")
                result = await run_step2(
                    args.resume, args.rewrite, args.runs_dir, rewrite=True
                )
                if not result.get("rewritten"):
                    sys.exit(1)
                # 输出由 run_step2 内部打印
                return

            print(f"恢复会话: {args.resume}")
            print(f"用户追加: {args.add or '(无)'}")
            result = await run_step2(args.resume, args.add, args.runs_dir)

            print()
            print("=" * 60)
            print("Step 2 完成！")
            print(f"  session_id: {result['session_id']}")
            print(f"  消息总数: {result['message_count']}")
            print(f"  脚本长度: {len(result['script'])} 字")
            print(f"  即梦提示词长度: {len(result['prompt'])} 字")
            print(f"  重试次数: {result['retries_used']}")
            print(f"  产物目录: {result['run_dir']}")
            print("=" * 60)
            return

        # Mode A: 新会话（Step 1）
        if args.raw:
            print(f"自然语言输入: {args.raw}")
            print()
            from src.parser import parse_user_input
            user = await parse_user_input(args.raw)
        else:
            user = DEFAULT_EXAMPLE
            print(f"灵感: {user.inspiration}")
            print(f"时长: {user.duration}s | 镜头数: {user.shot_count}")
            print(f"风格: {user.style_hint}")
            print()

        # M8: 自动检测上一集 fact_sheet（仅在新会话模式）
        parent_session_id: str | None = None
        latest_with_fs = Session.find_latest_session_with_fact_sheet(args.runs_dir)
        if latest_with_fs:
            prev_fs = Session.load_fact_sheet(latest_with_fs, runs_dir=args.runs_dir)
            title = prev_fs.get("title", "(无标题)") if prev_fs else "(无标题)"
            print()
            print(f"[M8 续集检测] 找到上一集：{latest_with_fs} - {title}")
            print(f"  续接上集吗？[y/N/session_id]（直接回车 = 不续接；输入 y = 续接；输入其他 session_id = 续接指定集）")
            try:
                answer = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer.lower() == "y":
                parent_session_id = latest_with_fs
            elif answer and answer.lower() not in ("n", "no"):
                # 视为指定 session_id
                if Session.load_fact_sheet(answer, runs_dir=args.runs_dir):
                    parent_session_id = answer
                else:
                    print(f"  [警告] {answer} 不存在 fact_sheet，按不续接处理")
            if parent_session_id:
                print(f"  [M8] 续集模式已启用，parent_session_id = {parent_session_id}")
            print()

        # --no-pause: 一次跑完（向后兼容）
        if args.no_pause:
            result = await run_full(user, args.runs_dir, parent_session_id=parent_session_id)
            print()
            print("=" * 60)
            print("完成（一次性）！")
            print(f"  消息总数: {result['message_count']}")
            print(f"  脚本长度: {len(result['script'])} 字")
            print(f"  即梦提示词长度: {len(result['prompt'])} 字")
            print(f"  重试次数: {result['retries_used']}")
            print(f"  产物目录: {result['run_dir']}")
            if result.get("parent_session_id"):
                print(f"  续接自: {result['parent_session_id']}")
            print("=" * 60)
            return

        # 默认 Step 1（暂停）
        result = await run_step1(user, args.runs_dir, parent_session_id=parent_session_id)
        print()
        print("=" * 60)
        print("Step 1 完成！请 review writer_output.md 后再 --resume --add 继续：")
        print()
        print(f"  session_id: {result['session_id']}")
        print(f"  Writer 输出长度: {len(result['writer_output'])} 字")
        print(f"  产物目录: {result['run_dir']}")
        if result.get("parent_session_id"):
            print(f"  续接自: {result['parent_session_id']}")
        if result.get("fact_sheet_saved"):
            print(f"  ✓ fact_sheet.json 已保存（M8 续集基础）")
        print()
        print(f"  接着跑：")
        print(f"    python main.py --resume {result['session_id']} --add \"你的修改/灵感\"")
        print()
        print(f"  一次跑完（跳过暂停）：")
        print(f"    python main.py --no-pause --raw \"...\"")
        print("=" * 60)
    except ParseError as e:
        print(f"\n[解析失败] {e}", file=sys.stderr)
        sys.exit(2)


def _parse_args():
    parser = argparse.ArgumentParser(
        description="AutoGen 即梦视频脚本工作流 (v0.4.0 M4 交互式)"
    )
    parser.add_argument(
        "--raw", type=str, help="自然语言灵感文本（不传则用 DEFAULT_EXAMPLE）"
    )
    parser.add_argument(
        "--resume", type=str, metavar="SESSION_ID",
        help="恢复会话：传 session_id（Step 1 输出的目录名）",
    )
    parser.add_argument(
        "--add", type=str, default="",
        help="用户追加的灵感/修改指令（与 --resume 配合使用）",
    )
    parser.add_argument(
        "--rewrite", type=str, default="", metavar="FEEDBACK",
        help="M11 用户反馈：触发 Writer 重写后暂停（与 --add 互斥）",
    )
    parser.add_argument(
        "--no-pause", action="store_true",
        help="不暂停：一次性跑完所有 6 个角色（向后兼容）",
    )
    parser.add_argument(
        "--runs-dir", type=Path, default=Path("runs"),
        help="产物目录（默认 runs/）",
    )
    parser.add_argument(
        "--web", action="store_true",
        help="M7 启动 Web UI（FastAPI + WebSocket，浏览器访问 localhost:8000）",
    )
    parser.add_argument(
        "--web-port", type=int, default=8000,
        help="Web UI 端口（默认 8000）",
    )
    return parser.parse_args()


def main():
    """同步入口：解析参数 → 决定走 uvicorn 还是 asyncio.run

    必须放在 asyncio.run() 外面——uvicorn 自身会创建 event loop，
    如果我们已经在 asyncio.run() 里再调 uvicorn.run() 会冲突。
    """
    args = _parse_args()

    # 加载 .env（OPENAI_API_KEY 等）
    load_dotenv()

    # M7 + M22: --web 模式启动 uvicorn + FastAPI
    #   v2: src.app（RoundRobinGroupChat 物理隔离 Step 1 / WS 协议匹配前端）
    if args.web:
        import uvicorn
        print("=" * 60)
        print("AutoGen 即梦工作流 Web UI v2.0 (M22, v2)")
        print(f"  浏览器打开：http://localhost:{args.web_port}")
        print("  (v1_legacy 已弃用，本入口改走 v2)")
        print("=" * 60)
        from src.app import app
        uvicorn.run(app, host="0.0.0.0", port=args.web_port, log_level="info")
        return

    # CLI 模式：进入 asyncio 跑
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
