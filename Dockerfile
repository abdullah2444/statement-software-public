FROM python:3.11-slim

WORKDIR /app

# Install WeasyPrint system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
    libffi-dev libcairo2 libglib2.0-0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY app.py .
COPY templates/ templates/
COPY static/ static/

# Create data directory for database, uploads, and backups
RUN mkdir -p /data /data/uploads /data/backups

# Environment variables
ENV PORT=18451
ENV FLASK_DEBUG=0
ENV HOST=0.0.0.0
ENV DATABASE_PATH=/data/firefly_statement.db
ENV UPLOAD_DIR=/data/uploads
ENV BACKUP_DIR=/data/backups

EXPOSE 18451

CMD ["python", "app.py"]
