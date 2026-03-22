"""Flask web UI for Docker Janitor."""

import os

from flask import Flask


def create_app(config_path: str = "configs/janitor.yaml") -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["JANITOR_CONFIG_PATH"] = config_path
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

    from janitor.web.routes import bp

    app.register_blueprint(bp)

    return app
