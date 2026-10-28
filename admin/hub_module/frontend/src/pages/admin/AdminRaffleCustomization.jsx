import { useState, useEffect, useRef } from 'react';
import { useParams } from 'react-router-dom';
import api from '../../services/api';
import { onActivate } from '../../components/ui/a11y';
import {
  SpeakerWaveIcon,
  ArrowUpTrayIcon,
  TrashIcon,
  PlayIcon,
  StopIcon,
  SparklesIcon,
} from '@heroicons/react/24/outline';

const EVENT_TYPES = [
  {
    key: 'raffle_start',
    label: 'Raffle Start',
    description: 'Plays when a raffle begins and members can start entering.',
    defaultMessage: '🎟️ A raffle has started in {{community_name}}! Use !enter to join. Prize: {{prize_name}}',
  },
  {
    key: 'raffle_winner',
    label: 'Raffle Winner',
    description: 'Plays when the raffle winner is drawn.',
    defaultMessage: '🎉 Congratulations {{winner_name}}! You won {{prize_name}} in the raffle!',
  },
  {
    key: 'raffle_end',
    label: 'Raffle End',
    description: 'Plays when a raffle closes with no winner or after the prize is claimed.',
    defaultMessage: '🎟️ The raffle in {{community_name}} has ended. {{entry_count}} entries were submitted.',
  },
  {
    key: 'giveaway_start',
    label: 'Giveaway Start',
    description: 'Plays when a giveaway is announced.',
    defaultMessage: '🎁 Giveaway time in {{community_name}}! Prize: {{prize_name}} — enter now!',
  },
  {
    key: 'giveaway_winner',
    label: 'Giveaway Winner',
    description: 'Plays when the giveaway winner is selected.',
    defaultMessage: '🥳 {{winner_name}} just won {{prize_name}} in the {{community_name}} giveaway!',
  },
  {
    key: 'giveaway_end',
    label: 'Giveaway End',
    description: 'Plays when a giveaway concludes.',
    defaultMessage: '🎁 The giveaway in {{community_name}} has ended. Thanks to all {{entry_count}} participants!',
  },
];

const TEMPLATE_VARIABLES = [
  { label: '{{winner_name}}', description: 'Name of the winner' },
  { label: '{{prize_name}}', description: 'Name of the prize' },
  { label: '{{community_name}}', description: 'Community display name' },
  { label: '{{entry_count}}', description: 'Number of entries submitted' },
];

function renderPreview(template, eventKey) {
  if (!template) return null;
  const samples = {
    winner_name: 'PenguinFan42',
    prize_name: 'Gaming Headset',
    community_name: 'PenguinTech Community',
    entry_count: '128',
  };
  return template.replace(/\{\{(\w+)\}\}/g, (_, key) => samples[key] || `{{${key}}}`);
}

function EventCard({ eventDef, customization, onSave, onDelete, onUpload }) {
  const [messageTemplate, setMessageTemplate] = useState(
    customization?.message_template ?? eventDef.defaultMessage
  );
  const [isDirty, setIsDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [playingAudio, setPlayingAudio] = useState(false);
  const [message, setMessage] = useState(null);
  const audioRef = useRef(null);
  const fileInputRef = useRef(null);

  useEffect(() => {
    setMessageTemplate(customization?.message_template ?? eventDef.defaultMessage);
    setIsDirty(false);
  }, [customization, eventDef.defaultMessage]);

  function handleTemplateChange(val) {
    setMessageTemplate(val);
    setIsDirty(val !== (customization?.message_template ?? eventDef.defaultMessage));
  }

  function insertVariable(variable) {
    setMessageTemplate((prev) => prev + variable);
    setIsDirty(true);
  }

  async function handleSave() {
    setSaving(true);
    setMessage(null);
    try {
      await onSave(eventDef.key, { message_template: messageTemplate });
      setIsDirty(false);
      setMessage({ type: 'success', text: 'Saved' });
    } catch {
      setMessage({ type: 'error', text: 'Failed to save' });
    } finally {
      setSaving(false);
    }
  }

  async function handleReset() {
    setMessage(null);
    try {
      await onDelete(eventDef.key);
      setMessageTemplate(eventDef.defaultMessage);
      setIsDirty(false);
      setMessage({ type: 'success', text: 'Reset to default' });
    } catch {
      setMessage({ type: 'error', text: 'Failed to reset' });
    }
  }

  async function handleFileUpload(file) {
    if (!file) return;
    const validFormats = ['audio/mpeg', 'audio/ogg', 'audio/wav'];
    const validExts = ['mp3', 'ogg', 'wav'];
    const ext = file.name.split('.').pop()?.toLowerCase();
    if (!validFormats.includes(file.type) && !validExts.includes(ext)) {
      setMessage({ type: 'error', text: 'Invalid format. Use MP3, OGG, or WAV.' });
      return;
    }
    if (file.size > 2 * 1024 * 1024) {
      setMessage({ type: 'error', text: 'File exceeds 2MB limit.' });
      return;
    }
    setUploading(true);
    setMessage(null);
    try {
      await onUpload(eventDef.key, file);
      setMessage({ type: 'success', text: 'Sound uploaded' });
    } catch {
      setMessage({ type: 'error', text: 'Upload failed' });
    } finally {
      setUploading(false);
    }
  }

  function handleDrop(e) {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    handleFileUpload(file);
  }

  function handlePlay() {
    if (!customization?.sound_url) return;
    if (playingAudio) {
      audioRef.current?.pause();
      setPlayingAudio(false);
      return;
    }
    if (!audioRef.current) {
      audioRef.current = new Audio(customization.sound_url);
      audioRef.current.addEventListener('ended', () => setPlayingAudio(false));
    } else {
      audioRef.current.src = customization.sound_url;
    }
    audioRef.current.play().then(() => setPlayingAudio(true)).catch(() => {
      setMessage({ type: 'error', text: 'Could not play audio' });
    });
  }

  const preview = renderPreview(messageTemplate, eventDef.key);
  const hasSound = !!(customization?.sound_url && customization?.sound_filename);

  return (
    <div className="bg-navy-900 border border-navy-700 rounded-xl p-5 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-gold-400 font-semibold text-base">{eventDef.label}</h3>
          <p className="text-navy-400 text-xs mt-0.5">{eventDef.description}</p>
        </div>
        <button
          onClick={handleReset}
          className="flex items-center space-x-1 text-xs text-navy-400 hover:text-red-400 transition-colors"
          title="Reset to default"
        >
          <TrashIcon className="w-4 h-4" />
          <span>Reset</span>
        </button>
      </div>

      {/* Sound upload zone */}
      <div>
        <div className="flex items-center space-x-2 mb-2">
          <SpeakerWaveIcon className="w-4 h-4 text-navy-400" />
          <span className="text-xs font-medium text-navy-300 uppercase tracking-wider">Custom Sound</span>
        </div>
        <div
          onDrop={handleDrop}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          className={`border-2 border-dashed rounded-lg p-4 transition-colors text-center ${
            dragOver
              ? 'border-sky-500 bg-sky-500/10'
              : 'border-navy-700 hover:border-navy-500 bg-navy-950/50'
          }`}
        >
          {hasSound ? (
            <div className="flex items-center justify-between">
              <div className="text-left">
                <p className="text-sm text-sky-300 font-medium">{customization.sound_filename}</p>
                <p className="text-xs text-navy-500 mt-0.5">
                  {customization.sound_format?.toUpperCase()} ·{' '}
                  {customization.sound_size_bytes
                    ? `${(customization.sound_size_bytes / 1024).toFixed(1)} KB`
                    : ''}
                </p>
              </div>
              <div className="flex items-center space-x-2">
                <button
                  onClick={handlePlay}
                  className="p-2 rounded-lg bg-navy-800 hover:bg-navy-700 text-sky-400 transition-colors"
                  title={playingAudio ? 'Stop' : 'Play'}
                >
                  {playingAudio ? <StopIcon className="w-4 h-4" /> : <PlayIcon className="w-4 h-4" />}
                </button>
                <button
                  onClick={() => fileInputRef.current?.click()}
                  className="p-2 rounded-lg bg-navy-800 hover:bg-navy-700 text-gold-400 transition-colors"
                  title="Replace"
                >
                  <ArrowUpTrayIcon className="w-4 h-4" />
                </button>
              </div>
            </div>
          ) : (
            <div
              role="button"
              tabIndex={0}
              onKeyDown={onActivate(() => fileInputRef.current?.click())}
              className="cursor-pointer"
              onClick={() => fileInputRef.current?.click()}
            >
              <ArrowUpTrayIcon className="w-6 h-6 text-navy-500 mx-auto mb-1" />
              <p className="text-xs text-navy-400">
                {uploading ? 'Uploading…' : 'Drop MP3/OGG/WAV here or click to upload (max 2MB)'}
              </p>
            </div>
          )}
        </div>
        <input
          aria-label="Upload sound file"
          ref={fileInputRef}
          type="file"
          accept=".mp3,.ogg,.wav,audio/mpeg,audio/ogg,audio/wav"
          className="hidden"
          onChange={(e) => handleFileUpload(e.target.files[0])}
        />
      </div>

      {/* Message template */}
      <div>
        <div className="flex items-center space-x-2 mb-2">
          <SparklesIcon className="w-4 h-4 text-navy-400" />
          <span className="text-xs font-medium text-navy-300 uppercase tracking-wider">Message Template</span>
        </div>
        <textarea
          aria-label="Enter message template"
          value={messageTemplate}
          onChange={(e) => handleTemplateChange(e.target.value)}
          rows={3}
          className="w-full bg-navy-950 border border-navy-700 rounded-lg px-3 py-2 text-sm text-sky-100
                     focus:outline-none focus:ring-2 focus:ring-sky-500 focus:border-transparent
                     placeholder-navy-600 resize-none"
          placeholder="Enter message template…"
        />

        {/* Variable chips */}
        <div className="flex flex-wrap gap-1.5 mt-2">
          {TEMPLATE_VARIABLES.map((v) => (
            <button
              key={v.label}
              onClick={() => insertVariable(v.label)}
              title={v.description}
              className="px-2 py-0.5 text-xs rounded bg-navy-800 text-sky-300 hover:bg-navy-700
                         border border-navy-600 hover:border-sky-500 transition-colors font-mono"
            >
              {v.label}
            </button>
          ))}
        </div>

        {/* Preview */}
        {preview && (
          <div className="mt-3 p-3 bg-navy-950 border border-navy-800 rounded-lg">
            <p className="text-[10px] text-navy-500 uppercase tracking-wider mb-1">Preview</p>
            <p className="text-sm text-sky-200">{preview}</p>
          </div>
        )}
      </div>

      {/* Save button + feedback */}
      <div className="flex items-center justify-between pt-1">
        {message ? (
          <p className={`text-xs ${message.type === 'success' ? 'text-emerald-400' : 'text-red-400'}`}>
            {message.text}
          </p>
        ) : (
          <span />
        )}
        <button
          onClick={handleSave}
          disabled={!isDirty || saving}
          className="px-4 py-1.5 text-sm font-medium rounded-lg transition-colors
                     bg-sky-600 hover:bg-sky-500 text-white
                     disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  );
}

function AdminRaffleCustomization() {
  const { communityId } = useParams();
  const [customizations, setCustomizations] = useState({});
  const [loading, setLoading] = useState(true);
  const [pageMessage, setPageMessage] = useState(null);

  useEffect(() => {
    console.log('[AdminRaffleCustomization] Mounting', { communityId });
    fetchCustomizations();
  }, [communityId]);

  async function fetchCustomizations() {
    setLoading(true);
    try {
      const response = await api.get(`/api/v1/admin/${communityId}/raffle-customization`);
      if (response.data.success) {
        setCustomizations(response.data.customizations || {});
        console.log('[AdminRaffleCustomization] Loaded customizations', {
          eventTypes: Object.keys(response.data.customizations || {}),
        });
      }
    } catch (err) {
      console.error('[AdminRaffleCustomization] Failed to load', { error: err?.message });
      setPageMessage({ type: 'error', text: 'Failed to load raffle customizations' });
    } finally {
      setLoading(false);
    }
  }

  async function handleSave(eventType, payload) {
    const response = await api.put(
      `/api/v1/admin/${communityId}/raffle-customization/${eventType}`,
      payload
    );
    if (response.data.success) {
      setCustomizations((prev) => ({
        ...prev,
        [eventType]: response.data.customization,
      }));
      console.log('[AdminRaffleCustomization] Saved', { eventType });
    } else {
      throw new Error(response.data.error?.message || 'Save failed');
    }
  }

  async function handleDelete(eventType) {
    await api.delete(`/api/v1/admin/${communityId}/raffle-customization/${eventType}`);
    setCustomizations((prev) => {
      const next = { ...prev };
      delete next[eventType];
      return next;
    });
    console.log('[AdminRaffleCustomization] Reset', { eventType });
  }

  async function handleUpload(eventType, file) {
    const formData = new FormData();
    formData.append('sound', file);
    const response = await api.post(
      `/api/v1/admin/${communityId}/raffle-customization/${eventType}/upload`,
      formData,
      { headers: { 'Content-Type': 'multipart/form-data' } }
    );
    if (response.data.success) {
      setCustomizations((prev) => ({
        ...prev,
        [eventType]: response.data.customization,
      }));
      console.log('[AdminRaffleCustomization] Sound uploaded', { eventType, filename: response.data.customization?.sound_filename });
    } else {
      throw new Error(response.data.error?.message || 'Upload failed');
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400" />
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center space-x-3">
            <h1 className="text-2xl font-bold text-gold-400">Raffle &amp; Giveaway Customization</h1>
            <span className="px-2 py-0.5 text-[10px] rounded bg-gold-500 text-navy-900 font-bold uppercase tracking-wider">
              PRO
            </span>
          </div>
          <p className="text-navy-400 text-sm mt-1">
            Configure custom sounds and message templates for each raffle and giveaway event.
            Use the variable chips to insert dynamic values into your messages.
          </p>
        </div>
      </div>

      {pageMessage && (
        <div
          className={`px-4 py-3 rounded-lg text-sm ${
            pageMessage.type === 'success'
              ? 'bg-emerald-500/10 border border-emerald-500/30 text-emerald-400'
              : 'bg-red-500/10 border border-red-500/30 text-red-400'
          }`}
        >
          {pageMessage.text}
        </div>
      )}

      {/* Raffle events */}
      <section>
        <h2 className="text-sm font-semibold text-navy-400 uppercase tracking-wider mb-3">
          Raffle Events
        </h2>
        <div className="grid grid-cols-1 gap-4">
          {EVENT_TYPES.filter((e) => e.key.startsWith('raffle_')).map((eventDef) => (
            <EventCard
              key={eventDef.key}
              eventDef={eventDef}
              customization={customizations[eventDef.key] || null}
              onSave={handleSave}
              onDelete={handleDelete}
              onUpload={handleUpload}
            />
          ))}
        </div>
      </section>

      {/* Giveaway events */}
      <section>
        <h2 className="text-sm font-semibold text-navy-400 uppercase tracking-wider mb-3">
          Giveaway Events
        </h2>
        <div className="grid grid-cols-1 gap-4">
          {EVENT_TYPES.filter((e) => e.key.startsWith('giveaway_')).map((eventDef) => (
            <EventCard
              key={eventDef.key}
              eventDef={eventDef}
              customization={customizations[eventDef.key] || null}
              onSave={handleSave}
              onDelete={handleDelete}
              onUpload={handleUpload}
            />
          ))}
        </div>
      </section>

      {/* Template variable reference */}
      <section className="bg-navy-900 border border-navy-700 rounded-xl p-5">
        <h2 className="text-sm font-semibold text-navy-400 uppercase tracking-wider mb-3">
          Available Template Variables
        </h2>
        <div className="grid grid-cols-2 gap-2">
          {TEMPLATE_VARIABLES.map((v) => (
            <div key={v.label} className="flex items-center space-x-2">
              <code className="px-2 py-0.5 text-xs font-mono rounded bg-navy-800 text-sky-300 border border-navy-600">
                {v.label}
              </code>
              <span className="text-xs text-navy-400">{v.description}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

export default AdminRaffleCustomization;
