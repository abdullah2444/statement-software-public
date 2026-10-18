"""Gunicorn settings for the app container. Read by: gunicorn -c gunicorn.conf.py wsgi:application"""

import os

bind = f"{os.environ.get('HOST', '0.0.0.0')}:{os.environ.get('PORT', '18451')}"

# One process, many threads. The app runs its backup, cloud-backup and update
# jobs inside the process and keeps some state in memory (login limits, the
# backup/restore gate), so there must be exactly one worker process.
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("WEB_THREADS", "8"))

# Big receipt uploads and backup downloads can take a while on slow phones.
timeout = 300
graceful_timeout = 30
keepalive = 5
# No max_requests: restarting the worker would cut off a running backup.

accesslog = "-"
errorlog = "-"
loglevel = "info"
# Don't log query strings: they can contain search terms or client names.
access_log_format = '%(h)s "%(m)s %(U)s" %(s)s %(b)s %(L)ss'
