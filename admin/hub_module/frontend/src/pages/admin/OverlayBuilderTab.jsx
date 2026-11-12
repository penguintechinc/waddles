import { useState, useEffect, useRef, useMemo } from 'react';
import {
  PlusIcon,
  TrashIcon,
  EyeIcon,
  EyeSlashIcon,
  ClipboardDocumentIcon,
  ArrowPathIcon,
  SparklesIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  TvIcon,
  MusicalNoteIcon,
  BellAlertIcon,
  UserGroupIcon,
  Squares2X2Icon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';

const RESOLUTIONS = {
  '1080p': { label: '1080p (Full HD)', width: 1920, height: 1080 },
  '2k': { label: '2K (QHD)', width: 2560, height: 1440 },
  '4k': { label: '4K (Ultra HD)', width: 3840, height: 2160 },
};

const THEMES = [
  { id: 'transparent', label: 'Transparent', bgClass: 'bg-transparent border-dashed border-navy-600' },
  { id: 'dark', label: 'Dark Navy', bgClass: 'bg-navy-950 border-solid border-navy-700' },
  { id: 'cyberpunk', label: 'Cyberpunk Neon', bgClass: 'bg-slate-950 border-solid border-cyan-500/50 shadow-[0_0_20px_rgba(6,182,212,0.15)]' },
  { id: 'fantasy', label: 'Fantasy Parchment', bgClass: 'bg-amber-950/40 border-solid border-amber-600/50' },
  { id: 'retro', label: 'Retro Synthwave', bgClass: 'bg-purple-950/60 border-solid border-fuchsia-500/50 shadow-[0_0_20px_rgba(217,70,239,0.15)]' },
];

function getCategoryIcon(cat) {
  switch (cat) {
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

export default function OverlayBuilderTab({ communityId }) {
  const [layout, setLayout] = useState({
    resolution: '1080p',
    theme: 'transparent',
    components: [],
  });
  const [widgets, setWidgets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [successMsg, setSuccessMsg] = useState(null);
  const [selectedInstanceId, setSelectedInstanceId] = useState(null);
  const [searchFilter, setSearchFilter] = useState('');
  const [selectedCategory, setSelectedCategory] = useState('all');
  const [showClearConfirm, setShowClearConfirm] = useState(false);
  const [sceneUrl, setSceneUrl] = useState('');
  const [copiedSceneUrl, setCopiedSceneUrl] = useState(false);

  const canvasRef = useRef(null);
  const isDraggingRef = useRef(false);
  const dragStartRef = useRef({ mouseX: 0, mouseY: 0, compX: 0, compY: 0 });

  const activeRes = RESOLUTIONS[layout.resolution] || RESOLUTIONS['1080p'];

  useEffect(() => {
    loadBuilderData();
  }, [communityId]);

  async function loadBuilderData() {
    setLoading(true);
    setError(null);
    try {
      const [layoutRes, widgetsRes, sourcesRes] = await Promise.all([
        adminApi.getOverlaySceneLayout(communityId),
        adminApi.getAvailableOverlayWidgets(communityId),
        adminApi.getBrowserSources(communityId).catch(() => ({ data: { sources: [] } })),
      ]);

      if (layoutRes?.data?.layout) {
        setLayout({
          resolution: layoutRes.data.layout.resolution || '1080p',
          theme: layoutRes.data.layout.theme || 'transparent',
          components: layoutRes.data.layout.components || [],
        });
      }

      const widgetList = widgetsRes?.data?.widgets || [];
      setWidgets(widgetList);

      const sources = sourcesRes?.data?.sources || [];
      const sceneSource = sources.find((s) => s.sourceType === 'scene');
      if (sceneSource?.url) {
        setSceneUrl(sceneSource.url);
      }
    } catch (err) {
      console.error('Failed to load overlay builder data:', err);
      setError('Failed to load overlay scene layout or available widgets.');
    } finally {
      setLoading(false);
    }
  }

  const selectedComponent = useMemo(() => {
    return layout.components.find((c) => c.instance_id === selectedInstanceId) || null;
  }, [layout.components, selectedInstanceId]);

  const categories = useMemo(() => {
    const set = new Set();
    widgets.forEach((w) => {
      if (w.category) set.add(w.category);
    });
    return ['all', ...Array.from(set)];
  }, [widgets]);

  const filteredWidgets = useMemo(() => {
    return widgets.filter((w) => {
      const matchesCat = selectedCategory === 'all' || w.category === selectedCategory;
      const matchesSearch =
        !searchFilter ||
        w.name.toLowerCase().includes(searchFilter.toLowerCase()) ||
        w.description.toLowerCase().includes(searchFilter.toLowerCase()) ||
        w.widget_id.toLowerCase().includes(searchFilter.toLowerCase());
      return matchesCat && matchesSearch;
    });
  }, [widgets, selectedCategory, searchFilter]);

  async function handleAddWidget(widget) {
    setError(null);
    try {
      // Default position centered or staggered
      const x = Math.max(20, Math.min(activeRes.width - widget.default_width - 20, 100 + (layout.components.length * 30) % 300));
      const y = Math.max(20, Math.min(activeRes.height - widget.default_height - 20, 100 + (layout.components.length * 30) % 200));

      const payload = {
        widget_id: widget.widget_id,
        x,
        y,
        width: widget.default_width,
        height: widget.default_height,
        z_index: layout.components.length + 1,
        is_visible: true,
      };

      const res = await adminApi.addOverlaySceneComponent(communityId, payload);
      const newComp = res?.data?.component;
      if (newComp) {
        setLayout((prev) => ({
          ...prev,
          components: [...prev.components, newComp],
        }));
        setSelectedInstanceId(newComp.instance_id);
        showFeedback(`Added "${widget.name}" to canvas`);
      }
    } catch (err) {
      console.error('Failed to add component:', err);
      setError('Could not add component to overlay.');
    }
  }

  async function handleUpdateComponent(instanceId, updates) {
    setError(null);
    try {
      const res = await adminApi.updateOverlaySceneComponent(communityId, instanceId, updates);
      const updated = res?.data?.component;
      if (updated) {
        setLayout((prev) => ({
          ...prev,
          components: prev.components.map((c) => (c.instance_id === instanceId ? updated : c)),
        }));
      }
    } catch (err) {
      console.error('Failed to update component:', err);
      setError('Failed to update component placement.');
    }
  }

  async function handleRemoveComponent(instanceId) {
    setError(null);
    try {
      await adminApi.removeOverlaySceneComponent(communityId, instanceId);
      setLayout((prev) => ({
        ...prev,
        components: prev.components.filter((c) => c.instance_id !== instanceId),
      }));
      if (selectedInstanceId === instanceId) {
        setSelectedInstanceId(null);
      }
      showFeedback('Component removed from canvas');
    } catch (err) {
      console.error('Failed to remove component:', err);
      setError('Failed to remove component from overlay.');
    }
  }

  async function handleClearCanvas() {
    setError(null);
    try {
      await adminApi.clearOverlaySceneLayout(communityId);
      setLayout((prev) => ({
        ...prev,
        components: [],
      }));
      setSelectedInstanceId(null);
      setShowClearConfirm(false);
      showFeedback('Canvas layout cleared');
    } catch (err) {
      console.error('Failed to clear canvas:', err);
      setError('Failed to clear canvas.');
    }
  }

  async function handleThemeOrResolutionChange(newRes, newTheme) {
    const resToSet = newRes || layout.resolution;
    const themeToSet = newTheme || layout.theme;
    setError(null);
    try {
      await adminApi.setOverlayCanvasTheme(communityId, {
        resolution: resToSet,
        theme: themeToSet,
      });
      setLayout((prev) => ({
        ...prev,
        resolution: resToSet,
        theme: themeToSet,
      }));
      showFeedback('Canvas settings saved');
    } catch (err) {
      console.error('Failed to save canvas settings:', err);
      setError('Failed to update canvas theme or resolution.');
    }
  }

  function handleAlign(preset) {
    if (!selectedComponent) return;
    const { width: compW, height: compH } = selectedComponent;
    let newX = selectedComponent.x;
    let newY = selectedComponent.y;

    switch (preset) {
      case 'center':
        newX = Math.round((activeRes.width - compW) / 2);
        newY = Math.round((activeRes.height - compH) / 2);
        break;
      case 'top-left':
        newX = 40;
        newY = 40;
        break;
      case 'top-right':
        newX = activeRes.width - compW - 40;
        newY = 40;
        break;
      case 'bottom-left':
        newX = 40;
        newY = activeRes.height - compH - 40;
        break;
      case 'bottom-right':
        newX = activeRes.width - compW - 40;
        newY = activeRes.height - compH - 40;
        break;
      case 'bottom-banner':
        newX = Math.round((activeRes.width - compW) / 2);
        newY = activeRes.height - compH - 40;
        break;
      default:
        break;
    }

    handleUpdateComponent(selectedComponent.instance_id, { x: newX, y: newY });
  }

  function showFeedback(msg) {
    setSuccessMsg(msg);
    setTimeout(() => setSuccessMsg(null), 3000);
  }

  async function copySceneUrl() {
    if (sceneUrl) {
      await navigator.clipboard.writeText(sceneUrl);
      setCopiedSceneUrl(true);
      setTimeout(() => setCopiedSceneUrl(false), 2000);
    }
  }

  // Mouse drag handlers on canvas components
  function handleMouseDown(e, comp) {
    e.stopPropagation();
    setSelectedInstanceId(comp.instance_id);
    isDraggingRef.current = true;
    dragStartRef.current = {
      mouseX: e.clientX,
      mouseY: e.clientY,
      compX: comp.x,
      compY: comp.y,
    };

    const handleMouseMove = (moveEvent) => {
      if (!isDraggingRef.current || !canvasRef.current) return;
      const rect = canvasRef.current.getBoundingClientRect();
      const scaleX = activeRes.width / rect.width;
      const scaleY = activeRes.height / rect.height;

      const deltaX = (moveEvent.clientX - dragStartRef.current.mouseX) * scaleX;
      const deltaY = (moveEvent.clientY - dragStartRef.current.mouseY) * scaleY;

      let nextX = Math.round(dragStartRef.current.compX + deltaX);
      let nextY = Math.round(dragStartRef.current.compY + deltaY);

      // Bound within canvas
      nextX = Math.max(0, Math.min(activeRes.width - comp.width, nextX));
      nextY = Math.max(0, Math.min(activeRes.height - comp.height, nextY));

      // Local update for responsiveness
      setLayout((prev) => ({
        ...prev,
        components: prev.components.map((c) =>
          c.instance_id === comp.instance_id ? { ...c, x: nextX, y: nextY } : c
        ),
      }));
    };

    const handleMouseUp = (upEvent) => {
      isDraggingRef.current = false;
      window.removeEventListener('mousemove', handleMouseMove);
      window.removeEventListener('mouseup', handleMouseUp);

      const rect = canvasRef.current?.getBoundingClientRect();
      if (!rect) return;
      const scaleX = activeRes.width / rect.width;
      const scaleY = activeRes.height / rect.height;

      const deltaX = (upEvent.clientX - dragStartRef.current.mouseX) * scaleX;
      const deltaY = (upEvent.clientY - dragStartRef.current.mouseY) * scaleY;

      let finalX = Math.round(dragStartRef.current.compX + deltaX);
      let finalY = Math.round(dragStartRef.current.compY + deltaY);

      finalX = Math.max(0, Math.min(activeRes.width - comp.width, finalX));
      finalY = Math.max(0, Math.min(activeRes.height - comp.height, finalY));

      handleUpdateComponent(comp.instance_id, { x: finalX, y: finalY });
    };

    window.addEventListener('mousemove', handleMouseMove);
    window.addEventListener('mouseup', handleMouseUp);
  }

  const currentThemeObj = THEMES.find((t) => t.id === layout.theme) || THEMES[0];

  if (loading) {
    return (
      <div className="flex items-center justify-center h-72">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Feedback Messages */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center space-x-3 text-red-400">
            <ExclamationTriangleIcon className="w-5 h-5 flex-shrink-0" />
            <span>{error}</span>
          </div>
          <button onClick={() => setError(null)} className="text-red-400 hover:text-red-300">
            &times;
          </button>
        </div>
      )}

      {successMsg && (
        <div className="bg-emerald-500/10 border border-emerald-500/30 rounded-lg p-3 flex items-center space-x-3 text-emerald-400">
          <CheckCircleIcon className="w-5 h-5 flex-shrink-0" />
          <span>{successMsg}</span>
        </div>
      )}

      {/* Top Action Bar & Scene URL */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-4 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center space-x-4">
          <div>
            <label className="block text-xs uppercase tracking-wider text-navy-400 mb-1">Resolution</label>
            <div className="flex rounded-lg bg-navy-900 p-1 border border-navy-700">
              {Object.keys(RESOLUTIONS).map((key) => (
                <button
                  key={key}
                  onClick={() => handleThemeOrResolutionChange(key, layout.theme)}
                  className={`px-3 py-1 text-xs font-semibold rounded-md transition-colors ${
                    layout.resolution === key
                      ? 'bg-gold-500 text-navy-900 shadow'
                      : 'text-navy-300 hover:text-sky-200'
                  }`}
                >
                  {key.toUpperCase()}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="block text-xs uppercase tracking-wider text-navy-400 mb-1">Canvas Theme</label>
            <select
              value={layout.theme}
              onChange={(e) => handleThemeOrResolutionChange(layout.resolution, e.target.value)}
              className="bg-navy-900 border border-navy-700 text-sky-100 text-xs rounded-lg px-3 py-1.5 focus:outline-none focus:border-gold-400"
            >
              {THEMES.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="flex items-center space-x-3">
          {sceneUrl && (
            <button
              onClick={copySceneUrl}
              className="flex items-center space-x-2 px-3 py-1.5 bg-navy-700 hover:bg-navy-600 text-sky-100 text-xs font-medium rounded-lg transition-colors border border-navy-600"
              title="Copy OBS Browser Source URL"
            >
              <ClipboardDocumentIcon className="w-4 h-4 text-gold-400" />
              <span>{copiedSceneUrl ? 'Copied URL!' : 'Copy Scene URL'}</span>
            </button>
          )}

          {sceneUrl && (
            <a
              href={sceneUrl}
              target="_blank"
              rel="noreferrer"
              className="flex items-center space-x-1.5 px-3 py-1.5 bg-navy-700 hover:bg-navy-600 text-sky-200 text-xs font-medium rounded-lg transition-colors border border-navy-600"
            >
              <TvIcon className="w-4 h-4 text-sky-400" />
              <span>Preview</span>
            </a>
          )}

          <button
            onClick={() => setShowClearConfirm(true)}
            className="flex items-center space-x-1 px-3 py-1.5 bg-red-500/10 hover:bg-red-500/20 text-red-400 text-xs font-medium rounded-lg transition-colors border border-red-500/30"
          >
            <TrashIcon className="w-4 h-4" />
            <span>Clear</span>
          </button>

          <button
            onClick={loadBuilderData}
            className="p-1.5 text-navy-400 hover:text-sky-300 rounded"
            title="Refresh Layout"
          >
            <ArrowPathIcon className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Main Designer Grid: Widget Palette (Left) + Virtual Canvas & Inspector (Right) */}
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        {/* Widget Palette Sidebar (1 Column) */}
        <div className="lg:col-span-1 bg-navy-800 border border-navy-700 rounded-lg p-4 flex flex-col h-[750px]">
          <div className="mb-4">
            <h2 className="text-base font-semibold text-sky-100 flex items-center space-x-2">
              <SparklesIcon className="w-5 h-5 text-gold-400" />
              <span>Component Palette</span>
            </h2>
            <p className="text-xs text-navy-400 mt-1">
              Components available for this community. Click to add to the canvas.
            </p>
          </div>

          {/* Search & Category Filter */}
          <div className="space-y-2 mb-3">
            <input
              type="text"
              placeholder="Search components..."
              value={searchFilter}
              onChange={(e) => setSearchFilter(e.target.value)}
              className="w-full bg-navy-900 border border-navy-700 rounded-lg px-3 py-1.5 text-xs text-sky-100 placeholder-navy-500 focus:outline-none focus:border-gold-400"
            />
            <div className="flex flex-wrap gap-1">
              {categories.map((cat) => (
                <button
                  key={cat}
                  onClick={() => setSelectedCategory(cat)}
                  className={`px-2 py-0.5 text-[11px] rounded capitalize ${
                    selectedCategory === cat
                      ? 'bg-gold-500/20 text-gold-400 border border-gold-500/30'
                      : 'text-navy-400 hover:text-sky-300'
                  }`}
                >
                  {cat}
                </button>
              ))}
            </div>
          </div>

          {/* Widget List */}
          <div className="flex-1 overflow-y-auto space-y-2.5 pr-1">
            {filteredWidgets.length === 0 ? (
              <div className="text-center py-8 text-xs text-navy-400">
                No matching overlay widgets found for this community.
              </div>
            ) : (
              filteredWidgets.map((w) => (
                <div
                  key={w.widget_id}
                  className="bg-navy-900 border border-navy-700/80 hover:border-gold-400/50 rounded-lg p-3 transition-colors flex flex-col justify-between"
                >
                  <div>
                    <div className="flex items-center space-x-2">
                      {getCategoryIcon(w.category)}
                      <h3 className="text-xs font-semibold text-sky-100 truncate">{w.name}</h3>
                    </div>
                    <p className="text-[11px] text-navy-400 mt-1 line-clamp-2">{w.description}</p>
                    <div className="flex items-center space-x-2 mt-2 text-[10px] text-navy-500">
                      <span>
                        {w.default_width}×{w.default_height}px
                      </span>
                      <span>•</span>
                      <span className="truncate">{w.bundle_id.replace('waddles.core.', '')}</span>
                    </div>
                  </div>
                  <button
                    onClick={() => handleAddWidget(w)}
                    className="mt-3 flex items-center justify-center space-x-1.5 w-full py-1.5 bg-navy-800 hover:bg-gold-500 hover:text-navy-900 text-gold-400 text-xs font-medium rounded border border-gold-500/30 transition-colors"
                  >
                    <PlusIcon className="w-3.5 h-3.5" />
                    <span>Add to Canvas</span>
                  </button>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Canvas & Inspector Area (3 Columns) */}
        <div className="lg:col-span-3 space-y-4 flex flex-col">
          {/* Virtual 16:9 Canvas */}
          <div className="bg-navy-850 border border-navy-700 rounded-lg p-4">
            <div className="flex items-center justify-between text-xs text-navy-400 mb-2">
              <span className="font-mono">
                Canvas: {activeRes.width} × {activeRes.height} ({activeRes.label})
              </span>
              <span>
                {layout.components.length} component{layout.components.length === 1 ? '' : 's'} placed
              </span>
            </div>

            <div
              ref={canvasRef}
              onClick={() => setSelectedInstanceId(null)}
              className={`relative w-full aspect-video rounded-lg overflow-hidden transition-all select-none ${currentThemeObj.bgClass}`}
              style={{
                // Checkerboard pattern for transparent canvas preview
                backgroundImage:
                  layout.theme === 'transparent'
                    ? 'radial-gradient(rgba(255, 255, 255, 0.08) 1px, transparent 1px)'
                    : undefined,
                backgroundSize: '24px 24px',
              }}
            >
              {layout.components.map((comp) => {
                const isSelected = comp.instance_id === selectedInstanceId;
                const leftPct = (comp.x / activeRes.width) * 100;
                const topPct = (comp.y / activeRes.height) * 100;
                const widthPct = (comp.width / activeRes.width) * 100;
                const heightPct = (comp.height / activeRes.height) * 100;

                const widgetMeta = widgets.find((w) => w.widget_id === comp.widget_id);
                const title = widgetMeta ? widgetMeta.name : comp.widget_id;

                return (
                  <div
                    key={comp.instance_id}
                    onMouseDown={(e) => handleMouseDown(e, comp)}
                    className={`absolute rounded transition-shadow cursor-move flex flex-col justify-between p-2 overflow-hidden ${
                      isSelected
                        ? 'ring-2 ring-gold-400 shadow-[0_0_15px_rgba(250,204,21,0.3)] bg-navy-900/90'
                        : 'border border-sky-400/40 bg-navy-900/75 hover:border-gold-400/80'
                    } ${!comp.is_visible ? 'opacity-40' : 'opacity-100'}`}
                    style={{
                      left: `${leftPct}%`,
                      top: `${topPct}%`,
                      width: `${widthPct}%`,
                      height: `${heightPct}%`,
                      zIndex: comp.z_index || 1,
                    }}
                  >
                    <div className="flex items-center justify-between text-[11px] font-semibold text-sky-100 pointer-events-none truncate">
                      <span className="truncate">{title}</span>
                      <span className="text-[9px] px-1 py-0.5 rounded bg-navy-800 text-gold-400 font-mono">
                        Z:{comp.z_index}
                      </span>
                    </div>

                    <div className="text-[10px] text-navy-400 font-mono pointer-events-none">
                      {comp.x},{comp.y} • {comp.width}×{comp.height}
                    </div>
                  </div>
                );
              })}

              {layout.components.length === 0 && (
                <div className="absolute inset-0 flex flex-col items-center justify-center text-navy-500 pointer-events-none">
                  <TvIcon className="w-12 h-12 stroke-1 mb-2 opacity-50" />
                  <p className="text-sm">Empty Canvas</p>
                  <p className="text-xs text-navy-600 mt-1">
                    Select a component from the left palette to place it on this scene
                  </p>
                </div>
              )}
            </div>
          </div>

          {/* Selected Component Inspector Panel */}
          {selectedComponent ? (
            <div className="bg-navy-800 border border-navy-700 rounded-lg p-4">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center space-x-2">
                  <h3 className="text-sm font-semibold text-sky-100">
                    Component: <span className="text-gold-400">{selectedComponent.widget_id}</span>
                  </h3>
                  <span className="text-xs text-navy-400 font-mono">({selectedComponent.instance_id})</span>
                </div>
                <div className="flex items-center space-x-2">
                  <button
                    onClick={() =>
                      handleUpdateComponent(selectedComponent.instance_id, {
                        is_visible: !selectedComponent.is_visible,
                      })
                    }
                    className={`p-1.5 rounded transition-colors ${
                      selectedComponent.is_visible
                        ? 'text-sky-300 hover:text-sky-100'
                        : 'text-navy-500 hover:text-navy-400'
                    }`}
                    title={selectedComponent.is_visible ? 'Hide from overlay' : 'Show on overlay'}
                  >
                    {selectedComponent.is_visible ? (
                      <EyeIcon className="w-5 h-5" />
                    ) : (
                      <EyeSlashIcon className="w-5 h-5" />
                    )}
                  </button>
                  <button
                    onClick={() => handleRemoveComponent(selectedComponent.instance_id)}
                    className="p-1.5 text-red-400 hover:text-red-300 rounded"
                    title="Remove component"
                  >
                    <TrashIcon className="w-5 h-5" />
                  </button>
                </div>
              </div>

              <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 text-xs">
                <div>
                  <label className="block text-navy-400 mb-1">X Position</label>
                  <input
                    type="number"
                    value={selectedComponent.x}
                    onChange={(e) =>
                      handleUpdateComponent(selectedComponent.instance_id, {
                        x: parseInt(e.target.value) || 0,
                      })
                    }
                    className="w-full bg-navy-900 border border-navy-700 rounded px-2.5 py-1.5 text-sky-100 font-mono"
                  />
                </div>

                <div>
                  <label className="block text-navy-400 mb-1">Y Position</label>
                  <input
                    type="number"
                    value={selectedComponent.y}
                    onChange={(e) =>
                      handleUpdateComponent(selectedComponent.instance_id, {
                        y: parseInt(e.target.value) || 0,
                      })
                    }
                    className="w-full bg-navy-900 border border-navy-700 rounded px-2.5 py-1.5 text-sky-100 font-mono"
                  />
                </div>

                <div>
                  <label className="block text-navy-400 mb-1">Width</label>
                  <input
                    type="number"
                    value={selectedComponent.width}
                    onChange={(e) =>
                      handleUpdateComponent(selectedComponent.instance_id, {
                        width: parseInt(e.target.value) || 100,
                      })
                    }
                    className="w-full bg-navy-900 border border-navy-700 rounded px-2.5 py-1.5 text-sky-100 font-mono"
                  />
                </div>

                <div>
                  <label className="block text-navy-400 mb-1">Height</label>
                  <input
                    type="number"
                    value={selectedComponent.height}
                    onChange={(e) =>
                      handleUpdateComponent(selectedComponent.instance_id, {
                        height: parseInt(e.target.value) || 100,
                      })
                    }
                    className="w-full bg-navy-900 border border-navy-700 rounded px-2.5 py-1.5 text-sky-100 font-mono"
                  />
                </div>

                <div>
                  <label className="block text-navy-400 mb-1">Layer (Z-Index)</label>
                  <input
                    type="number"
                    value={selectedComponent.z_index}
                    onChange={(e) =>
                      handleUpdateComponent(selectedComponent.instance_id, {
                        z_index: parseInt(e.target.value) || 1,
                      })
                    }
                    className="w-full bg-navy-900 border border-navy-700 rounded px-2.5 py-1.5 text-sky-100 font-mono"
                  />
                </div>
              </div>

              {/* Quick Presets */}
              <div className="mt-4 pt-3 border-t border-navy-700 flex flex-wrap items-center gap-2">
                <span className="text-xs text-navy-400 mr-2">Quick Align:</span>
                <button
                  onClick={() => handleAlign('center')}
                  className="px-2.5 py-1 bg-navy-900 hover:bg-navy-700 text-navy-300 rounded text-xs border border-navy-700"
                >
                  Center
                </button>
                <button
                  onClick={() => handleAlign('top-left')}
                  className="px-2.5 py-1 bg-navy-900 hover:bg-navy-700 text-navy-300 rounded text-xs border border-navy-700"
                >
                  Top-Left
                </button>
                <button
                  onClick={() => handleAlign('top-right')}
                  className="px-2.5 py-1 bg-navy-900 hover:bg-navy-700 text-navy-300 rounded text-xs border border-navy-700"
                >
                  Top-Right
                </button>
                <button
                  onClick={() => handleAlign('bottom-left')}
                  className="px-2.5 py-1 bg-navy-900 hover:bg-navy-700 text-navy-300 rounded text-xs border border-navy-700"
                >
                  Bottom-Left
                </button>
                <button
                  onClick={() => handleAlign('bottom-right')}
                  className="px-2.5 py-1 bg-navy-900 hover:bg-navy-700 text-navy-300 rounded text-xs border border-navy-700"
                >
                  Bottom-Right
                </button>
                <button
                  onClick={() => handleAlign('bottom-banner')}
                  className="px-2.5 py-1 bg-navy-900 hover:bg-navy-700 text-navy-300 rounded text-xs border border-navy-700"
                >
                  Bottom Banner
                </button>
              </div>
            </div>
          ) : (
            <div className="bg-navy-800 border border-navy-700 rounded-lg p-4 text-xs text-navy-400 flex items-center justify-center">
              <span>Click any component on the canvas to inspect coordinates, dimensions, and layering.</span>
            </div>
          )}
        </div>
      </div>

      {/* Clear Confirmation Modal */}
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
              This will remove all components from the full-screen composite overlay scene. This action cannot be undone.
            </p>
            <div className="flex justify-end space-x-3">
              <button
                onClick={() => setShowClearConfirm(false)}
                className="px-3 py-1.5 text-xs text-navy-300 hover:text-sky-100"
              >
                Cancel
              </button>
              <button
                onClick={handleClearCanvas}
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

