FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ ./

RUN useradd --system --uid 10001 --no-create-home mcp
USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0) if os.getenv('MCP_TRANSPORT','http')!='http' else urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('MCP_PORT','8000'), timeout=4)"

CMD ["python", "server.py"]
