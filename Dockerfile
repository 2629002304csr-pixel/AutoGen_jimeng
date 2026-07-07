FROM python:3.11-slim AS base

# 防止 .pyc 写入、stdout/stderr 不缓冲（uvicorn 日志实时输出）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 装依赖（先 COPY pyproject 充分利用 Docker 缓存层）
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# 拷源码
COPY src/ ./src/
COPY static/ ./static/
COPY prompts/ ./prompts/

# runs/ 留给 volume 挂载（不打包个人 session）
# 创建空目录以确保所有权正确
RUN mkdir -p /app/runs

EXPOSE 8000

# 健康检查（curl 检测 /docs 是否可访问；PORT 可能是 Railway 分配的）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request, os; urllib.request.urlopen(f'http://localhost:{os.getenv(\"PORT\", 8000)}/docs', timeout=3)" || exit 1

# Railway / Render / fly.io 都会设 PORT 环境变量；本地 docker compose 默认 8000
# 用 sh -c 让 $PORT 在容器启动时展开
CMD ["sh", "-c", "uvicorn src.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
