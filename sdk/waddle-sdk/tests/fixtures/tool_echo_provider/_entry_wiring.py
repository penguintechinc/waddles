"""Hand-authored entry wiring: importing ``app`` registers the ``echo`` tool."""

from __future__ import annotations

import app  # noqa: F401 - import side effect registers @tool handlers
