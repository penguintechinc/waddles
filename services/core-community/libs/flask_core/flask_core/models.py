"""Backwards-compatibility shim.

The models package (flask_core/models/) now owns db and all models.
This file is shadowed by the package directory in Python's import system,
but is kept for clarity. All imports resolve to models/__init__.py.
"""
# This file is never imported when models/ package directory exists.
# See models/__init__.py for the actual db instance and model imports.
