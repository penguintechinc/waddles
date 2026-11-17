import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowPathIcon,
  BellAlertIcon,
  CheckCircleIcon,
  ClipboardDocumentIcon,
  EyeIcon,
  EyeSlashIcon,
  MusicalNoteIcon,
  PlusIcon,
  Squares2X2Icon,
  TrashIcon,
  TvIcon,
  UserGroupIcon,
  ExclamationTriangleIcon,
  SparklesIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';

/** Canvas presets; the server derives the same sizes from the stored resolution code. */
const RESOLUTIONS = {
  '1080p': { label: '1080p (Full HD)', width: 1920, height: 1080 },
  '2k': { label: '2K (QHD)', width: 2560, height: 1440 },
  '4k': { label: '4K (Ultra HD)', width: 3840, height: 2160 },
};

const MIN_SIZE = 32;
const ALIGN_MARGIN = 40;

/** Icon for a widget category in the palette. */
function CategoryIcon({ category }) {
  switch (category) {
    case 'music':
      return <MusicalNoteIcon className="w-5 h-5 text-emerald-400" />;
    case 'alerts':
      return <BellAlertIcon className="w-5 h-5 text-gold-400" />;
    case 'avatars':
      return <UserGroupIcon className="w-5 h-5 text-cyan-400" />;
    default:
      return <Squares2X2Icon className="w-5 h-5 text-sky-400" />;
  }
}

/** Human-readable message from an API failure (the server's own text for 4xx, else a fallback). */
function apiMessage(err, fallback) {
  const status = err?.response?.status;
  if (status === 402) return 'The overlay builder is not enabled for this plan.';
  const body = err?.response?.data;
  const text = body?.message || body?.error;
  if (status && status < 500 && typeof text === 'string' && text) return text;
  return fallback;
}

/** Clamp a number into [lo, hi]; non-numeric input falls back to `lo`. */
function clamp(value, lo, hi) {
  const n = Number.parseInt(value, 10);
  if (Number.isNaN(n)) return lo;
  return Math.max(lo, Math.min(hi, n));
}

/** One numeric inspector field that commits on blur/Enter, never per keystroke. */
function NumberField({ label, value, min, max, onCommit }) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);

  const commit = () => {
    const next = clamp(draft, min, max);
    setDraft(String(next));
    if (next !== value) onCommit(next);
  };

  return (
    <div>
      <label className="block text-navy-400 mb-1">
        {label}
        <input
          type="number"
          value={draft}
          min={min}
          max={max}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()}
          className="mt-1 w-full bg-navy-900 border border-navy-700 rounded px-2.5 py-1.5 text-sky-100 font-mono"
        />
      </label>
    </div>
  );
}

/**
 * Overlay Builder: drag-and-drop composite scene designer for a community.
 *
 * Talks only to hub-api (palette, layout, canvas); the page it configures is served by
 * svc-presentation at the scene URL listed with the community's browser sources.
 */
export default function OverlayBuilderTab({ communityId }) {
  const [canvas, setCanvas] = useState({ resolution: '1080p', width: 1920, height: 1080 });
  const [components, setComponents] = useState([]);
  const [widgets, setWidgets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [successMsg, setSuccessMsg] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState('all');
  const [showClearConfirm, setShowClearConfirm] = useState(false);
  const [sceneUrl, setSceneUrl] = useState('');
  const [copied, setCopied] = useState(false);

  const canvasRef = useRef(null);
  const timers = useRef([]);

  const activeRes = RESOLUTIONS[canvas.resolution] || RESOLUTIONS['1080p'];

  useEffect(() => {
    const pending = timers.current;
    return () => pending.forEach(clearTimeout);
  }, []);

  const showFeedback = useCallback((msg) => {
    setSuccessMsg(msg);
    timers.current.push(setTimeout(() => setSuccessMsg(null), 3000));
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [layoutRes, widgetsRes] = await Promise.all([
        adminApi.getOverlayLayout(communityId),
        adminApi.getOverlayWidgets(communityId),
      ]);
      const layout = layoutRes?.data?.layout;
      if (layout) {
        setCanvas(layout.canvas);
        setComponents(layout.components || []);
      }
      setWidgets(widgetsRes?.data?.widgets || []);
    } catch (err) {
      setError(apiMessage(err, 'Failed to load the overlay scene layout or available widgets.'));
      setLoading(false);
      return;
    }
    // The scene URL is optional: a failure here never blocks the designer.
    try {
      const sourcesRes = await adminApi.getBrowserSources(communityId);
      const scene = (sourcesRes?.data?.sources || []).find((s) => s.sourceType === 'scene');
      setSceneUrl(scene?.url || '');
    } catch {
      setSceneUrl('');
    }
    setLoading(false);
  }, [communityId]);

  useEffect(() => {
    load();
  }, [load]);

  const selected = useMemo(
    () => components.find((c) => c.instance_id === selectedId) || null,
    [components, selectedId],
  );

  const categories = useMemo(
    () => ['all', ...new Set(widgets.map((w) => w.category).filter(Boolean))],
    [widgets],
  );

  const filteredWidgets = useMemo(() => {
    const q = search.trim().toLowerCase();
    return widgets.filter(
      (w) =>
        (category === 'all' || w.category === category) &&
        (!q ||
          w.display_name.toLowerCase().includes(q) ||
          (w.description || '').toLowerCase().includes(q) ||
          w.widget_id.toLowerCase().includes(q)),
    );
  }, [widgets, category, search]);

  const titleFor = (comp) =>
    widgets.find((w) => w.bundle_app_id === comp.bundle_app_id && w.widget_id === comp.widget_id)
      ?.display_name || comp.display_name;

  async function addWidget(widget) {
    setError(null);
    try {
      // Server falls back to the widget's own defaults for anything omitted.
      const res = await adminApi.addOverlayComponent(communityId, {
        bundle_app_id: widget.bundle_app_id,
        widget_id: widget.widget_id,
      });
      const comp = res?.data?.component;
      if (comp) {
        setComponents((prev) => [...prev, comp]);
        setSelectedId(comp.instance_id);
        showFeedback(`Added "${widget.display_name}" to the canvas`);
      }
    } catch (err) {
      setError(apiMessage(err, 'Could not add the component to the overlay.'));
    }
  }

  async function updateComponent(instanceId, patch) {
    setError(null);
    try {
      const res = await adminApi.updateOverlayComponent(communityId, instanceId, patch);
      const updated = res?.data?.component;
      if (updated) {
        setComponents((prev) => prev.map((c) => (c.instance_id === instanceId ? updated : c)));
      }
    } catch (err) {
      setError(apiMessage(err, 'Failed to update the component.'));
      await load();
    }
  }

  async function removeComponent(instanceId) {
    setError(null);
    try {
      await adminApi.removeOverlayComponent(communityId, instanceId);
      setComponents((prev) => prev.filter((c) => c.instance_id !== instanceId));
      if (selectedId === instanceId) setSelectedId(null);
      showFeedback('Component removed from the canvas');
    } catch (err) {
      setError(apiMessage(err, 'Failed to remove the component.'));
    }
  }

  async function clearCanvas() {
    setError(null);
    try {
      await adminApi.clearOverlayLayout(communityId);
      setComponents([]);
      setSelectedId(null);
      showFeedback('Canvas layout cleared');
    } catch (err) {
      setError(apiMessage(err, 'Failed to clear the canvas.'));
    } finally {
      setShowClearConfirm(false);
    }
  }

  async function changeResolution(resolution) {
    if (resolution === canvas.resolution) return;
    setError(null);
    try {
      const res = await adminApi.setOverlayCanvas(communityId, resolution);
      if (res?.data?.canvas) setCanvas(res.data.canvas);
      showFeedback('Canvas resolution saved');
    } catch (err) {
      setError(apiMessage(err, 'Failed to change the canvas resolution.'));
    }
  }

  function align(preset) {
    if (!selected) return;
    const { width: w, height: h } = selected;
    const right = activeRes.width - w - ALIGN_MARGIN;
    const bottom = activeRes.height - h - ALIGN_MARGIN;
    const spots = {
      center: [Math.round((activeRes.width - w) / 2), Math.round((activeRes.height - h) / 2)],
      'top-left': [ALIGN_MARGIN, ALIGN_MARGIN],
      'top-right': [right, ALIGN_MARGIN],
      'bottom-left': [ALIGN_MARGIN, bottom],
      'bottom-right': [right, bottom],
      'bottom-banner': [Math.round((activeRes.width - w) / 2), bottom],
    };
    const [x, y] = spots[preset] || [selected.x, selected.y];
    updateComponent(selected.instance_id, {
      x: clamp(x, 0, activeRes.width - MIN_SIZE),
      y: clamp(y, 0, activeRes.height - MIN_SIZE),
    });
  }

  async function copySceneUrl() {
    try {
      await navigator.clipboard.writeText(sceneUrl);
      setCopied(true);
      timers.current.push(setTimeout(() => setCopied(false), 2000));
    } catch {
      setError('Could not copy to the clipboard; copy the URL from the Preview link instead.');
    }
  }

  /** Keyboard alternative to dragging: Enter/Space selects, arrows nudge (Shift = 50px). */
  function keyNudge(e, comp) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      setSelectedId(comp.instance_id);
      return;
    }
    const step = e.shiftKey ? 50 : 10;
    const delta = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[
      e.key
    ];
    if (!delta) return;
    e.preventDefault();
    setSelectedId(comp.instance_id);
    updateComponent(comp.instance_id, {
      x: clamp(comp.x + delta[0], 0, activeRes.width - comp.width),
      y: clamp(comp.y + delta[1], 0, activeRes.height - comp.height),
    });
  }

  /** Pointer drag: preview locally, persist once on release. */
  function startDrag(e, comp) {
    e.stopPropagation();
    setSelectedId(comp.instance_id);
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;
    const scaleX = activeRes.width / rect.width;
    const scaleY = activeRes.height / rect.height;
    const start = { mx: e.clientX, my: e.clientY, x: comp.x, y: comp.y };
    let last = { x: comp.x, y: comp.y };

    const position = (ev) => ({
      x: clamp(start.x + (ev.clientX - start.mx) * scaleX, 0, activeRes.width - comp.width),
      y: clamp(start.y + (ev.clientY - start.my) * scaleY, 0, activeRes.height - comp.height),
    });
    const move = (ev) => {
      last = position(ev);
      setComponents((prev) =>
        prev.map((c) => (c.instance_id === comp.instance_id ? { ...c, ...last } : c)),
      );
    };
    const up = () => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
      if (last.x !== comp.x || last.y !== comp.y) updateComponent(comp.instance_id, last);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-72" role="status" aria-label="Loading">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {error && (
        <div
          role="alert"
          className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 flex items-center justify-between"
        >
          <div className="flex items-center space-x-3 text-red-400">
            <ExclamationTriangleIcon className="w-5 h-5 flex-shrink-0" />
            <span>{error}</span>
          </div>
          <button
            type="button"
            aria-label="Dismiss error"
            onClick={() => setError(null)}
            className="text-red-400 hover:text-red-300"
          >
            &times;
          </button>
        </div>
      )}

      {successMsg && (
        <div
          role="status"
          className="bg-emerald-500/10 border border-emerald-500/30 rounded-lg p-3 flex items-center space-x-3 text-emerald-400"
        >
          <CheckCircleIcon className="w-5 h-5 flex-shrink-0" />
          <span>{successMsg}</span>
        </div>
      )}

      <div className="bg-navy-800 border border-navy-700 rounded-lg p-4 flex flex-wrap items-center justify-between gap-4">
        <div>
          <span className="block text-xs uppercase tracking-wider text-navy-400 mb-1">Resolution</span>
          <div className="flex rounded-lg bg-navy-900 p-1 border border-navy-700">
            {Object.keys(RESOLUTIONS).map((key) => (
              <button
                key={key}
                type="button"
                aria-pressed={canvas.resolution === key}
                onClick={() => changeResolution(key)}
                className={`px-3 py-1 text-xs font-semibold rounded-md transition-colors ${
                  canvas.resolution === key
                    ? 'bg-gold-500 text-navy-900 shadow'
                    : 'text-navy-300 hover:text-sky-200'
                }`}
              >
                {key.toUpperCase()}
              </button>
            ))}
          </div>
        </div>

        <div className="flex items-center space-x-3">
          {sceneUrl && (
            <>
              <button
                type="button"
                onClick={copySceneUrl}
                className="flex items-center space-x-2 px-3 py-1.5 bg-navy-700 hover:bg-navy-600 text-sky-100 text-xs font-medium rounded-lg border border-navy-600"
              >
                <ClipboardDocumentIcon className="w-4 h-4 text-gold-400" />
                <span>{copied ? 'Copied URL!' : 'Copy Scene URL'}</span>
              </button>
              <a
                href={sceneUrl}
                target="_blank"
                rel="noreferrer noopener"
                className="flex items-center space-x-1.5 px-3 py-1.5 bg-navy-700 hover:bg-navy-600 text-sky-200 text-xs font-medium rounded-lg border border-navy-600"
              >
                <TvIcon className="w-4 h-4 text-sky-400" />
                <span>Preview</span>
              </a>
            </>
          )}
          <button
            type="button"
            onClick={() => setShowClearConfirm(true)}
            disabled={components.length === 0}
            className="flex items-center space-x-1 px-3 py-1.5 bg-red-500/10 hover:bg-red-500/20 text-red-400 text-xs font-medium rounded-lg border border-red-500/30 disabled:opacity-40"
          >
            <TrashIcon className="w-4 h-4" />
            <span>Clear</span>
          </button>
          <button
            type="button"
            onClick={load}
            aria-label="Refresh layout"
            className="p-1.5 text-navy-400 hover:text-sky-300 rounded"
          >
            <ArrowPathIcon className="w-4 h-4" />
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        <div className="lg:col-span-1 bg-navy-800 border border-navy-700 rounded-lg p-4 flex flex-col h-[750px]">
          <h2 className="text-base font-semibold text-sky-100 flex items-center space-x-2">
            <SparklesIcon className="w-5 h-5 text-gold-400" />
            <span>Component Palette</span>
          </h2>
          <p className="text-xs text-navy-400 mt-1 mb-4">
            Widgets from the bundles enabled for this community. Click to add to the canvas.
          </p>
          <div className="space-y-2 mb-3">
            <input
              type="text"
              aria-label="Search components"
              placeholder="Search components..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full bg-navy-900 border border-navy-700 rounded-lg px-3 py-1.5 text-xs text-sky-100 placeholder-navy-500"
            />
            <div className="flex flex-wrap gap-1">
              {categories.map((cat) => (
                <button
                  key={cat}
                  type="button"
                  aria-pressed={category === cat}
                  onClick={() => setCategory(cat)}
                  className={`px-2 py-0.5 text-[11px] rounded capitalize ${
                    category === cat
                      ? 'bg-gold-500/20 text-gold-400 border border-gold-500/30'
                      : 'text-navy-400 hover:text-sky-300'
                  }`}
                >
                  {cat}
                </button>
              ))}
            </div>
          </div>
          <div className="flex-1 overflow-y-auto space-y-2.5 pr-1">
            {filteredWidgets.length === 0 ? (
              <div className="text-center py-8 text-xs text-navy-400">
                No matching overlay widgets for this community. Enable an overlay bundle to see its widgets.
              </div>
            ) : (
              filteredWidgets.map((w) => (
                <div
                  key={`${w.bundle_app_id}/${w.widget_id}`}
                  className="bg-navy-900 border border-navy-700/80 hover:border-gold-400/50 rounded-lg p-3"
                >
                  <div className="flex items-center space-x-2">
                    <CategoryIcon category={w.category} />
                    <h3 className="text-xs font-semibold text-sky-100 truncate">{w.display_name}</h3>
                  </div>
                  <p className="text-[11px] text-navy-400 mt-1 line-clamp-2">{w.description}</p>
                  <div className="flex items-center space-x-2 mt-2 text-[10px] text-navy-500">
                    <span>
                      {w.default_width}×{w.default_height}px
                    </span>
                    <span>•</span>
                    <span className="truncate">{w.bundle_app_id}</span>
                  </div>
                  <button
                    type="button"
                    onClick={() => addWidget(w)}
                    className="mt-3 flex items-center justify-center space-x-1.5 w-full py-1.5 bg-navy-800 hover:bg-gold-500 hover:text-navy-900 text-gold-400 text-xs font-medium rounded border border-gold-500/30"
                  >
                    <PlusIcon className="w-3.5 h-3.5" />
                    <span>Add to Canvas</span>
                  </button>
                </div>
              ))
            )}
          </div>
        </div>

        <div className="lg:col-span-3 space-y-4 flex flex-col">
          <div className="bg-navy-850 border border-navy-700 rounded-lg p-4">
            <div className="flex items-center justify-between text-xs text-navy-400 mb-2">
              <span className="font-mono">
                Canvas: {activeRes.width} × {activeRes.height} ({activeRes.label})
              </span>
              <span>
                {components.length} component{components.length === 1 ? '' : 's'} placed
              </span>
            </div>
            <div
              ref={canvasRef}
              data-testid="builder-canvas"
              className="relative w-full aspect-video rounded-lg overflow-hidden select-none border border-dashed border-navy-600"
              style={{
                backgroundImage: 'radial-gradient(rgba(255, 255, 255, 0.08) 1px, transparent 1px)',
                backgroundSize: '24px 24px',
              }}
            >
              <button
                type="button"
                tabIndex={-1}
                aria-label="Deselect component"
                onClick={() => setSelectedId(null)}
                className="absolute inset-0 w-full h-full cursor-default"
              />
              {components.map((comp) => (
                <div
                  key={comp.instance_id}
                  role="button"
                  tabIndex={0}
                  aria-label={`${titleFor(comp)} at ${comp.x},${comp.y}; arrow keys move it`}
                  aria-pressed={comp.instance_id === selectedId}
                  data-testid={`component-${comp.instance_id}`}
                  onMouseDown={(e) => startDrag(e, comp)}
                  onKeyDown={(e) => keyNudge(e, comp)}
                  className={`absolute rounded cursor-move flex flex-col justify-between p-2 overflow-hidden ${
                    comp.instance_id === selectedId
                      ? 'ring-2 ring-gold-400 bg-navy-900/90'
                      : 'border border-sky-400/40 bg-navy-900/75 hover:border-gold-400/80'
                  } ${comp.visible ? 'opacity-100' : 'opacity-40'}`}
                  style={{
                    left: `${(comp.x / activeRes.width) * 100}%`,
                    top: `${(comp.y / activeRes.height) * 100}%`,
                    width: `${(comp.width / activeRes.width) * 100}%`,
                    height: `${(comp.height / activeRes.height) * 100}%`,
                    zIndex: comp.z_index,
                  }}
                >
                  <div className="flex items-center justify-between text-[11px] font-semibold text-sky-100 pointer-events-none truncate">
                    <span className="truncate">{titleFor(comp)}</span>
                    <span className="text-[9px] px-1 py-0.5 rounded bg-navy-800 text-gold-400 font-mono">
                      Z:{comp.z_index}
                    </span>
                  </div>
                  <div className="text-[10px] text-navy-400 font-mono pointer-events-none">
                    {comp.x},{comp.y} • {comp.width}×{comp.height}
                  </div>
                </div>
              ))}
              {components.length === 0 && (
                <div className="absolute inset-0 flex flex-col items-center justify-center text-navy-500 pointer-events-none">
                  <TvIcon className="w-12 h-12 stroke-1 mb-2 opacity-50" />
                  <p className="text-sm">Empty Canvas</p>
                  <p className="text-xs text-navy-600 mt-1">
                    Pick a component from the palette to place it on this scene
                  </p>
                </div>
              )}
            </div>
          </div>

          {selected ? (
            <div className="bg-navy-800 border border-navy-700 rounded-lg p-4">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-semibold text-sky-100">
                  Component: <span className="text-gold-400">{selected.widget_id}</span>
                </h3>
                <div className="flex items-center space-x-2">
                  <button
                    type="button"
                    onClick={() => updateComponent(selected.instance_id, { visible: !selected.visible })}
                    aria-label={selected.visible ? 'Hide from overlay' : 'Show on overlay'}
                    className={`p-1.5 rounded ${selected.visible ? 'text-sky-300' : 'text-navy-500'}`}
                  >
                    {selected.visible ? <EyeIcon className="w-5 h-5" /> : <EyeSlashIcon className="w-5 h-5" />}
                  </button>
                  <button
                    type="button"
                    onClick={() => removeComponent(selected.instance_id)}
                    aria-label="Remove component"
                    className="p-1.5 text-red-400 hover:text-red-300 rounded"
                  >
                    <TrashIcon className="w-5 h-5" />
                  </button>
                </div>
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 text-xs">
                <NumberField
                  label="X Position"
                  value={selected.x}
                  min={0}
                  max={activeRes.width - MIN_SIZE}
                  onCommit={(x) => updateComponent(selected.instance_id, { x })}
                />
                <NumberField
                  label="Y Position"
                  value={selected.y}
                  min={0}
                  max={activeRes.height - MIN_SIZE}
                  onCommit={(y) => updateComponent(selected.instance_id, { y })}
                />
                <NumberField
                  label="Width"
                  value={selected.width}
                  min={MIN_SIZE}
                  max={activeRes.width}
                  onCommit={(width) => updateComponent(selected.instance_id, { width })}
                />
                <NumberField
                  label="Height"
                  value={selected.height}
                  min={MIN_SIZE}
                  max={activeRes.height}
                  onCommit={(height) => updateComponent(selected.instance_id, { height })}
                />
                <NumberField
                  label="Layer (Z-Index)"
                  value={selected.z_index}
                  min={-1000}
                  max={1000}
                  onCommit={(z_index) => updateComponent(selected.instance_id, { z_index })}
                />
              </div>
              <div className="mt-4 pt-3 border-t border-navy-700 flex flex-wrap items-center gap-2">
                <span className="text-xs text-navy-400 mr-2">Quick Align:</span>
                {[
                  ['center', 'Center'],
                  ['top-left', 'Top-Left'],
                  ['top-right', 'Top-Right'],
                  ['bottom-left', 'Bottom-Left'],
                  ['bottom-right', 'Bottom-Right'],
                  ['bottom-banner', 'Bottom Banner'],
                ].map(([preset, label]) => (
                  <button
                    key={preset}
                    type="button"
                    onClick={() => align(preset)}
                    className="px-2.5 py-1 bg-navy-900 hover:bg-navy-700 text-navy-300 rounded text-xs border border-navy-700"
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="bg-navy-800 border border-navy-700 rounded-lg p-4 text-xs text-navy-400 flex items-center justify-center">
              <span>Click a component on the canvas to inspect its position, size and layer.</span>
            </div>
          )}
        </div>
      </div>

      {showClearConfirm && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="clear-canvas-modal-title"
            className="bg-navy-800 border border-navy-700 rounded-lg p-6 max-w-sm w-full"
          >
            <h3 id="clear-canvas-modal-title" className="text-base font-semibold text-sky-100 mb-2">
              Clear Canvas Layout?
            </h3>
            <p className="text-xs text-navy-300 mb-5">
              This removes every component from the composite overlay scene and cannot be undone.
            </p>
            <div className="flex justify-end space-x-3">
              <button
                type="button"
                onClick={() => setShowClearConfirm(false)}
                className="px-3 py-1.5 text-xs text-navy-300 hover:text-sky-100"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={clearCanvas}
                className="px-4 py-1.5 bg-red-500 hover:bg-red-600 text-white font-medium rounded text-xs"
              >
                Clear All
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
