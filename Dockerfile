# 矿权日报 Agent —— 统一镜像，按 CMD 启动不同组件
FROM python:3.11-slim

# 系统依赖：PyMuPDF/pdfplumber 所需的轻量库
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用层缓存），再装项目本体
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir . \
    && pip uninstall -y mining-daily-agent

COPY mda ./mda
COPY scripts ./scripts
COPY data ./data
RUN pip install --no-cache-dir .

# 非 root 运行
RUN useradd -m appuser
RUN mkdir -p /app/data/cache /app/reports && chown -R appuser:appuser /app
USER appuser

# 默认启动 API；MCP Server 用 docker-compose 的 command 覆盖
CMD ["python", "-m", "mda.api.main"]
