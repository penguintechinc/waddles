import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import {
  MusicalNoteIcon,
  PlusIcon,
  TrashIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  XMarkIcon,
  SignalIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';

const PAGE_SIZE = 25;
const EMPTY_FORM = { name: '', url: '', genre: '', description: '' };

/** Server error message from hub-api's `{error: {message}}` envelope, else `fallback`. */
const errorText = (err, fallback) => err?.response?.data?.error?.message || fallback;

/**
 * Radio station admin: list (paginated), add, delete, reachability test and default
 * station, all served by hub-api `music/radio-stations`. Stations are stream URLs only;
 * provider accounts are connected on the Music Providers page, never stored here.
 */
function AdminRadioStations() {
  const { communityId } = useParams();
  const [stations, setStations] = useState([]);
  const [pagination, setPagination] = useState({ page: 1, pages: 1, total: 0 });
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);

  // Add station form
  const [showForm, setShowForm] = useState(false);
  const [formData, setFormData] = useState(EMPTY_FORM);
  const [adding, setAdding] = useState(false);

  // Action states
  const [testing, setTesting] = useState({});
  const [deleting, setDeleting] = useState({});
  const [setting, setSetting] = useState({});

  // Delete confirmation
  const [deleteConfirm, setDeleteConfirm] = useState(null);

  useEffect(() => {
    loadStations();
  }, [communityId, page]);

  const loadStations = async () => {
    try {
      setLoading(true);
      setLoadError(null);
      const response = await adminApi.getRadioStations(communityId, { page, limit: PAGE_SIZE });
      setStations(response.data.stations || []);
      setPagination(response.data.pagination || { page: 1, pages: 1, total: 0 });
    } catch (err) {
      console.error('[AdminRadioStations] Load failed', { status: err.response?.status });
      setLoadError(errorText(err, 'Failed to load radio stations'));
    } finally {
      setLoading(false);
    }
  };

  const handleFormChange = (field, value) => {
    setFormData({ ...formData, [field]: value });
  };

  const handleAddStation = async (e) => {
    e.preventDefault();
    if (!formData.name.trim() || !formData.url.trim()) {
      setError('Station name and stream URL are required');
      return;
    }
    try {
      setAdding(true);
      setError(null);
      const payload = { name: formData.name.trim(), url: formData.url.trim() };
      if (formData.genre.trim()) payload.genre = formData.genre.trim();
      if (formData.description.trim()) payload.description = formData.description.trim();
      await adminApi.addRadioStation(communityId, payload);
      setMessage({ type: 'success', text: 'Radio station added successfully' });
      setFormData(EMPTY_FORM);
      setShowForm(false);
      loadStations();
    } catch (err) {
      console.error('[AdminRadioStations] Add failed', { status: err.response?.status });
      setError(errorText(err, 'Failed to add radio station'));
    } finally {
      setAdding(false);
    }
  };

  const handleTestStream = async (stationId) => {
    try {
      setTesting((t) => ({ ...t, [stationId]: true }));
      setError(null);
      setMessage(null);
      const response = await adminApi.testRadioStreamUrl(communityId, stationId);
      const result = response.data.result || {};
      if (result.reachable) {
        setMessage({ type: 'success', text: `Stream reachable (HTTP ${result.httpStatus}).` });
      } else {
        setError(
          result.httpStatus
            ? `Stream unreachable (HTTP ${result.httpStatus}).`
            : 'Stream unreachable: no response from the stream URL.',
        );
      }
      loadStations();
    } catch (err) {
      console.error('[AdminRadioStations] Test failed', { status: err.response?.status });
      setError(errorText(err, 'Failed to test stream'));
    } finally {
      setTesting((t) => ({ ...t, [stationId]: false }));
    }
  };

  const handleSetDefault = async (stationId) => {
    try {
      setSetting((s) => ({ ...s, [stationId]: true }));
      setError(null);
      await adminApi.setDefaultRadioStation(communityId, stationId);
      setMessage({ type: 'success', text: 'Default station updated successfully' });
      loadStations();
    } catch (err) {
      console.error('[AdminRadioStations] Set default failed', { status: err.response?.status });
      setError(errorText(err, 'Failed to set default station'));
    } finally {
      setSetting((s) => ({ ...s, [stationId]: false }));
    }
  };

  const handleDelete = async (stationId) => {
    try {
      setDeleting((d) => ({ ...d, [stationId]: true }));
      setError(null);
      await adminApi.deleteRadioStation(communityId, stationId);
      setMessage({ type: 'success', text: 'Radio station removed successfully' });
      setDeleteConfirm(null);
      loadStations();
    } catch (err) {
      console.error('[AdminRadioStations] Delete failed', { status: err.response?.status });
      setError(errorText(err, 'Failed to remove radio station'));
    } finally {
      setDeleting((d) => ({ ...d, [stationId]: false }));
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div role="status" aria-label="Loading radio stations" className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div role="alert" className="bg-red-500/10 border border-red-500/30 rounded-lg p-6 space-y-3">
        <div className="flex items-center gap-3 text-red-300">
          <ExclamationTriangleIcon className="w-5 h-5" />
          <span>{loadError}</span>
        </div>
        <button type="button" onClick={loadStations} className="btn btn-primary text-sm">
          Retry
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-sky-100">Radio Stations</h1>
          <p className="text-navy-400 mt-1">
            Manage the stream URLs your community can play
          </p>
        </div>
        <button
          onClick={() => setShowForm(!showForm)}
          className="inline-flex items-center space-x-2 px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium rounded-lg transition-colors"
        >
          <PlusIcon className="w-5 h-5" />
          <span>Add Station</span>
        </button>
      </div>

      {/* Messages */}
      {error && (
        <div role="alert" className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <ExclamationTriangleIcon className="w-5 h-5 text-red-400" />
            <span className="text-red-400">{error}</span>
          </div>
          <button aria-label="Close" onClick={() => setError(null)} className="text-red-400 hover:text-red-300">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
      )}

      {message && (
        <div
          role="status"
          className="rounded-lg p-4 flex items-center justify-between bg-emerald-500/20 text-emerald-300 border border-emerald-500/30"
        >
          <div className="flex items-center space-x-3">
            <CheckCircleIcon className="w-5 h-5" />
            <span>{message.text}</span>
          </div>
          <button aria-label="Close" onClick={() => setMessage(null)} className="hover:opacity-75">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
      )}

      {/* Add Station Form */}
      {showForm && (
        <div className="bg-navy-800 border border-navy-700 rounded-lg p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Add New Radio Station</h2>
          <form onSubmit={handleAddStation} className="space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label htmlFor="admin-radio-stations-station-name" className="block text-sm font-medium text-navy-300 mb-2">
                  Station Name *
                </label>
                <input
                  id="admin-radio-stations-station-name"
                  type="text"
                  maxLength={255}
                  value={formData.name}
                  onChange={(e) => handleFormChange('name', e.target.value)}
                  placeholder="e.g., Lofi Hip Hop"
                  className="w-full bg-navy-900 border border-navy-700 rounded-lg px-4 py-2 text-sky-100 focus:border-gold-500"
                />
              </div>

              <div>
                <label htmlFor="admin-radio-stations-genre" className="block text-sm font-medium text-navy-300 mb-2">
                  Genre
                </label>
                <input
                  id="admin-radio-stations-genre"
                  type="text"
                  maxLength={100}
                  value={formData.genre}
                  onChange={(e) => handleFormChange('genre', e.target.value)}
                  placeholder="e.g., lofi"
                  className="w-full bg-navy-900 border border-navy-700 rounded-lg px-4 py-2 text-sky-100 focus:border-gold-500"
                />
              </div>

              <div className="md:col-span-2">
                <label htmlFor="admin-radio-stations-stream-url" className="block text-sm font-medium text-navy-300 mb-2">
                  Stream URL *
                </label>
                <input
                  id="admin-radio-stations-stream-url"
                  type="url"
                  maxLength={2048}
                  value={formData.url}
                  onChange={(e) => handleFormChange('url', e.target.value)}
                  placeholder="https://stream.example.com/live"
                  className="w-full bg-navy-900 border border-navy-700 rounded-lg px-4 py-2 text-sky-100 focus:border-gold-500"
                />
                <p className="text-xs text-navy-500 mt-1">
                  A public http(s) Icecast/Shoutcast or similar stream URL
                </p>
              </div>

              <div className="md:col-span-2">
                <label htmlFor="admin-radio-stations-description" className="block text-sm font-medium text-navy-300 mb-2">
                  Description
                </label>
                <input
                  id="admin-radio-stations-description"
                  type="text"
                  maxLength={1000}
                  value={formData.description}
                  onChange={(e) => handleFormChange('description', e.target.value)}
                  className="w-full bg-navy-900 border border-navy-700 rounded-lg px-4 py-2 text-sky-100 focus:border-gold-500"
                />
              </div>
            </div>

            <div className="flex gap-3 pt-4">
              <button
                type="submit"
                disabled={adding}
                className="flex-1 px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium rounded-lg disabled:opacity-50 transition-colors"
              >
                {adding ? 'Adding...' : 'Add Station'}
              </button>
              <button
                type="button"
                onClick={() => {
                  setShowForm(false);
                  setFormData(EMPTY_FORM);
                }}
                className="flex-1 px-4 py-2 bg-navy-700 hover:bg-navy-600 text-sky-100 font-medium rounded-lg transition-colors"
              >
                Cancel
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Stations List */}
      {stations.length === 0 ? (
        <div data-testid="radio-stations-empty" className="bg-navy-800 border border-navy-700 rounded-lg p-12 text-center">
          <MusicalNoteIcon className="w-12 h-12 text-navy-500 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-sky-100 mb-2">No Radio Stations</h3>
          <p className="text-navy-400">
            Add a radio station above to get started managing music streams.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {stations.map((station) => (
            <div
              key={station.id}
              data-testid="radio-station"
              className={`rounded-lg p-6 border transition-colors ${
                station.isDefault
                  ? 'bg-gold-500/5 border-gold-500/30'
                  : 'bg-navy-800 border-navy-700'
              }`}
            >
              <div className="flex items-start justify-between mb-4">
                <div className="flex items-start space-x-4 flex-1">
                  <div className="mt-1">
                    <MusicalNoteIcon className="w-6 h-6 text-gold-400" />
                  </div>
                  <div className="flex-1">
                    <div className="flex items-center space-x-2">
                      <h3 className="text-lg font-semibold text-sky-100">{station.name}</h3>
                      {station.isDefault && (
                        <span className="inline-flex items-center space-x-1 px-2 py-0.5 rounded-full text-xs font-medium bg-gold-500/20 text-gold-300 border border-gold-500/30">
                          <CheckCircleIcon className="w-3 h-3" />
                          <span>Default</span>
                        </span>
                      )}
                    </div>
                    {(station.genre || station.description) && (
                      <div className="flex items-center space-x-3 mt-2 text-sm text-navy-400">
                        {station.genre && (
                          <span className="inline-block px-2 py-1 rounded bg-navy-700 text-sky-300">{station.genre}</span>
                        )}
                        {station.description && <span>{station.description}</span>}
                      </div>
                    )}
                  </div>
                </div>
              </div>

              {/* Station Details */}
              <div className="bg-navy-900 rounded-lg p-4 mb-4 space-y-2">
                <div>
                  <span className="text-xs text-navy-400">Stream URL:</span>
                  <p className="text-sm text-sky-300 break-all font-mono">{station.url}</p>
                </div>
                <div>
                  <span className="text-xs text-navy-400">Last Tested:</span>
                  <p className="text-sm text-sky-300">
                    {station.lastTestedAt ? new Date(station.lastTestedAt).toLocaleString() : 'Never'}
                  </p>
                </div>
              </div>

              {/* Actions */}
              <div className="flex items-center space-x-2">
                {!station.isDefault && station.isActive && (
                  <button
                    onClick={() => handleSetDefault(station.id)}
                    disabled={setting[station.id]}
                    className="inline-flex items-center space-x-2 px-3 py-2 bg-sky-600 hover:bg-sky-700 text-white rounded-lg transition-colors disabled:opacity-50"
                  >
                    <CheckCircleIcon className="w-4 h-4" />
                    <span>{setting[station.id] ? 'Setting...' : 'Set Default'}</span>
                  </button>
                )}

                <button
                  onClick={() => handleTestStream(station.id)}
                  disabled={testing[station.id]}
                  className="inline-flex items-center space-x-2 px-3 py-2 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg transition-colors disabled:opacity-50"
                >
                  <SignalIcon className={`w-4 h-4 ${testing[station.id] ? 'animate-pulse' : ''}`} />
                  <span>{testing[station.id] ? 'Testing...' : 'Test Stream'}</span>
                </button>

                <button
                  onClick={() => setDeleteConfirm(station)}
                  className="ml-auto p-2 text-red-400 hover:bg-red-500/20 rounded-lg transition-colors"
                  title="Delete station"
                  aria-label={`Delete ${station.name}`}
                >
                  <TrashIcon className="w-5 h-5" />
                </button>
              </div>

              {/* Status Indicator */}
              <div className="mt-4 flex items-center space-x-2">
                {station.isActive ? (
                  <div className="flex items-center space-x-2 text-xs text-green-400">
                    <div className="w-2 h-2 rounded-full bg-green-400"></div>
                    <span>Active</span>
                  </div>
                ) : (
                  <div className="flex items-center space-x-2 text-xs text-yellow-400">
                    <div className="w-2 h-2 rounded-full bg-yellow-400"></div>
                    <span>Inactive</span>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Pagination */}
      {pagination.pages > 1 && (
        <nav aria-label="Radio station pages" className="flex items-center justify-between text-sm text-navy-300">
          <button
            type="button"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={pagination.page <= 1}
            className="px-3 py-1 rounded bg-navy-700 disabled:opacity-50"
          >
            Previous
          </button>
          <span>
            Page {pagination.page} of {pagination.pages} ({pagination.total} stations)
          </span>
          <button
            type="button"
            onClick={() => setPage((p) => p + 1)}
            disabled={pagination.page >= pagination.pages}
            className="px-3 py-1 rounded bg-navy-700 disabled:opacity-50"
          >
            Next
          </button>
        </nav>
      )}

      {/* Delete Confirmation Modal */}
      {deleteConfirm && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div role="dialog" aria-modal="true" aria-labelledby="admin-radio-stations-dialog-1-title" className="bg-navy-800 border border-navy-700 rounded-lg p-6 max-w-md mx-4">
            <h3 id="admin-radio-stations-dialog-1-title" className="text-lg font-semibold text-sky-100 mb-2">Remove Radio Station?</h3>
            <p className="text-navy-300 mb-4">
              Are you sure you want to remove{' '}
              <span className="text-gold-400 font-medium">{deleteConfirm.name}</span>?
              {deleteConfirm.isDefault && (
                <span className="block text-yellow-400 text-sm mt-2">
                  This is the default station. Removing it will require you to set a new default.
                </span>
              )}
              This action cannot be undone.
            </p>
            <div className="flex justify-end space-x-3">
              <button
                onClick={() => setDeleteConfirm(null)}
                className="px-4 py-2 text-navy-300 hover:text-sky-100"
              >
                Cancel
              </button>
              <button
                onClick={() => handleDelete(deleteConfirm.id)}
                disabled={deleting[deleteConfirm.id]}
                className="px-4 py-2 bg-red-600 hover:bg-red-700 text-white font-medium rounded-lg disabled:opacity-50"
              >
                {deleting[deleteConfirm.id] ? 'Removing...' : 'Remove Station'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default AdminRadioStations;
