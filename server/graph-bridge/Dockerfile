FROM python:3.12-slim

# graphiti-core 的 falkordb extra 与 fastapi/uvicorn 一并从 PyPI 装机（见 requirements.txt）。
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# 单 worker 串行入图（与 SDK 侧派发器一致）：图侧 LLM 调用不与主链路的 LLM 调用争抢。
ENV SEMAPHORE_LIMIT=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
