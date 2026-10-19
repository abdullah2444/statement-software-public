FROM python:3.11-slim

WORKDIR /app

# Install WeasyPrint system dependencies, and restic for encrypted cloud backups
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
    libffi-dev libcairo2 libglib2.0-0 fonts-dejavu-core \
    restic ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies at the exact versions in constraints.txt; also update pip
# and setuptools themselves, which ship with known security problems.
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir --upgrade pip setuptools \
    && pip install --no-cache-dir -r requirements.txt -c constraints.txt

# The app runs as this user, not root (docker-entrypoint.sh switches to it)
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app

# Copy app files (owned by root, so the app can't change its own code)
COPY VERSION app.py wsgi.py gunicorn.conf.py cloud_backup.py update_helper.py ./
COPY templates/ templates/
COPY static/ static/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

# Create data directory for database, uploads, backups, and the updater's shared folder
RUN mkdir -p /data /data/uploads /data/backups /data/updater && chown -R app:app /data

# Environment variables
ENV PORT=18451
ENV FLASK_DEBUG=0
ENV HOST=0.0.0.0
ENV DATABASE_PATH=/data/firefly_statement.db
ENV UPLOAD_DIR=/data/uploads
ENV BACKUP_DIR=/data/backups
ENV HOME=/home/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 18451

# Docker marks the container unhealthy if the app or its database stops answering
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request, json, sys; r = urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '18451'), timeout=8); sys.exit(0 if json.load(r).get('ok') else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:application"]
