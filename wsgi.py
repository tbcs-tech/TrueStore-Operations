"""
wsgi.py
=======
Production WSGI entry point. `python app.py` uses Flask's built-in
development server — single-threaded, not designed to survive real
traffic, and Flask itself warns against it. Use a real WSGI server
against this module instead:

    Linux/Mac (gunicorn):
        gunicorn -w 4 -b 127.0.0.1:8000 wsgi:app

    Windows (waitress):
        waitress-serve --host=127.0.0.1 --port=8000 wsgi:app

Bind to 127.0.0.1 (localhost only), not 0.0.0.0 — put this behind a
reverse proxy (nginx/Caddy) that terminates HTTPS and forwards to it.
See README.md "Deploying to production" for the full walkthrough
(reverse proxy config, systemd service, backups, environment variables).
"""
from app import app

if __name__ == "__main__":
    app.run()
