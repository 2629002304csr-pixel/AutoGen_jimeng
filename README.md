# 即梦视频脚本工作流

基于 Microsoft AutoGen 的多 Agent 工作流，把 1-3 句灵感变成即梦/豆包可用的视频生成提示词。

5 个角色顺序讨论 + 导演裁决：

- **主持人** 整理用户参数 → **作家** 写故事梗概 → **分镜师** 出分镜表 → **摄影指导** 配摄影参数 → **导演** 拍板输出最终脚本

最终产物：
- `script.md` — 多镜头 Markdown 脚本（生产用）
- `prompts/<集号>.md` — 即梦视频生成提示词（直接喂即梦/豆包）

## 两种使用方式

### A. CLI（一次性跑完）

```bash
pip install -e .
python main.py --raw "15秒赛博朋克女黑客短片，4K画质冷暖对比必须有雨不能出现清晰人脸"
```

跑完产物在 `runs/<session_id>/`。

支持 `--resume <sid> --add "..."` 恢复续写，`--rewrite "..."` 让 Writer 重写本集。

### B. Web UI（推荐，多集系列）

```bash
python main.py --web
# 浏览器打开 http://localhost:8000
```

特点：
- **流式输出**：每个角色输出实时刷新
- **侧栏 session 切换**：所有集在同一个 session 下，fact_sheet 自动继承
- **多集写作**：写下一集时自动注入前情（人设继承 + 故事线累积 + 最近结局）
- **API Key 浏览器配置**：每个用户填自己的 key，存 localStorage，不进服务器

## 配置 API Key

默认走 **DeepSeek**（国内最快最便宜：V3 输入 1 元/百万 token）。

| 供应商 | 拿 key 的地址 |
|--------|---------------|
| DeepSeek（推荐） | https://platform.deepseek.com/api_keys |
| Qwen（通义千问） | https://dashscope.console.aliyun.com/apiKey |
| OpenAI | https://platform.openai.com/api-keys |

可以：
1. 编辑 `.env`（`cp .env.example .env` 后改）
2. Web UI 侧栏点 ⚙️ 模型设置（更适合朋友多用户场景）

## 项目结构

```
.
├── main.py                  # CLI 入口（--raw / --web / --resume）
├── pyproject.toml
├── prompts/                 # 6 个 system prompt
│   ├── host.md / writer.md / storyboard.md / dp.md / director.md
│   └── parser.md            # 自然语言 → 结构化参数
├── src/
│   ├── app.py               # FastAPI /api/v2/* 路由（v2 路径）
│   ├── v1_legacy/           # 早期 v1 实现（兼容保留）
│   ├── config.py            # ModelConfig + 客户端工厂
│   ├── workflow.py          # 主工作流（SelectorGroupChat + 多集续写）
│   ├── session.py           # 跨会话状态持久化
│   ├── fact_sheet.py        # 跨集人物/世界观/剧情线累积
│   ├── validator.py         # 格式校验 + 自动重试
│   └── ...
├── static/
│   └── index.html           # Web UI（React UMD 单文件，无 build step）
├── tests/                   # ~398 个单元测试
└── runs/                    # 运行产物（每个 session 一个目录）
    └── <session_id>/
        ├── state.json
        ├── writer_output.md
        ├── fact_sheet.json
        ├── transcript.md
        └── prompts/
            ├── 001.md
            └── ...
```

## 部署

把工具挂在网页上，朋友浏览器打开就用，每个朋友自己填 key。

### 本地 Docker（开发）

```bash
docker compose up -d --build
# http://localhost:8000
```

### VPS / PaaS（给朋友用）

| 平台 | 难度 | 备注 |
|------|------|------|
| Railway | ⭐ | GitHub 仓库 → 自动部署 → `xxx.up.railway.app` 5 分钟上线 |
| Render | ⭐ | 免费层会休眠 |
| fly.io | ⭐⭐ | 全球边缘节点 |
| Hetzner 等 VPS | ⭐⭐ | $5/月，跑 `docker compose up -d` + cloudflared tunnel 反代 |

详细步骤见 `策划案.md` §"分发方式"章节。

### 部署后清单

- [ ] 浏览器看到主页（"👋 输入灵感开始生成视频脚本"）
- [ ] 第一次 ⚙️ 设置 → 填 key → 测试成功
- [ ] 输入灵感 → Step 1 → Step 2 → 看到脚本
- [ ] 不同浏览器（不同 localStorage）独立填自己的 key 互不干扰
- [ ] 服务器上 `ls runs/` 确认没有 `_user_config.json`（key 不进文件）

## 常用命令

```bash
# 跑回归测试
pytest tests/ -q

# 跑单测 + 覆盖率
pytest tests/ --cov=src

# 本地开发模式启动 Web UI（带 reload）
uvicorn src.app:app --reload --port 8000
```

## 参考

- 设计文档：[策划案.md](./策划案.md)
- AutoGen：https://microsoft.github.io/autogen/
- 即梦（豆包）：https://jimeng.jianying.com/
