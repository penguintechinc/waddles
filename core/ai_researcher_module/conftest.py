"""Shared pytest setup for `core/ai_researcher_module`.

Two ordering fixes, both applied before any test module (or `app.py`
itself) gets a chance to import for the first time in this process:

1. `app.py` does `sys.path.insert(0, .../libs)` then `from flask_core
   import ...` -- if the *first* `flask_core` import in the process comes
   from that sys.path-inserted route rather than the pip-installed
   `waddlebot-flask-core` package, `flask_core` resolves as a broken
   namespace package instead of the real one (mem0: "sys.path.insert
   caused app.py to incorrectly resolve flask_core as a broken namespace
   package instead of the pip-installed one"). Importing the real package
   here first populates `sys.modules['flask_core']`, so `app.py`'s own
   import is a no-op cache hit against the correct module.
2. `config.py`'s `Config` class body calls `require_secret_key()` (fails
   closed on an unset/placeholder `SECRET_KEY` in a production-like
   environment) and reads `SERVICE_API_KEY`/`CORS_ALLOWED_ORIGINS` at
   *class-body eval time* -- i.e. at first `import config`/`import app`,
   not per-test. Setting them here (module scope, before collection)
   rather than in an individual test's `monkeypatch` guarantees they're in
   place before that one-time import happens, however pytest orders test
   collection.
"""

import os

os.environ.setdefault("SECRET_KEY", "test-only-secret-not-a-real-placeholder")
os.environ.setdefault("SERVICE_API_KEY", "test-only-service-key")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "https://allowed.example.com")

import flask_core  # noqa: E402,F401
