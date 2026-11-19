# Container image for Azure Container Apps (or any Docker host): serves the app on port 8000.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.4 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
# runtime dependencies only: no dev tools, no geopandas/h3 (data preparation runs outside the container)
RUN uv sync --frozen --no-default-groups --no-install-project

COPY . .
# the commit the image was built from, reported by /health so the pipeline can tell revisions apart
ARG IMAGE_TAG=dev
ENV IMAGE_TAG=$IMAGE_TAG RELEASE_CACHE=/tmp/releases
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "app.py"]
