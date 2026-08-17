# ScopiEngine's zero-configuration default (a SQLite database file) needs
# nothing beyond the Python stdlib, so this image installs no system packages
# at all — just the project itself and its pure-Python/wheel dependencies.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install from project metadata before copying the rest of the tree, so an
# application-code-only change doesn't invalidate the dependency-install layer.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

# Run as a non-root user. /data is where the default `sqlite:////data/scopi.db`
# DSN below writes; docker-compose.yml mounts it as a named volume so the
# database survives a container recreate.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin scopi \
    && mkdir -p /data \
    && chown -R scopi:scopi /data /app
USER scopi

ENV SCOPI_STORAGE="sqlite:////data/scopi.db" \
    SCOPI_HOST=0.0.0.0 \
    SCOPI_PORT=9500 \
    SCOPI_LOG_FORMAT=json

EXPOSE 9500

# No curl/wget in the slim base image — urllib is already there.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s CMD \
    python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:9500/_health', timeout=2)"

ENTRYPOINT ["scopi"]
CMD ["serve", "--host", "0.0.0.0", "--port", "9500"]
