FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app
# Dependencies first so code-only changes rebuild fast.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
# Cloud Run sets PORT; app.py listens on 0.0.0.0:$PORT when it's present.
ENV PORT=8080
CMD [".venv/bin/python", "app.py"]
