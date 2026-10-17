# One image runs the web app, the RQ worker and the scheduler. Production: gunicorn, non-root, no dev tools.
# Development (docker-compose.yml) builds with INSTALL_DEV=true and overrides the command with Flask's reloader.
FROM python:3.14-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ARG INSTALL_DEV=false
ARG APP_COMMIT=dev
ENV APP_COMMIT=${APP_COMMIT}
COPY requirements.txt requirements-dev.txt ./
RUN if [ "$INSTALL_DEV" = "true" ]; then pip install --no-cache-dir -r requirements-dev.txt; else pip install --no-cache-dir -r requirements.txt; fi
COPY . .
RUN useradd --system --uid 10001 --home-dir /srv app && chown -R app /srv
USER app
EXPOSE 8431
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8431/healthz', timeout=4)" || exit 1
CMD ["gunicorn", "app:create_app()", "--bind", "0.0.0.0:8431", "--workers", "2", "--threads", "4", "--timeout", "90", "--access-logfile", "-"]
