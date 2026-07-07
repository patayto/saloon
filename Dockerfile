# syntax=docker/dockerfile:1
FROM python:3.13-slim

WORKDIR /app

RUN pip install uv --quiet

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

COPY . .

EXPOSE 8000
CMD [".venv/bin/python", "manage.py", "runserver", "0.0.0.0:8000"]
