# Self-contained deep-research UI image.
#
# Builds from the ui/ context ALONE — no agentfield source, no `af` CLI binary.
# The UI talks to the AgentField control plane purely over HTTP (server.py
# cp_get/cp_post), so it needs nothing but Python + Flask + SuperTokens.
#
# Railway: set the service root directory to `ui/` and
# RAILWAY_DOCKERFILE_PATH=Dockerfile so this file is the build recipe.
FROM python:3.10-slim

WORKDIR /app

# Runtime deps — the same set the old cross-repo Dockerfile installed for the UI.
RUN pip install --no-cache-dir \
    flask \
    flask-cors \
    supertokens-python \
    markdown \
    "psycopg[binary]"

# Copy the ui/ build context into an inner `ui/` package so the app's absolute
# `ui.workspace` / `ui.tenancy` imports resolve at runtime (root dir = ui/).
COPY . /app/ui/

# app.py binds UI_BIND_HOST (default "::" for Railway private net) on DR_UI_PORT.
EXPOSE 8899

# Run as a module so the `ui` package is importable (cwd /app is on sys.path).
CMD ["python", "-m", "ui.app"]
