"""Test configuration for calendar_interaction_module.

Sets up sys.path so tests can import from the module root (services/,
config.py) the same way app.py does at runtime -- same pattern as
action/interactive/welcome_interaction_module/tests/conftest.py.
"""
import os
import sys

# Add module root so `from services.* import ...` and `import config` work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
