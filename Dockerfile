FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# EXTRAS=ml 이면 학습용 torch를 같이 설치한다 (gpu 큐 워커 이미지).
# TORCH_INDEX: CPU 전용은 .../whl/cpu, CUDA 노드는 .../whl/cu124 등으로 바꿔 빌드한다.
ARG EXTRAS=""
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

COPY pyproject.toml alembic.ini ./
COPY factory_agent ./factory_agent
COPY migrations ./migrations
RUN if [ -n "$EXTRAS" ]; then pip install ".[$EXTRAS]" --extra-index-url "$TORCH_INDEX"; else pip install .; fi \
    && useradd --create-home --uid 10001 app

USER app
EXPOSE 8000
CMD ["uvicorn", "factory_agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
