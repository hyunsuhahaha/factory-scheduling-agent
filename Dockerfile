FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY factory_agent ./factory_agent
RUN pip install . && useradd --create-home --uid 10001 app

USER app
EXPOSE 8000
CMD ["uvicorn", "factory_agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
