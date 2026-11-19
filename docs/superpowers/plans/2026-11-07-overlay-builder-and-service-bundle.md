# Rudimentary Overlay Builder & Overlay Service Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a StreamElements-style full-screen composite Overlay Builder (`1920×1080` virtual coordinate canvas) alongside Waddles' existing static individual overlays (`media`, `music`/`queue`, `alert_box`, `chat`, `goals`, `ticker`, `crawler`), backed by a general **`waddles.core.service.overlay` service bundle** whose MCP tools (`add_component`, `update_component`, `remove_component`, `list_components`, `set_canvas_theme`, `register_widget`, `push_widget`) manage layout, custom widget registration, and live widget output (`sanitized_html`, `image`, or `text`). Any **presentation-layer app bundle** (similar to gateway, process, and action stage bundles—for example, a Star Wars–styled Stream Avatars app bundle, a music player bundle, or a custom alert bundle) can register its custom overlay components/outputs with `waddles.core.service.overlay`, and the Overlay Designer automatically discovers and exposes in its palette any widgets registered by bundles enabled for the active community.

**Architecture:**
1. **Static Overlays Preserved + Composite Scene Added**:
   - Existing static per-surface browser sources (`GET /{overlay_code}/{surface}?key=<VIEW_KEY>` and `GET /surfaces/{overlay_code}/view?surfaces=...&key=<VIEW_KEY>`) remain 100% intact for streamers who prefer separate OBS browser sources.
   - A new full-screen composite scene endpoint (`GET /{overlay_code}/scene?key=<VIEW_KEY>`) renders a single `1920×1080` transparent stage that positions placed component instances at `(x, y, width, height, z_index)` and subscribes over SSE to both `AppState::frame_hub` (`/{overlay_code}/{surface}/live`), `SurfaceHub` (`/surfaces/{overlay_code}/live?surfaces=...`), and custom presentation-bundle widget streams (`/{overlay_code}/widgets/live?key=<VIEW_KEY>`).
   - **Standalone Custom Output Routes (`GET /{overlay_code}/widget/{widget_id}?key=<VIEW_KEY>`)**: When a presentation app bundle registers a custom overlay output (`output_mode: "both" | "standalone_or_canvas"`), streamers can EITHER drop it as a component onto the composite `/{overlay_code}/scene` canvas OR load it as its own dedicated standalone OBS browser source URL.
2. **General Overlay Service Bundle (`waddles.core.service.overlay`)**:
   - A `kind: service` Rust WASM bundle on `world stage-tools` (`waddle:bundle/tool-provider#call`) that owns canonical layout, widget-registration, and widget-output tools:
     - Layout management (`required_scope: community:admin`): `add_component`, `update_component`, `remove_component`, `list_components`, `clear_layout`, `set_canvas_theme`.
     - Presentation App Bundle Registration & Live Output: `register_widget` (idempotently registers a bundle's custom widget descriptor, output kind [`sanitized_html` | `image` | `text` | `surface`], default `(x, y, width, height, z_index)`, and configurable `props_schema`) and `push_widget` (pushes live `sanitized_html`, `image`, or `text` frames for a `widget_id` to all connected composite scenes and standalone widget browser sources in the community).
3. **Presentation-Layer App Bundles (`stages.presentation` + `register_widget` / `push_widget`)**:
   - Just like bundles can plug into `process` and `action` stages (`_ALLOWED_STAGES = {"process", "action", "presentation"}` in `bundle_manifest_v2.py`), developers can build **presentation app bundles** (e.g., `waddles.community.overlay.starwars_avatars`, `waddles.core.example.dj`, `waddles.core.overlay.alerts`).
   - When loaded/enabled for a community, the presentation app bundle registers its widget(s) with `waddles.core.service.overlay` via `register_widget` (and/or declares `overlay_widgets:` in `bundle.yaml` / `hub-manifest.yaml` for static seeding) and pushes live updates via `push_widget` (`output_kind: "sanitized_html" | "image" | "text"`).
   - **Server-Side HTML/CSS Sanitization (`ammonia` allowlist in `svc-presentation` / `overlay` host)**: When a presentation app bundle outputs `sanitized_html` (e.g., positioned avatar sprites, health bars, speech bubbles, or styled cards for a Star Wars–style Stream Avatars overlay), the Rust host sanitizes the markup through a strict `ammonia` allowlist (`div, span, p, strong, em, b, i, br, ul, ol, li, img, svg, path, circle, rect, g, text` with safe presentational/animation classes and bounded inline positioning, stripping all `<script>`, `on*` event handlers, `javascript:`/`data:text/html` URIs, `<iframe/object/embed/form/style>`, and validating all `img[src]` against `https://` + allowed hosts) and detokenizes any `{user:<uuid>}` tokens via `OverlayDetokenizer` **before** the frame ever reaches SSE or the browser DOM.

**Tech Stack:**
- **Data Plane / Presentation**: Rust (`core/svc_presentation`, `core/overlay_schema`, `core/bundle_host_surface`, `core/bundle_executor`, `ammonia` HTML sanitizer)
- **Service & Presentation App Bundles**: Rust (`bundles/rust/overlay-service`, `bundles/rust/dj`, `bundles/rust/stream-alerts`, and example presentation bundle `bundles/rust/starwars-avatars`) targeting `wasm32-wasip2` (`world stage-tools` & `world stage-next-tools`)
- **Control Plane API**: Python 3.13 + Quart (`hub_api/blueprints/v1/overlay.py`, `hub_api/services/overlay_service.py`, `hub_api/services/bundle_manifest_v2.py`, `quart-schema` DTOs)
- **Frontend Designer**: React 18 + Vite + TailwindCSS v4 (`admin/hub_module/frontend/src/pages/admin/AdminStreamOverlays.jsx` + `OverlayBuilderTab.jsx`)

---

## 1. End-to-End Architecture

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                 Presentation-Layer & App Bundles (WASM — Plug & Play)                  │
│  ┌──────────────────────────┐ ┌──────────────────────────┐ ┌────────────────────────┐  │
│  │ waddles.core.example.dj  │ │ waddles.core.overlay.    │ │ Custom Presentation    │  │
│  │ • Album Cover + Details  │ │   alerts                 │ │ App Bundle (e.g.       │  │
│  │ • Song Request Queue     │ │ • Sub/Follow/Cheer/Raid  │ │   Star Wars Avatars)   │  │
│  └────────────┬─────────────┘ └────────────┬─────────────┘ └───────────┬────────────┘  │
│               │                            │                           │               │
│               │  tool.invoke("waddles.core.service.overlay", ...)      │               │
│               │  • register_widget(widget_id, output_kind, defaults)   │               │
│               │  • push_widget(widget_id, { html | image_url | text }) │               │
│               ▼                            ▼                           ▼               │
│  ┌──────────────────────────────────────────────────────────────────────────────────┐  │
│  │          General Overlay Service Bundle: waddles.core.service.overlay            │  │
│  │  Exported MCP Tools (waddle:bundle/tool-provider#call on world stage-tools):     │  │
│  │   • register_widget(widget_id, display_name, output_kind, default_layout, ...)   │  │
│  │   • push_widget(widget_id, output_kind, html?, image_url?, text?, extra?)        │  │
│  │   • add_component(widget_id, x, y, width, height, z_index, props)                │  │
│  │   • update_component(instance_id, x?, y?, width?, height?, z_index?, props?)     │  │
│  │   • remove_component(instance_id) / clear_layout()                               │  │
│  │   • list_components() / list_registered_widgets() / set_canvas_theme(...)        │  │
│  └─────────────────────────────────────────┬────────────────────────────────────────┘  │
└────────────────────────────────────────────┼───────────────────────────────────────────┘
                                             │ Host Capability (`interface overlay`)
                                             │ + Rust Server-Side Sanitizer (`ammonia`)
                                             │ + `OverlayDetokenizer` (`{user:<uuid>}`)
                                             ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                           PostgreSQL (Tenant/Community Scoped)                         │
│  • overlay_widget_registry (id BIGINT PK, public_id UUID, tenant_id, community_id,     │
│    bundle_app_id, widget_id, display_name, output_kind SMALLINT, surface_id,           │
│    default_x, default_y, default_width, default_height, default_z, props_schema JSONB) │
│  • overlay_scene_components (id BIGINT PK, public_id UUID, tenant_id, community_id,    │
│    bundle_app_id, widget_id, x_px, y_px, width_px, height_px, z_index,                 │
│    status_code SMALLINT, props JSONB)                                                  │
└─────────────────────┬────────────────────────────────────────────────┬─────────────────┘
                      │                                                │
                      ▼                                                ▼
┌───────────────────────────────────────────┐    ┌───────────────────────────────────────┐
│          hub-api (Control Plane)          │    │  svc-presentation (Rust Data Plane)   │
│  • Intersects `overlay_widget_registry`   │    │  • Static overlays (unchanged):       │
│    + manifest `overlay_widgets` with      │    │    GET /{code}/{surface}?key=...      │
│    `app_installations` enabled for        │    │  • Composite Full-Screen Scene:       │
│    active `community_id`                  │    │    GET /{code}/scene?key=...          │
│  • Proxies Designer layout actions to     │    │  • Standalone Custom Bundle Output:   │
│    `waddles.core.service.overlay` tools   │    │    GET /{code}/widget/{wid}?key=...   │
└─────────────────────┬─────────────────────┘    └───────────────────┬───────────────────┘
                      │                                              │
                      ▼                                              ▼
┌───────────────────────────────────────────┐    ┌───────────────────────────────────────┐
│     Web UI: Overlay Builder Designer      │    │     OBS Browser Source (1920×1080)    │
│  • Automatically shows palette items from │    │  • Single URL renders all placed      │
│    all active community presentation apps │    │    widgets at (x, y, w, h, z)         │
│  • Drag/resize on 1920×1080 stage         │    │  • Renders structured surfaces PLUS   │
│  • X/Y/W/H/Z & custom props inspector     │    │    custom `sanitized_html`, `image`,  │
│  • One-click Test Event & Copy OBS URL    │    │    and `text` presentation outputs    │
└───────────────────────────────────────────┘    └───────────────────────────────────────┘
```

---

## 2. Presentation-Layer App Bundles & Output Kinds (`sanitized_html`, `image`, `text`, `surface`)

Just as Waddles supports bundles for **gateway/connector**, **process**, and **action** stages, any developer can author a **presentation app bundle** (declaring `stages.presentation` and/or calling `waddles.core.service.overlay` via `tool.invoke`).

### 2.1 How a Presentation App Bundle Works (Example: Star Wars–Style Stream Avatars)
Suppose a developer builds `waddles.community.overlay.starwars_avatars`:
1. **Install & Enable**: When the bundle is enabled for a community, its initialization / presentation stage (or manifest `overlay_widgets` registration) calls `waddles.core.service.overlay` → `register_widget`:
   - `widget_id`: `"starwars_avatars.stage_strip"`
   - `display_name`: `"Galactic Stream Avatars (Jedi / Sith Walkers)"`
   - `output_kind`: `"sanitized_html"` (or integer code `3 = SanitizedHtml`)
   - `allow_standalone_output`: `true` (gives the streamer both a draggable component in the Overlay Builder **and** a standalone overlay URL `/{overlay_code}/widget/starwars_avatars.stage_strip?key=<VIEW_KEY>`)
   - `default_layout`: `{ "x": 0, "y": 920, "width": 1920, "height": 160, "z_index": 35 }`
   - `props_schema`: `{ "faction_theme": { "type": "string", "enum": ["rebel", "empire", "mandalorian"], "default": "rebel" }, "max_avatars": { "type": "integer", "default": 25, "minimum": 1, "maximum": 60 } }`
2. **Automatic Designer Discovery**: Because `waddles.community.overlay.starwars_avatars` is enabled in the user's active community, the **Overlay Builder** in the Web UI automatically displays **"Galactic Stream Avatars (Jedi / Sith Walkers)"** in the left-hand component palette. The streamer drags it onto the bottom of their `1920×1080` canvas (or clicks **+ Add to Overlay**).
3. **Live Runtime Pushes (`push_widget`)**: As chatters talk, use `!jump`, `!duel`, or join the stream, the bundle's `process`/`action`/`presentation` stage calls `waddles.core.service.overlay` → `push_widget`:
   ```json
   {
     "widget_id": "starwars_avatars.stage_strip",
     "output_kind": "sanitized_html",
     "html": "<div class=\"sw-avatar-stage\"><div class=\"sw-avatar sw-jedi\" style=\"left: 24%\"><img src=\"https://cdn.penguintech.io/avatars/jedi-idle.gif\" alt=\"avatar\" /><span class=\"sw-nameplate\">{user:0195f3a2-8c11-7000-8000-000000000001}</span></div></div>",
     "text": "{user:0195f3a2-8c11-7000-8000-000000000001} Force-jumped!"
   }
   ```
4. **Host Sanitization + Detokenization + Fanout**:
   - The Rust host capability in `svc-presentation` / `bundle_executor`:
     1. Resolves `{user:<uuid>}` via `OverlayDetokenizer` (HTML-escaping the resolved display name so a display name can never inject markup).
     2. Sanitizes `html` through the strict Rust `ammonia` policy (allowing only safe layout/sprite elements, `class`, bounded `style="left:..;top:..;transform:..;color:..;opacity:.."`, and `https://` `img[src]` matching the bundle's approved `overlay.media` `allowed_hosts`).
     3. Publishes the sanitized frame to `AppState::frame_hub` / `WidgetHub` for `(community_id, "starwars_avatars.stage_strip")`, which immediately updates the widget box inside `GET /{overlay_code}/scene` (and `GET /{overlay_code}/widget/starwars_avatars.stage_strip`).

### 2.2 Supported Widget Output Kinds (`WidgetOutputKind` Enum Codes)
Per `critical-rules.md` (Entity Identity & Typing: integer enum codes for type/kind), every registered widget has an explicit `output_kind_code SMALLINT`:

| Enum Code | Wire Name | Payload Fields Used | Security & Rendering Rules in `svc-presentation` |
|-----------|-----------|---------------------|--------------------------------------------------|
| `1` | `text` | `title?`, `body?`, `text` | Pure plain text. Rendered exclusively via `element.textContent`. `{user:<uuid>}` detokenized server-side. |
| `2` | `image` | `image_url`, `title?`, `caption_text?` | Validated `https://` image/GIF/WebP URL (host checked against bundle's `overlay.media` `allowed_hosts` or community media storage) + optional plain-text caption via `textContent`. |
| `3` | `sanitized_html` | `html` (`sanitized_html` on wire), `image_url?`, `text?` | Sanitized **server-side in Rust (`ammonia`)** at push time before caching or SSE fanout: strips all `<script>`, `<style>`, `<iframe>`, `<object>`, `<form>`, `on*` attributes, `javascript:`/`data:` URLs; allows safe structural/SVG/img tags, `data-widget-*` attributes, `class`, and a strict CSS property allowlist (`left`, `top`, `right`, `bottom`, `width`, `height`, `opacity`, `color`, `background-color`, `border-radius`, `transform`, `display`, `flex*`, `gap`, `align-items`, `justify-content`, `font-size`, `font-weight`). |
| `4` | `surface` | Structured JSON matching `surfaces.v1.json` (`music.now_playing`, `music.queue`, `alert`, `clip.alert`, `live.channels`) or built-in `core/overlay_schema::Surface` | Validated against `surfaces.v1.json` JSON Schema (`core/bundle_host_surface`) and rendered by the built-in structured surface templates. |

---

## 3. Rudimentary Starter Components & Surface Contract Extensions

Alongside custom presentation app bundles (`sanitized_html` / `image` / `text`), the Overlay Builder ships with the core starter components requested for music, song requests, and stream alerts, plus the example Star Wars–inspired avatar presentation bundle:

| Widget ID | Bundle Provider | Output Kind | Default Canvas Geometry (`1920×1080`) | Description & Rendered Fields |
|-----------|-----------------|-------------|---------------------------------------|-------------------------------|
| `dj.music.album_now_playing` | `waddles.core.example.dj` | `surface` (`music.now_playing`) | `x: 48, y: 860, w: 460, h: 160, z: 20` | **Music Album Cover & Track Details**: Square album art (`artwork_url`, `https://` validated, with vinyl/note fallback), track `title`, `artist`, elapsed/duration progress bar, and provider badge (`spotify`/`youtube`/`soundcloud`). |
| `dj.music.song_request_queue` | `waddles.core.example.dj` | `surface` (`music.queue`) | `x: 48, y: 560, w: 460, h: 280, z: 15` | **Song Request / Up-Next Queue**: Active song request queue (`up_next` tracks up to `max_items` prop, track title/artist/duration, `total_queued` counter, and configurable `!sr` header prompt). |
| `alerts.stream.event_box` | `waddles.core.overlay.alerts` | `surface` (`alert` / `alert_box`) | `x: 660, y: 120, w: 600, h: 220, z: 50` | **Stream Alerts (Sub, Follow, Cheer, Raid, Donation, Custom)**: Animated popup card triggered on `alert` / `alert_box` events. Supports filtering by `kinds` prop, custom duration, accent color, and detokenized display names. |
| `avatars.galactic.stage_strip` | `waddles.core.overlay.galactic_avatars` *(reference presentation app bundle)* | `sanitized_html` / `image` | `x: 0, y: 920, w: 1920, h: 160, z: 35` | **Galactic Stream Avatars (Presentation App Bundle)**: Renders sci-fi/Star-Wars-inspired pixel/sprite avatars along the bottom of the stream canvas for active chatters (`!avatar`, `!jump`, `!duel`), demonstrating dynamic `register_widget` + `push_widget(sanitized_html/image/text)`. |
| `core.static.media` | Built-in (`svc-presentation`) | `surface` (`media`) | `x: 640, y: 260, w: 640, h: 360, z: 30` | Standard static Media card placed as a region on the composite canvas. |
| `core.static.chat` | Built-in (`svc-presentation`) | `surface` (`chat`) | `x: 1480, y: 180, w: 400, h: 640, z: 10` | Standard static Chat box placed on the composite canvas. |
| `core.static.ticker` | Built-in (`svc-presentation`) | `surface` (`ticker`) | `x: 0, y: 1024, w: 1920, h: 56, z: 40` | Bottom news/announcement ticker bar. |

### `surfaces.v1.json` Additive Extension for Album Cover (`artwork_url`)
In `bundles/contracts/surfaces.v1.json` (and `core/svc_presentation/src/surfaces/surfaces.v1.json`), add optional `"artwork_url": { "$ref": "#/$defs/https_url" }` to `$defs.public_track.properties` so `dj.music.album_now_playing` renders the album cover image safely.

---

## 4. General Overlay Service Bundle (`waddles.core.service.overlay`)

Following the established service-bundle pattern from `waddles.core.service.reputation` (`bundles/rust/reputation/`) and `waddles.core.service.economy` (`bundles/rust/economy/`):

### 4.1 Manifest & Registration (`bundles/rust/overlay-service/hub-manifest.yaml` & `bundles/core-bundles.yaml`)
- **App ID**: `waddles.core.service.overlay`
- **Kind**: `service` (`trusted_source: true`, seeded via `hub_api/cli/seed_core_bundles.py`)
- **WIT World**: `stage-tools` (`waddle:bundle/tool-provider#call`)
- **Permissions**:
  - `overlay.layout.manage` (host capability to read/write community scene layout & widget registry)
  - `overlay.widget.push` (host capability to sanitize and fan out `sanitized_html`, `image`, and `text` widget updates)
  - `overlay.surface.music.now_playing`
  - `overlay.surface.music.queue`
  - `overlay.surface.alert`
  - `overlay.surface.clip.alert`
  - `overlay.surface.live.channels`

### 4.2 Exported MCP Tools (`exported_tools`)

| Tool Name | Caller / Required Scope | Idempotent Write? | Input Schema (`args` JSON) | Output Schema (JSON) |
|-----------|-------------------------|-------------------|----------------------------|----------------------|
| `register_widget` | Any app/presentation bundle with `tool.call:waddles.core.service.overlay:register_widget` | Yes (`idempotency_key` required) | `{ "widget_id": string, "display_name": string, "description": string?, "output_kind": "text" \| "image" \| "sanitized_html" \| "surface", "surface_id": string?, "allow_standalone_output": bool?, "default_x": i32, "default_y": i32, "default_width": i32, "default_height": i32, "default_z_index": i32?, "category": string?, "props_schema": object? }` | `{ "registered": true, "widget_id": string, "output_kind_code": i16, "standalone_path": string? }` |
| `push_widget` | Any app/presentation bundle with `tool.call:waddles.core.service.overlay:push_widget` (only for `widget_id`s owned by caller's `app_id`) | No (live push) | `{ "widget_id": string, "output_kind": "text" \| "image" \| "sanitized_html" \| "surface", "html": string?, "image_url": string?, "title": string?, "text": string?, "surface_id": string?, "payload": object?, "ttl_ms": u32? }` | `{ "status": "ok" \| "dropped", "widget_id": string }` |
| `add_component` | `required_scope: community:admin` | Yes (`idempotency_key` required) | `{ "widget_id": string, "x": i32 (0..1920), "y": i32 (0..1080), "width": i32 (32..1920), "height": i32 (32..1080), "z_index": i32 (-100..1000, default 10), "props": object? }` | `{ "instance_id": uuid_string, "widget_id": string, "output_kind": string, "x": i32, "y": i32, "width": i32, "height": i32, "z_index": i32, "visible": bool, "props": object }` |
| `update_component` | `required_scope: community:admin` | Yes (`idempotency_key` required) | `{ "instance_id": uuid_string, "x": i32?, "y": i32?, "width": i32?, "height": i32?, "z_index": i32?, "visible": bool?, "props": object? }` | `{ "instance_id": uuid_string, "updated": true, "component": { ... } }` |
| `remove_component` | `required_scope: community:admin` | Yes (`idempotency_key` required) | `{ "instance_id": uuid_string }` | `{ "instance_id": uuid_string, "removed": bool }` |
| `list_components` | Any caller with tool grant | No (read-only) | `{ "include_hidden": bool? }` | `{ "canvas": { "width": 1920, "height": 1080, "theme": string, "primary_color": string, "secondary_color": string, "font_family": string }, "components": [ ... ] }` |
| `list_registered_widgets` | Any caller with tool grant | No (read-only) | `{}` | `{ "widgets": [ { "widget_id": string, "bundle_app_id": string, "display_name": string, "output_kind": string, ... } ] }` |
| `clear_layout` | `required_scope: community:admin` | Yes (`idempotency_key` required) | `{}` | `{ "removed_count": u32 }` |
| `set_canvas_theme` | `required_scope: community:admin` | Yes (`idempotency_key` required) | `{ "theme": string?, "primary_color": hex_color?, "secondary_color": hex_color?, "font_family": string? }` | `{ "canvas": { ... } }` |

### 4.3 Ownership, Bounds & Security Invariants
- **Cross-Bundle Ownership Enforcement on `register_widget` & `push_widget`**:
  - The host passes the caller bundle's verified `caller_app_id` in the tool invocation context (`ToolContext`).
  - A presentation app bundle can only register or push to a `widget_id` bound to its own `bundle_app_id` (preventing bundle A from spoofing or overwriting bundle B's overlay widget).
- **Server-Side Sanitization of `sanitized_html` (`ammonia` in Rust)**:
  - Max `html` input length: `16_384` bytes (`16 KiB`).
  - Sanitized in Rust via `ammonia::Builder` BEFORE storing in the replay cache or sending over SSE:
    - Allowed tags: `div`, `span`, `p`, `strong`, `em`, `b`, `i`, `small`, `br`, `ul`, `ol`, `li`, `img`, `svg`, `g`, `path`, `circle`, `rect`, `line`, `polygon`, `polyline`, `text`.
    - Forbidden tags (stripped completely with contents): `script`, `style`, `iframe`, `frame`, `object`, `embed`, `applet`, `form`, `input`, `button`, `textarea`, `select`, `meta`, `link`, `base`, `audio`, `video`.
    - Allowed attributes: `class`, `title`, `alt`, `role`, `aria-label`, `data-state`, `data-faction`, `data-avatar-id`, `viewBox`, `width`, `height`, `d`, `fill`, `stroke`, `stroke-width`, `cx`, `cy`, `r`, `x`, `y`, `rx`, `ry`, and `src` on `img` (`https://` scheme only, validated against the bundle's approved `overlay.media` `allowed_hosts` or first-party CDN).
    - Inline `style` attribute is parsed and filtered to a strict property allowlist (`left`, `top`, `right`, `bottom`, `width`, `height`, `max-width`, `max-height`, `opacity`, `color`, `background-color`, `border-color`, `border-radius`, `transform`, `transition`, `display`, `flex`, `flex-direction`, `align-items`, `justify-content`, `gap`, `font-size`, `font-weight`, `text-align`), rejecting any value containing `url(`, `expression(`, `javascript:`, `@import`, or `-moz-binding`.
- **Coordinate Clamping & Validation**:
  - Canvas coordinate system is fixed to `1920×1080` (`CANVAS_WIDTH = 1920`, `CANVAS_HEIGHT = 1080`).
  - `x` in `0..=1888`, `y` in `0..=1048`, `width` in `32..=1920`, `height` in `32..=1080`, `z_index` in `-100..=1000`. Out-of-bounds values return `ToolError::InvalidArgs` (fail-loud per `critical-rules.md`).
  - Maximum `32` placed component instances per community scene.
- **Entity Identity & Typing (`critical-rules.md`)**:
  - Both `overlay_widget_registry` and `overlay_scene_components` use `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` internally and `public_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid()` across all API, tool, and SSE boundaries.
  - Output kinds and component statuses use `SMALLINT` enum codes internally (`output_kind_code SMALLINT`: `1 = text`, `2 = image`, `3 = sanitized_html`, `4 = surface`; `status_code SMALLINT`: `1 = active`, `2 = hidden`, `3 = bundle_disabled`).

---

## 5. Phased Implementation Tasks

### Task 1: Database Migration, Surface Contract (`artwork_url`), HTML Sanitizer, and `core/overlay_schema` Types

**Files:**
- Modify: `bundles/contracts/surfaces.v1.json`
- Modify: `core/svc_presentation/src/surfaces/surfaces.v1.json`
- Create: `config/postgres/migrations/081_overlay_builder_scene_components.sql` (and matching Alembic migration under `hub_api/migrations/versions/`)
- Modify: `core/overlay_schema/src/lib.rs`
- Modify: `core/overlay_schema/src/push.rs`
- Create: `core/overlay_schema/src/widget.rs`
- Create: `core/svc_presentation/src/overlay/sanitize.rs`
- Create: `core/svc_presentation/src/db/entities/overlay_widget_registry.rs`
- Create: `core/svc_presentation/src/db/entities/overlay_scene_component.rs`

- [ ] **Step 1: Extend `surfaces.v1.json` with optional `artwork_url` on `$defs.public_track`**
  Add `"artwork_url": { "$ref": "#/$defs/https_url" }` to `$defs.public_track.properties` in both `bundles/contracts/surfaces.v1.json` and `core/svc_presentation/src/surfaces/surfaces.v1.json`.
- [ ] **Step 2: Add `WidgetOutputKind`, `WidgetPushFrame`, `WidgetDescriptorDto`, and `SceneComponentDto` to `core/overlay_schema`**
  - Define `WidgetOutputKind` (`Text = 1`, `Image = 2`, `SanitizedHtml = 3`, `Surface = 4`), `CANVAS_WIDTH: i32 = 1920`, `CANVAS_HEIGHT: i32 = 1080`, `MAX_SCENE_COMPONENTS: usize = 32`, `ComponentStatusCode` (`Active = 1`, `Hidden = 2`, `BundleDisabled = 3`).
  - Add `sanitized_html: Option<String>` and `widget_id: Option<String>` to `OverlayPush` (with `#[serde(skip_serializing_if = "Option::is_none", default)]` so existing wire payloads remain byte-for-byte identical).
- [ ] **Step 3: Implement `core/svc_presentation/src/overlay/sanitize.rs` (`sanitize_widget_html`)**
  Implement the deterministic Rust HTML/SVG/style allowlist sanitizer + `https://` image host validator + `{user:<uuid>}` pre-detokenization hook, with exhaustive unit tests against XSS vectors (`<script>`, `<img src=x onerror=...>`, `<svg onload=...>`, `style="background:url(javascript:...)"`, `href="javascript:..."`, unclosed tags, oversized payloads >16 KiB).
- [ ] **Step 4: Create PostgreSQL + Alembic migration for `overlay_widget_registry` and `overlay_scene_components`**
  ```sql
  CREATE TABLE IF NOT EXISTS overlay_widget_registry (
      id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      public_id               UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
      tenant_id               BIGINT NOT NULL,
      community_id            BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
      bundle_app_id           VARCHAR(128) NOT NULL,
      widget_id               VARCHAR(128) NOT NULL,
      display_name            VARCHAR(120) NOT NULL,
      description             VARCHAR(280) NOT NULL DEFAULT '',
      category                VARCHAR(64)  NOT NULL DEFAULT 'custom',
      output_kind_code        SMALLINT NOT NULL DEFAULT 1 CHECK (output_kind_code IN (1, 2, 3, 4)),
      surface_id              VARCHAR(64),
      allow_standalone_output BOOLEAN NOT NULL DEFAULT TRUE,
      default_x_px            INTEGER NOT NULL DEFAULT 48 CHECK (default_x_px >= 0 AND default_x_px <= 1920),
      default_y_px            INTEGER NOT NULL DEFAULT 48 CHECK (default_y_px >= 0 AND default_y_px <= 1080),
      default_width_px        INTEGER NOT NULL DEFAULT 400 CHECK (default_width_px >= 32 AND default_width_px <= 1920),
      default_height_px       INTEGER NOT NULL DEFAULT 200 CHECK (default_height_px >= 32 AND default_height_px <= 1080),
      default_z_index         INTEGER NOT NULL DEFAULT 20 CHECK (default_z_index >= -100 AND default_z_index <= 1000),
      props_schema            JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      UNIQUE (tenant_id, community_id, widget_id)
  );

  CREATE TABLE IF NOT EXISTS overlay_scene_components (
      id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      public_id       UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
      tenant_id       BIGINT NOT NULL,
      community_id    BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
      bundle_app_id   VARCHAR(128) NOT NULL,
      widget_id       VARCHAR(128) NOT NULL,
      surface_id      VARCHAR(64)  NOT NULL DEFAULT 'custom',
      output_kind_code SMALLINT NOT NULL DEFAULT 1 CHECK (output_kind_code IN (1, 2, 3, 4)),
      display_name    VARCHAR(120) NOT NULL,
      x_px            INTEGER NOT NULL DEFAULT 48 CHECK (x_px >= 0 AND x_px <= 1920),
      y_px            INTEGER NOT NULL DEFAULT 48 CHECK (y_px >= 0 AND y_px <= 1080),
      width_px        INTEGER NOT NULL DEFAULT 400 CHECK (width_px >= 32 AND width_px <= 1920),
      height_px       INTEGER NOT NULL DEFAULT 200 CHECK (height_px >= 32 AND height_px <= 1080),
      z_index         INTEGER NOT NULL DEFAULT 10 CHECK (z_index >= -100 AND z_index <= 1000),
      status_code     SMALLINT NOT NULL DEFAULT 1 CHECK (status_code IN (1, 2, 3)),
      props           JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
  );
  CREATE INDEX IF NOT EXISTS idx_overlay_scene_components_tenant_community
      ON overlay_scene_components (tenant_id, community_id, z_index, id);
  ```
- [ ] **Step 5: Run schema, sanitizer & surface contract unit tests**
  Run: `cargo test -p overlay_schema -p bundle_host_surface -p svc_presentation`
  Expected: PASS with ≥90% coverage.

---

### Task 2: General Overlay Service Bundle (`waddles.core.service.overlay`) & Host Wiring

**Files:**
- Create: `bundles/rust/overlay-service/Cargo.toml`
- Create: `bundles/rust/overlay-service/bundle.yaml`
- Create: `bundles/rust/overlay-service/hub-manifest.yaml`
- Create: `bundles/rust/overlay-service/src/lib.rs`
- Create: `bundles/rust/overlay-service/src/logic.rs`
- Modify: `core/bundle_executor/src/host/stage_next_imports.rs`
- Modify: `bundles/core-bundles.yaml`

- [ ] **Step 1: Wire `overlay::register-widget` and `overlay::push` in `core/bundle_executor`**
  Replace the `overlay_not_implemented()` stubs in `core/bundle_executor/src/host/stage_next_imports.rs` (lines 175–190) with real `OverlayHost` trait dispatch so `register-widget`, `push`, and `publish-surface` route to `svc-presentation` / the overlay store with `(tenant_id, community_id, caller_app_id)` derived strictly from `InvocationScope`.
- [ ] **Step 2: Implement `bundles/rust/overlay-service/src/logic.rs` with 100% host-testable tool handlers**
  Implement the `ToolRegistry` handlers for:
  - `register_widget` (registers a presentation bundle's custom widget descriptor in `overlay_widget_registry`, enforcing that `widget_id` belongs to the calling bundle)
  - `push_widget` (pushes live `sanitized_html`, `image`, `text`, or `surface` updates for `widget_id`, enforcing ownership, size bounds, and HTML/URL sanitization)
  - `add_component` (`required_scope: community:admin`)
  - `update_component` (`required_scope: community:admin`)
  - `remove_component` (`required_scope: community:admin`)
  - `list_components`
  - `list_registered_widgets`
  - `clear_layout` (`required_scope: community:admin`)
  - `set_canvas_theme` (`required_scope: community:admin`)
- [ ] **Step 3: Create `hub-manifest.yaml`, `bundle.yaml`, and register in `bundles/core-bundles.yaml`**
  Declare `kind: service`, `app_id: waddles.core.service.overlay`, `wit_world: stage-tools`, and all 9 `exported_tools`.
- [ ] **Step 4: Run unit tests for `waddle-bundle-overlay-service`**
  Run: `cargo test --manifest-path bundles/rust/overlay-service/Cargo.toml`
  Expected: PASS (≥90% coverage across registration, `sanitized_html`/`image`/`text` pushes, cross-bundle ownership rejection, coordinate bounds, and admin scope gates).

---

### Task 3: Presentation-Layer App Bundles (`dj`, `stream-alerts`, and `galactic-avatars`)

**Files:**
- Modify: `bundles/rust/dj/hub-manifest.yaml`
- Modify: `bundles/rust/dj/bundle.yaml`
- Modify: `bundles/rust/dj/src/logic.rs`
- Create: `bundles/rust/stream-alerts/Cargo.toml`
- Create: `bundles/rust/stream-alerts/bundle.yaml`
- Create: `bundles/rust/stream-alerts/hub-manifest.yaml`
- Create: `bundles/rust/stream-alerts/src/lib.rs`
- Create: `bundles/rust/stream-alerts/src/logic.rs`
- Create: `bundles/rust/galactic-avatars/Cargo.toml`
- Create: `bundles/rust/galactic-avatars/bundle.yaml`
- Create: `bundles/rust/galactic-avatars/hub-manifest.yaml`
- Create: `bundles/rust/galactic-avatars/src/lib.rs`
- Create: `bundles/rust/galactic-avatars/src/logic.rs`
- Modify: `bundles/core-bundles.yaml`

- [ ] **Step 1: Update `waddles.core.example.dj` with `overlay_widgets` descriptors & `artwork_url` publishing**
  Declare `dj.music.album_now_playing` (`output_kind: surface`, `surface_id: music.now_playing`) and `dj.music.song_request_queue` (`output_kind: surface`, `surface_id: music.queue`), request `waddles.core.service.overlay` tools (`register_widget`, `push_widget`), and include optional `artwork_url` on published tracks.
- [ ] **Step 2: Create presentation-layer app bundle `waddles.core.overlay.alerts` (`bundles/rust/stream-alerts/`)**
  Build a Rust app bundle on `world stage-next-tools` that registers `alerts.stream.event_box` with `waddles.core.service.overlay` (`register_widget`) and publishes follow/sub/raid/cheer/donation alerts via `publish-surface("alert", ...)` and `push_widget`.
- [ ] **Step 3: Create presentation-layer app bundle `waddles.core.overlay.galactic_avatars` (`bundles/rust/galactic-avatars/`)**
  Build a reference presentation app bundle (Star Wars–inspired Stream Avatars stage strip) on `world stage-next-tools` that:
  - Declares `stages.presentation` and `stages.action` (`!avatar`, `!jump`, `!duel`, `!force`).
  - Calls `waddles.core.service.overlay` → `register_widget` to register `avatars.galactic.stage_strip` (`output_kind: "sanitized_html"`, `allow_standalone_output: true`, `default_x: 0, default_y: 920, default_width: 1920, default_height: 160`).
  - Calls `waddles.core.service.overlay` → `push_widget` to emit sanitized HTML/image/text avatar stage updates with tokenized `{user:<uuid>}` nameplates and faction sprites.
- [ ] **Step 4: Test all three presentation/app bundles**
  Run: `cargo test --manifest-path bundles/rust/dj/Cargo.toml && cargo test --manifest-path bundles/rust/stream-alerts/Cargo.toml && cargo test --manifest-path bundles/rust/galactic-avatars/Cargo.toml`
  Expected: PASS.

---

### Task 4: `svc-presentation` Composite Scene Renderer (`/{overlay_code}/scene`) & Standalone Custom Widget Output (`/{overlay_code}/widget/{widget_id}`)

**Files:**
- Create: `core/svc_presentation/src/http/scene_page.rs`
- Create: `core/svc_presentation/src/http/widget_page.rs`
- Create: `core/svc_presentation/src/http/templates/scene.html`
- Create: `core/svc_presentation/src/http/templates/widget.html`
- Modify: `core/svc_presentation/src/http/mod.rs`
- Modify: `core/svc_presentation/src/metrics.rs`

- [ ] **Step 1: Add composite scene and standalone custom widget routes to `core/svc_presentation`**
  - `GET /{overlay_code}/scene?key=<VIEW_KEY>`: Validates `overlay_code` and `?key=` (`ViewAuth`), emits OTel counter + histogram, and serves `scene.html` (`1920×1080` composite stage).
  - `GET /{overlay_code}/scene/layout?key=<VIEW_KEY>`: Returns active `SceneLayoutDto` (`canvas` theme + placed `components` ordered by `z_index, id` whose providing `bundle_app_id` is enabled in the community).
  - `GET /{overlay_code}/widget/{widget_id}?key=<VIEW_KEY>` & `GET /{overlay_code}/widget/{widget_id}/live?key=<VIEW_KEY>`: Serves any presentation-bundle-registered custom overlay output (`sanitized_html`, `image`, or `text`) as its own standalone OBS browser source URL in addition to working inside the composite `/scene`.
  - `POST /{overlay_code}/widget/{widget_id}/push`: Authenticated via `PushAuth` (`presentation.overlay:push:<community_id>`), runs `OverlayDetokenizer` (`{user:<uuid>}`) + `sanitize_widget_html` (for `sanitized_html`) + `https://` URL validation (for `image`), caches the last frame for replay, and fans out over SSE.
- [ ] **Step 2: Build `scene.html` and `widget.html` with safe rendering for all 4 `WidgetOutputKind`s**
  - `output_kind == "text"`: Renders `title`/`body`/`text` via `textContent`.
  - `output_kind == "image"`: Renders `https://`-verified `<img>` + optional `textContent` caption.
  - `output_kind == "sanitized_html"`: Parses the server-sanitized HTML fragment via `DOMParser` and mounts only allowlisted element/text nodes through a client-side defense-in-depth tree walker (never raw `innerHTML` on unverified strings, rejecting any `<script>`, `on*`, or non-`https:` `src`).
  - `output_kind == "surface"`: Renders `music.now_playing` (album cover + title + artist + progress), `music.queue` (song request queue), `alert` (sub/follow/raid/cheer alert popup), and core static surfaces.
- [ ] **Step 3: Add Rust integration tests for `/scene`, `/scene/layout`, and `/widget/{widget_id}`**
  Verify CSP header hash, `?key=` and `PushAuth` enforcement, server-side HTML sanitization on `/widget/{widget_id}/push`, `{user:<uuid>}` detokenization, `public_id` UUID externalization, and OTel metric increments.

---

### Task 5: `hub-api` Overlay Builder, Manifest `overlay_widgets` Parsing & Community-Filtered Discovery

**Files:**
- Modify: `hub_api/services/bundle_manifest_v2.py`
- Modify: `hub_api/services/overlay_service.py`
- Modify: `hub_api/blueprints/v1/overlay.py`
- Create: `tests/unit/test_overlay_builder_api.py`

- [ ] **Step 1: Parse optional `overlay_widgets:` declarations in `hub_api/services/bundle_manifest_v2.py`**
  Validate optional `overlay_widgets:` entries in `bundle.yaml` v2 (`widget_id`, `display_name`, `output_kind` in `{"text", "image", "sanitized_html", "surface"}`, `surface_id`, `allow_standalone_output`, `default_layout`, `props_schema`) and sync them into `overlay_widget_registry` when a bundle is installed/enabled for a community.
- [ ] **Step 2: Implement community-active widget discovery & scene layout methods in `OverlayService`**
  In `hub_api/services/overlay_service.py`:
  - `list_available_widgets(tenant_id: int, community_id: int)`: Queries `overlay_widget_registry` joined with active `app_installations` for `(tenant_id, community_id)` so any presentation app bundle registered with the overlay service bundle and enabled for the active community automatically appears in the palette (alongside built-in static surfaces), complete with its `output_kind` (`sanitized_html`, `image`, `text`, `surface`) and standalone overlay URL (`/{overlay_code}/widget/{widget_id}?key=<VIEW_KEY>`).
  - `get_builder_state`, `add_scene_component`, `update_scene_component`, `remove_scene_component`, `save_scene_layout`, and `trigger_test_overlay_event` (supports testing `now_playing`, `song_queue`, `alert`, and custom `widget_id` pushes).
- [ ] **Step 3: Expose validated REST routes in `hub_api/blueprints/v1/overlay.py`**
  Protected by `@require_community_admin` and explicit response DTOs (`jsonify_dto` / `@validate_response`):
  - `GET /api/v1/admin/<int:community_id>/overlay/builder`
  - `POST /api/v1/admin/<int:community_id>/overlay/builder/components`
  - `PATCH /api/v1/admin/<int:community_id>/overlay/builder/components/<uuid:instance_id>`
  - `DELETE /api/v1/admin/<int:community_id>/overlay/builder/components/<uuid:instance_id>`
  - `PUT /api/v1/admin/<int:community_id>/overlay/builder/layout`
  - `POST /api/v1/admin/<int:community_id>/overlay/builder/test-event`
- [ ] **Step 4: Write unit & API filter tests (`tests/unit/test_overlay_builder_api.py`)**
  Test community isolation, verify enabling/disabling a presentation app bundle (like `waddles.core.overlay.galactic_avatars`) dynamically adds/hides its widget in the community's builder palette, and assert exact response DTO field sets.

---

### Task 6: Web UI Rudimentary Overlay Builder (`AdminStreamOverlays.jsx` + `OverlayBuilderTab.jsx`)

**Files:**
- Create: `admin/hub_module/frontend/src/pages/admin/OverlayBuilderTab.jsx`
- Modify: `admin/hub_module/frontend/src/pages/admin/AdminStreamOverlays.jsx`
- Create: `admin/hub_module/frontend/src/pages/admin/__tests__/OverlayBuilderTab.test.jsx`

- [ ] **Step 1: Build `OverlayBuilderTab.jsx` with a 16:9 (`1920×1080`) interactive designer canvas**
  - **Top Toolbar**:
    - Single **Composite Scene OBS URL** (`https://.../overlays/{overlay_code}/scene?key=...`) with **Copy URL**, **Save Layout**, **Test Alert / Test Widget Push**, and **Snap-to-Grid (16px)** toggle.
  - **Left Sidebar — Community Component Palette (Auto-Discovered from Enabled Bundles)**:
    - Automatically lists all widgets registered with `waddles.core.service.overlay` by bundles enabled for `activeCommunityId`, grouped into:
      - **Music & Audio** (`Now Playing Album & Details`, `Song Request Queue`)
      - **Stream Alerts** (`Sub / Follow / Raid / Cheer Alert Box`)
      - **Presentation App Bundles** (e.g., `Galactic Stream Avatars`, or any custom `sanitized_html` / `image` / `text` widget registered by an enabled app bundle, showing an output-type badge `HTML` / `IMAGE` / `TEXT` and a button to either **+ Add to Canvas** or **Copy Standalone URL**)
      - **Core Static Surfaces** (`Media`, `Chat`, `Ticker`, `Goals`)
  - **Center Stage — `1920×1080` Scaled Interactive Canvas**:
    - 16:9 stage with drag-to-move (`x`, `y`) and handle drag-to-resize (`width`, `height`), live preview of album cover, song queue, alert box, and custom presentation bundle widgets (`sanitized_html` / `image` / `text`).
  - **Right Sidebar — Selected Component Inspector**:
    - `X`, `Y`, `Width`, `Height`, `Layer (Z-Index)` inputs, dynamic `props_schema` controls, standalone widget URL copy button (when `allow_standalone_output` is true), visibility toggle, and delete button.
- [ ] **Step 2: Wire `"Overlay Builder"` into `AdminStreamOverlays.jsx` alongside `"All-in-One Overlay"` and `"Individual Sources"`**
  Also list any standalone presentation-bundle overlay outputs (`/{overlay_code}/widget/{widget_id}`) in the **Individual Sources** tab so streamers can grab standalone URLs for custom presentation app bundles there too.
- [ ] **Step 3: Run frontend unit tests, Playwright E2E tests, and capture marketing screenshots**
  - Run Vitest unit tests on `OverlayBuilderTab.test.jsx` (≥90% coverage).
  - Run Playwright E2E verifying that a custom presentation app bundle (`galactic_avatars`) registers with the overlay service bundle, appears in the active community's palette, can be placed/positioned on the `1920×1080` canvas, and renders live sanitized HTML/image/text updates.
  - Capture updated marketing screenshots in `docs/screenshots/`.

---

## 6. Verification, Observability & Security Checklist

- [ ] **Server-Side Sanitization of Presentation Bundle Outputs (`security.md`)**:
  - `sanitized_html` widget outputs are sanitized in Rust (`ammonia`) before storage/fanout AND mounted via safe DOM construction on the client—zero raw `innerHTML` of unsanitized strings, zero `<script>`/`on*`/`javascript:` execution paths.
  - `image` URLs are validated as `https://` against approved hosts (`overlay.media`).
  - `text` outputs are rendered strictly via `textContent`.
- [ ] **OTel Telemetry (`critical-rules.md`)**:
  - `svc-presentation` emits counters and latency histograms for `/scene`, `/widget/{widget_id}`, and `/widget/{widget_id}/push` alongside structured penguin logging.
- [ ] **PII Tokenization & Entity Typing (`critical-rules.md`)**:
  - Zero raw PII in `overlay_widget_registry`, `overlay_scene_components`, or tool arguments; `{user:<uuid>}` placeholders in custom presentation widgets are detokenized server-side via `OverlayDetokenizer`.
  - All external IDs use `public_id` (`UUID`), never sequential BIGINT PKs; `output_kind_code` and `status_code` use `SMALLINT` enum codes.
- [ ] **Output Validation (`security.md`)**:
  - Every `hub-api` endpoint uses explicit response DTO serialization (`jsonify_dto` / `@validate_response`) with regression tests asserting exact field sets.
