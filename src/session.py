"""Session 状态持久化（M4）

把工作流的中间状态保存到 runs/<session_id>/state.json，
支持跨会话恢复（Step 1 跑完今天关掉，明天 --resume 接着跑）。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .user_input import UserInput


# Session 状态机
STEP_START = "start"             # 初始
STEP_AFTER_WRITER = "after_writer"  # 跑完 Host + Writer，等用户
STEP_COMPLETE = "complete"       # 全部完成


@dataclass
class Session:
    """工作流会话状态（M13：同 session 多集）

    每集共用一个 session：
    - current_episode: 当前是第几集（默认 1，跑完一集 +1）
    - current_inspiration: 本集灵感（每次新集覆盖，不存 list）
    - current_rewrite_feedback: 重写本集的反馈（不入 list）
    - fact_sheet: 每集跑完自动更新（characters 等人设承袭、ending_state 更新）

    目录结构：
        runs/<session_id>/
        ├── state.json          # 本对象
        ├── user_input.json     # UserInput 副本
        ├── writer_output.md    # 本集 Step 1 完成后生成（每次覆盖）
        ├── jimeng_prompt.md    # 旧 session 兼容：单文件最后输出
        ├── prompts/            # M13 新格式：每集 1 个文件
        │   ├── 001.md
        │   ├── 002.md
        │   └── ...
        ├── prompts_index.json  # M13：prompts 目录的索引
        ├── fact_sheet.json     # M13：每集跑完更新
        ├── transcript.md       # 完整对话流
        └── additions.log       # 历史 log（旧 session 可能有，不再生新）
    """
    session_id: str
    user_input_dict: dict  # UserInput 序列化为 dict（避免依赖 dataclass 字段）
    messages: list[dict]   # [{role, content, ts}, ...]
    current_step: str
    current_episode: int = 1              # M13：当前是第几集（默认 1）
    current_inspiration: str = ""         # M13：本集灵感（不存 list）
    current_rewrite_feedback: str = ""    # M13：重写本集反馈（不存 list）
    created_at: str = ""
    updated_at: str = ""
    parent_session_id: str | None = None  # M8: 续集时指向上一集 session_id（旧版兼容）
    fact_sheet_dict: dict | None = None   # M13：会话级 fact_sheet 缓存（每集更新）

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        self.updated_at = datetime.now().isoformat()

    @staticmethod
    def new_id() -> str:
        """生成 session_id：YYYYMMDD_HHMMSS_xxxx（短 hash 避免冲突）"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = secrets.token_hex(2)  # 4 chars
        return f"{ts}_{suffix}"

    @classmethod
    def create(
        cls,
        user: "UserInput",
        runs_dir: Path = Path("runs"),
        parent_session_id: str | None = None,
    ) -> "Session":
        """创建新 Session（Step 1 之前）

        Args:
            user: 用户输入
            runs_dir: 产物根目录
            parent_session_id: M8 旧版续集字段（新 M13 用 session_id 续接，但保留兼容）
        """
        from .user_input import UserInput  # 避免循环导入
        session_id = cls.new_id()
        return cls(
            session_id=session_id,
            user_input_dict=asdict(user) if hasattr(user, "__dataclass_fields__") else dict(user),
            messages=[],
            current_step=STEP_START,
            current_episode=1,
            current_inspiration=user.inspiration if hasattr(user, "inspiration") else "",
            parent_session_id=parent_session_id,
        )

    def save(self, runs_dir: Path = Path("runs")) -> Path:
        """保存到 runs/<session_id>/state.json"""
        run_dir = runs_dir / self.session_id
        run_dir.mkdir(parents=True, exist_ok=True)
        state_path = run_dir / "state.json"
        self.updated_at = datetime.now().isoformat()
        state_path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return state_path

    @classmethod
    def load(cls, session_id: str, runs_dir: Path = Path("runs")) -> "Session":
        """从 state.json 加载

        向后兼容：旧 state.json 可能含 user_additions 字段（list），
        加载时静默丢弃，不写入新字段。
        M15 迁移：旧 messages 没 ep_id 字段 → 补 ep_id=1（首集）。
        """
        state_path = runs_dir / session_id / "state.json"
        if not state_path.exists():
            raise FileNotFoundError(f"Session {session_id} 不存在: {state_path}")
        data = json.loads(state_path.read_text(encoding="utf-8"))
        # 删旧字段（user_additions 已被 current_inspiration 单字段替代）
        data.pop("user_additions", None)
        # 旧字段缺失时给默认值
        data.setdefault("current_episode", 1)
        data.setdefault("current_inspiration", "")
        data.setdefault("current_rewrite_feedback", "")
        data.setdefault("fact_sheet_dict", None)
        # M15 迁移：旧 messages 没 ep_id 字段 → 补 ep_id=1（首集）
        for m in data.get("messages", []):
            m.setdefault("ep_id", 1)
        return cls(**data)

    def add_message(self, role: str, content: str, ep_id: int | None = None) -> None:
        """追加一条消息

        Args:
            role: 角色（Host / Writer / Storyboard / DP / Director / user）
            content: 消息内容
            ep_id: M15 所属集数（None 时默认 = self.current_episode）。
                用于跨集切片时区分"本集" vs "上集"。
        """
        if ep_id is None:
            ep_id = self.current_episode
        self.messages.append({
            "role": role,
            "content": content,
            "ts": datetime.now().isoformat(),
            "ep_id": ep_id,
        })
        self.updated_at = datetime.now().isoformat()

    # ============== M13：本集灵感管理 ==============

    def set_current_inspiration(self, text: str) -> None:
        """设置本集灵感（覆盖式，每次新集替换）

        不再追加到 list——每集只 1 段灵感，由 fact_sheet 沉淀历史剧情。
        """
        self.current_inspiration = text
        self.updated_at = datetime.now().isoformat()

    def set_rewrite_feedback(self, text: str) -> None:
        """设置重写本集的反馈"""
        self.current_rewrite_feedback = text
        self.updated_at = datetime.now().isoformat()

    def clear_rewrite_feedback(self) -> None:
        """跑完重写后清空反馈"""
        self.current_rewrite_feedback = ""
        self.updated_at = datetime.now().isoformat()

    def advance_episode(self) -> None:
        """跑完一集 Step 2 后调用：集数 +1，清空本集灵感/反馈"""
        self.current_episode += 1
        self.current_inspiration = ""
        self.current_rewrite_feedback = ""
        self.updated_at = datetime.now().isoformat()

    def last_messages(self, n: int = 1) -> list[dict]:
        """取最后 n 条消息"""
        return self.messages[-n:] if n > 0 else []

    def writer_output(self, ep_id: int | None = None) -> str | None:
        """取出指定 ep_id 的 Writer 最后一条输出（默认 = current_episode）

        M15：跨集场景下不传 ep_id 可能命中其他集的 Writer。
             调用方应明确传 ep_id 以保证正确性。
        """
        target_ep = ep_id if ep_id is not None else self.current_episode
        for m in reversed(self.messages):
            if m["role"] == "Writer" and m.get("ep_id") == target_ep:
                return m["content"]
        return None

    def sync_writer_output_from_file(self, runs_dir: Path = Path("runs")) -> bool:
        """如果 writer_output.md 与 session.messages 中 Writer 的消息不同，
        用文件内容替换 session.messages 中的 Writer 消息。

        用途：用户编辑 writer_output.md 后跑 --resume，Step 2 用编辑后的版本。

        Returns:
            bool: 是否做了替换（True = 文件被修改并应用）

        行为：
        - writer_output.md 不存在 → 返回 False（向后兼容）
        - 文件是占位符 "(无 ...)" → 返回 False
        - 文件内容与 session 一致 → 返回 False（no-op）
        - 不一致 → 替换并返回 True
        """
        writer_output_path = runs_dir / self.session_id / "writer_output.md"
        if not writer_output_path.exists():
            return False

        file_content = writer_output_path.read_text(encoding="utf-8").strip()
        if not file_content or file_content.startswith("(无"):
            return False

        # M15：只替换本集 ep_id 的 Writer 消息（续集场景下避免误改上集）
        for i, m in enumerate(self.messages):
            if m["role"] == "Writer" and m.get("ep_id") == self.current_episode:
                if self.messages[i]["content"].strip() != file_content:
                    self.messages[i]["content"] = file_content
                    self.updated_at = datetime.now().isoformat()
                    return True
                return False  # 内容相同

        return False  # session 中没有本集 Writer 消息

    def is_paused(self) -> bool:
        """是否处于暂停状态（等用户输入）"""
        return self.current_step == STEP_AFTER_WRITER

    def is_complete(self) -> bool:
        """是否已完成"""
        return self.current_step == STEP_COMPLETE

    def get_user_input(self) -> "UserInput":
        """从 dict 重建 UserInput"""
        from .user_input import UserInput
        return UserInput(**self.user_input_dict)

    # ============== M8: fact_sheet 集成 / M13: 每集更新 ==============

    def get_latest_writer_output(self) -> str:
        """取最新一条 Writer 输出（M13 每集更新 fact_sheet 用）

        比 writer_output() 多一个 fallback：messages 里没有 Writer 时，读文件。
        """
        from_messages = self.writer_output()
        if from_messages:
            return from_messages
        # fallback: 读 writer_output.md
        path = Path("runs") / self.session_id / "writer_output.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def save_fact_sheet(
        self,
        writer_output: str | None = None,
        runs_dir: Path = Path("runs"),
        fact_sheet: dict | None = None,
    ) -> dict | None:
        """M13：保存 fact_sheet 到 runs/<session_id>/fact_sheet.json

        两种调用方式：
        1. save_fact_sheet(writer_output=..., runs_dir=...)  —— 首集，从 writer 产物提取
        2. save_fact_sheet(fact_sheet={...}, runs_dir=...)    —— 续集/重写，直接传 dict

        Returns:
            fact_sheet dict；输入为空时返回 None
        """
        if fact_sheet is not None:
            fs = fact_sheet
        elif writer_output:
            from .v1_legacy.fact_sheet import extract_fact_sheet
            if not writer_output or writer_output.startswith("(无"):
                return None
            fs = extract_fact_sheet(
                writer_output,
                session_id=self.session_id,
                parent_session_id=self.parent_session_id,
            )
        else:
            return None
        run_dir = runs_dir / self.session_id
        run_dir.mkdir(parents=True, exist_ok=True)
        fs_path = run_dir / "fact_sheet.json"
        fs_path.write_text(
            json.dumps(fs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # M13：同步缓存到 session.fact_sheet_dict（下次 load 时直接拿到最新）
        self.fact_sheet_dict = fs
        return fs

    @staticmethod
    def load_fact_sheet(session_id: str, runs_dir: Path = Path("runs")) -> dict | None:
        """从 runs/<session_id>/fact_sheet.json 加载 fact_sheet

        Returns:
            fact_sheet dict；文件不存在时返回 None
        """
        fs_path = runs_dir / session_id / "fact_sheet.json"
        if not fs_path.exists():
            return None
        return json.loads(fs_path.read_text(encoding="utf-8"))

    @staticmethod
    def find_latest_session_with_fact_sheet(runs_dir: Path = Path("runs")) -> str | None:
        """扫描 runs/ 下所有 session，找到最近一个有 fact_sheet.json 的 session_id

        排序依据：优先用 fact_sheet.json 的 extracted_at 字段（ISO 字符串可直接比大小），
        缺失时 fallback 到 state.json 的 created_at。

        Returns:
            session_id 字符串；没找到返回 None
        """
        if not runs_dir.exists():
            return None
        candidates: list[tuple[str, str]] = []  # (timestamp, session_id)
        for run_dir in runs_dir.iterdir():
            if not run_dir.is_dir():
                continue
            fs_path = run_dir / "fact_sheet.json"
            state_path = run_dir / "state.json"
            if not fs_path.exists():
                continue
            timestamp = ""
            # 优先 fact_sheet.json
            try:
                fs = json.loads(fs_path.read_text(encoding="utf-8"))
                timestamp = fs.get("extracted_at", "") or timestamp
            except (json.JSONDecodeError, OSError):
                pass
            # fallback state.json
            if not timestamp and state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    timestamp = state.get("created_at", "")
                except (json.JSONDecodeError, OSError):
                    continue
            if timestamp:
                candidates.append((timestamp, run_dir.name))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    @staticmethod
    def delete(runs_dir: Path, sid: str) -> bool:
        """删除 session 目录（含 state.json + 所有产物）

        Args:
            runs_dir: runs 根目录
            sid: 要删除的 session_id

        Returns:
            True if directory existed and was removed;
            False if directory didn't exist (no-op)
        """
        target = runs_dir / sid
        if not target.exists():
            return False
        if not target.is_dir():
            raise ValueError(f"{target} 不是目录")
        shutil.rmtree(target)
        return True


def serialize_message(msg: Any) -> dict:
    """把 AutoGen 消息对象序列化为 dict

    兼容 TextMessage / UserMessage 等——只看 source + content。
    """
    src = getattr(msg, "source", "") or "?"
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        # content blocks（OpenAI 多模态格式）
        content = "\n".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    if hasattr(content, "to_text"):
        try:
            content = content.to_text() or ""
        except Exception:
            pass
    return {
        "role": src,
        "content": str(content) if content else "",
        "ts": datetime.now().isoformat(),
    }
