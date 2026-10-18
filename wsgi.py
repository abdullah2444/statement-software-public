"""Entry point for the production web server (Gunicorn).

    gunicorn -c gunicorn.conf.py wsgi:application

`python app.py` still works for running the app on your own computer.
"""

from app import app as application, start_app_services

start_app_services()
