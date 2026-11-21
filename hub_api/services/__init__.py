"""hub-api service layer -- business logic ported from Node's inline-in-controller SQL.

Node's `authController.js`/`identityController.js`/`passkeyController.js`/
`userManagementController.js`/`profileController.js` had no separate
service layer (queries lived directly in the Express controllers). This
package introduces one during the port -- routes (`blueprints/v1/*.py`)
stay thin (auth/tenant/DTO wiring only per `blueprints/v2/platform.py`'s
pattern), all DB/business logic lives here, off the event loop via
`flask_core.database.AsyncDAL`. See `hub_api/PORTING.md` for the recipe.

Per the migration plan's checklist (§4 step 3), each ported controller
group gets its own `services/<group>_*.py` module so blueprint handlers
stay thin (route/auth/DTO only) and I/O lives off the Quart handler body.
The Bot module (M5, `blueprints/v1/bot.py`) follows the same pattern:
async service functions the `bot` blueprint group calls into -- pydal
against existing tables (no schema change) and httpx proxy calls to the
same downstream services the Node controllers called
(`server-manager-service`, `ai-interaction`, local Ollama), per
docs/plans/2026-08-31-hubapi-node-to-quart-migration.md M5.

Each ``community_*.py`` module here is the service layer for one ported
Community-module (engagement) controller (M6): pydal/HTTP-proxy I/O the
corresponding ``blueprints/v1/community_*.py`` blueprint calls into. Kept
separate from the blueprint modules per the migration plan's checklist
(`Service -> async service fn (I/O off the event loop)`), even though
this monorepo's established convention (`services/core-community/app.py`,
`blueprints/v2/platform.py`) calls the raw synchronous `pydal` `dal`
directly rather than wrapping every query in `asyncio.to_thread` -- see
`community_common.py`'s docstring for why that established pattern is
followed here too.
"""

from __future__ import annotations
