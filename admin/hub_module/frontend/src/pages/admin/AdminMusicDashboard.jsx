import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import {
  PlayIcon,
  PauseIcon,
  ForwardIcon,
  MusicalNoteIcon,
  Cog6ToothIcon,
  SparklesIcon,
  RadioIcon,
  ChevronRightIcon,
  ExclamationTriangleIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';

function AdminMusicDashboard() {
  const { communityId } = useParams();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [data, setData] = useState({
    currentTrack: null,
    mode: 'queue',
    isPlaying: false,
    queue: [],
    recentRequests: [],
    providers: [],
  });

  useEffect(() => {
    fetchMusicData();
  }, [communityId]);

  const fetchMusicData = async () => {
    try {
      setLoading(true);
      setError(null);

      // Attempt to fetch music data from the unified music module
      // This assumes a GET endpoint exists for music dashboard data
      const response = await adminApi.getMusicDashboard?.(communityId);

      if (response?.data) {
        setData({
          currentTrack: response.data.currentTrack || null,
          mode: response.data.mode || 'queue',
          isPlaying: response.data.isPlaying || false,
          queue: response.data.queue || [],
          recentRequests: response.data.recentRequests || [],
          providers: response.data.providers || [],
        });
      } else {
        // Fallback: Set mock data for demonstration
        setData({
          currentTrack: {
            title: 'No Track Playing',
            artist: 'Waddles Music',
            duration: 0,
            progress: 0,
            thumbnail: null,
          },
          mode: 'queue',
          isPlaying: false,
          queue: [],
          recentRequests: [],
          providers: [
            { name: 'Spotify', status: 'disconnected', color: 'bg-green-500/20 text-green-300 border-green-500/30' },
            { name: 'YouTube', status: 'disconnected', color: 'bg-red-500/20 text-red-300 border-red-500/30' },
            { name: 'SoundCloud', status: 'disconnected', color: 'bg-orange-500/20 text-orange-300 border-orange-500/30' },
          ],
        });
      }
    } catch (err) {
      console.error('Failed to fetch music data:', err);
      // Set mock data for demonstration when API not available
      setData({
        currentTrack: {
          title: 'No Track Playing',
          artist: 'Waddles Music',
          duration: 0,
          progress: 0,
          thumbnail: null,
        },
        mode: 'queue',
        isPlaying: false,
        queue: [],
        recentRequests: [],
        providers: [
          { name: 'Spotify', status: 'disconnected', color: 'bg-green-500/20 text-green-300 border-green-500/30' },
          { name: 'YouTube', status: 'disconnected', color: 'bg-red-500/20 text-red-300 border-red-500/30' },
          { name: 'SoundCloud', status: 'disconnected', color: 'bg-orange-500/20 text-orange-300 border-orange-500/30' },
        ],
      });
    } finally {
      setLoading(false);
    }
  };

  const handlePlayPause = async () => {
    try {
      if (data.isPlaying) {
        await adminApi.pauseMusic?.(communityId);
      } else {
        await adminApi.playMusic?.(communityId);
      }
      setData({
        ...data,
        isPlaying: !data.isPlaying,
      });
    } catch (err) {
      console.error('Failed to toggle playback:', err);
      setError('Failed to toggle playback');
    }
  };

  const handleSkip = async () => {
    try {
      await adminApi.skipTrack?.(communityId);
      // Refresh data after skip
      fetchMusicData();
    } catch (err) {
      console.error('Failed to skip track:', err);
      setError('Failed to skip track');
    }
  };

  const formatDuration = (seconds) => {
    if (!seconds || seconds <= 0) return '0:00';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-sky-100">Music Dashboard</h1>
        <p className="text-navy-400 mt-1">
          Manage music playback, queue, and provider settings
        </p>
      </div>

      {/* Error Message */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <ExclamationTriangleIcon className="w-5 h-5 text-red-400" />
            <span className="text-red-400">{error}</span>
          </div>
          <button aria-label="Close" onClick={() => setError(null)} className="text-red-400 hover:text-red-300">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
      )}

      {/* Current Playback Status Card */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-8">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h2 className="text-lg font-semibold text-sky-100 mb-2">Now Playing</h2>
            <p className="text-sm text-navy-400">Mode: <span className="text-gold-400 font-medium capitalize">{data.mode}</span></p>
          </div>
          <div className="w-16 h-16 bg-navy-700 rounded-lg flex items-center justify-center">
            <MusicalNoteIcon className="w-8 h-8 text-gold-400" />
          </div>
        </div>

        {/* Track Info */}
        <div className="bg-navy-900 rounded-lg p-6 mb-6">
          {data.currentTrack ? (
            <>
              <h3 className="text-xl font-bold text-sky-100 truncate">{data.currentTrack.title}</h3>
              <p className="text-navy-400 mt-1 truncate">{data.currentTrack.artist}</p>

              {/* Progress Bar */}
              <div className="mt-4">
                <div className="bg-navy-700 rounded-full h-2 overflow-hidden">
                  <div
                    className="bg-gold-400 h-full rounded-full transition-all duration-300"
                    style={{
                      width: `${data.currentTrack.duration > 0
                        ? (data.currentTrack.progress / data.currentTrack.duration) * 100
                        : 0
                      }%`,
                    }}
                  />
                </div>
                <div className="flex justify-between text-xs text-navy-400 mt-2">
                  <span>{formatDuration(data.currentTrack.progress)}</span>
                  <span>{formatDuration(data.currentTrack.duration)}</span>
                </div>
              </div>
            </>
          ) : (
            <div className="text-center py-4">
              <MusicalNoteIcon className="w-12 h-12 text-navy-600 mx-auto mb-2" />
              <p className="text-navy-400">No track currently playing</p>
            </div>
          )}
        </div>

        {/* Quick Controls */}
        <div className="flex items-center justify-center gap-4">
          <button
            onClick={handlePlayPause}
            className="flex items-center justify-center w-12 h-12 rounded-full bg-gold-500 hover:bg-gold-600 text-navy-900 font-bold transition-colors"
            title={data.isPlaying ? 'Pause' : 'Play'}
          >
            {data.isPlaying ? (
              <PauseIcon className="w-6 h-6" />
            ) : (
              <PlayIcon className="w-6 h-6" />
            )}
          </button>
          <button
            onClick={handleSkip}
            disabled={data.queue.length === 0 && !data.currentTrack}
            className="flex items-center justify-center w-12 h-12 rounded-full bg-navy-700 hover:bg-navy-600 disabled:opacity-50 disabled:cursor-not-allowed text-sky-100 transition-colors"
            title="Skip to Next"
          >
            <ForwardIcon className="w-6 h-6" />
          </button>
        </div>
      </div>

      {/* Queue Preview */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-6">
        <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center">
          <MusicalNoteIcon className="w-5 h-5 mr-2" />
          Queue Preview
        </h2>

        {data.queue.length > 0 ? (
          <div className="space-y-3">
            {data.queue.slice(0, 5).map((track, index) => (
              <div key={index} className="flex items-center justify-between p-3 bg-navy-900 rounded-lg hover:bg-navy-700/50 transition-colors">
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-sky-100 truncate">{track.title}</p>
                  <p className="text-xs text-navy-400 truncate">{track.artist}</p>
                </div>
                <span className="text-xs text-navy-500 ml-2 flex-shrink-0">
                  {formatDuration(track.duration)}
                </span>
              </div>
            ))}
            {data.queue.length > 5 && (
              <p className="text-xs text-navy-400 text-center pt-2">
                +{data.queue.length - 5} more songs in queue
              </p>
            )}
          </div>
        ) : (
          <div className="text-center py-8">
            <MusicalNoteIcon className="w-12 h-12 text-navy-600 mx-auto mb-2" />
            <p className="text-navy-400">Queue is empty</p>
          </div>
        )}
      </div>

      {/* Recent Song Requests */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-6">
        <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center">
          <SparklesIcon className="w-5 h-5 mr-2" />
          Recent Requests
        </h2>

        {data.recentRequests.length > 0 ? (
          <div className="space-y-3">
            {data.recentRequests.slice(0, 5).map((request, index) => (
              <div key={index} className="flex items-center justify-between p-3 bg-navy-900 rounded-lg">
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-sky-100 truncate">{request.title}</p>
                  <p className="text-xs text-navy-400">Requested by <span className="text-gold-400">{request.requester}</span></p>
                </div>
                <span className="text-xs text-navy-500 ml-2 flex-shrink-0">
                  {new Date(request.requestedAt).toLocaleDateString(undefined, {
                    month: 'short',
                    day: 'numeric',
                    hour: '2-digit',
                    minute: '2-digit',
                  })}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-center py-8">
            <SparklesIcon className="w-12 h-12 text-navy-600 mx-auto mb-2" />
            <p className="text-navy-400">No recent requests</p>
          </div>
        )}
      </div>

      {/* Provider Status Cards */}
      <div>
        <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center">
          <RadioIcon className="w-5 h-5 mr-2" />
          Provider Status
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {data.providers.map((provider, index) => (
            <div
              key={index}
              className={`rounded-lg border p-4 ${provider.color}`}
            >
              <div className="flex items-center justify-between">
                <div>
                  <p className="font-medium text-sm">{provider.name}</p>
                  <p className="text-xs opacity-75 capitalize mt-1">{provider.status}</p>
                </div>
                <div className="w-3 h-3 rounded-full" style={{
                  backgroundColor: provider.status === 'connected' ? '#10b981' : '#6b7280',
                }} />
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Quick Links Section */}
      <div>
        <h2 className="text-lg font-semibold text-sky-100 mb-4">Quick Navigation</h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {/* Public queue page link */}
          <Link
            to={`/c/${communityId}/music/queue`}
            target="_blank"
            rel="noopener noreferrer"
            className="bg-navy-800 border border-navy-700 hover:border-gold-500/50 rounded-lg p-6 transition-colors group"
          >
            <div className="flex items-center justify-between">
              <div>
                <h3 className="font-semibold text-sky-100 mb-1">Public Queue Page</h3>
                <p className="text-sm text-navy-400">Share with viewers via !sq in chat</p>
              </div>
              <MusicalNoteIcon className="w-5 h-5 text-navy-500 group-hover:text-gold-400 transition-colors" />
            </div>
          </Link>

          {/* Settings Link */}
          <Link
            to={`/admin/${communityId}/music/settings`}
            className="bg-navy-800 border border-navy-700 hover:border-gold-500/50 rounded-lg p-6 transition-colors group"
          >
            <div className="flex items-center justify-between">
              <div>
                <h3 className="font-semibold text-sky-100 mb-1">Music Settings</h3>
                <p className="text-sm text-navy-400">Configure playback and defaults</p>
              </div>
              <Cog6ToothIcon className="w-5 h-5 text-navy-500 group-hover:text-gold-400 transition-colors" />
            </div>
          </Link>

          {/* Providers Link */}
          <Link
            to={`/admin/${communityId}/music/providers`}
            className="bg-navy-800 border border-navy-700 hover:border-gold-500/50 rounded-lg p-6 transition-colors group"
          >
            <div className="flex items-center justify-between">
              <div>
                <h3 className="font-semibold text-sky-100 mb-1">Providers</h3>
                <p className="text-sm text-navy-400">Connect and manage services</p>
              </div>
              <RadioIcon className="w-5 h-5 text-navy-500 group-hover:text-gold-400 transition-colors" />
            </div>
          </Link>

          {/* Radio Link */}
          <Link
            to={`/admin/${communityId}/music/radio`}
            className="bg-navy-800 border border-navy-700 hover:border-gold-500/50 rounded-lg p-6 transition-colors group"
          >
            <div className="flex items-center justify-between">
              <div>
                <h3 className="font-semibold text-sky-100 mb-1">Radio Stations</h3>
                <p className="text-sm text-navy-400">Manage radio playlists</p>
              </div>
              <SparklesIcon className="w-5 h-5 text-navy-500 group-hover:text-gold-400 transition-colors" />
            </div>
          </Link>
        </div>
      </div>

      {/* Detailed Queue Section */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg overflow-hidden">
        <div className="p-6 border-b border-navy-700">
          <h2 className="text-lg font-semibold text-sky-100 flex items-center">
            <MusicalNoteIcon className="w-5 h-5 mr-2" />
            Full Queue ({data.queue.length} songs)
          </h2>
        </div>

        {data.queue.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-navy-900">
                <tr>
                  <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">#</th>
                  <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Title</th>
                  <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Artist</th>
                  <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Duration</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-navy-700">
                {data.queue.map((track, index) => (
                  <tr key={index} className="hover:bg-navy-700/50">
                    <td className="py-3 px-4 text-navy-400 text-sm">{index + 1}</td>
                    <td className="py-3 px-4">
                      <p className="font-medium text-sky-100 truncate">{track.title}</p>
                    </td>
                    <td className="py-3 px-4 text-navy-400 text-sm truncate">{track.artist}</td>
                    <td className="py-3 px-4 text-navy-400 text-sm">{formatDuration(track.duration)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="p-8 text-center">
            <MusicalNoteIcon className="w-12 h-12 text-navy-600 mx-auto mb-2" />
            <p className="text-navy-400">Queue is empty. Add songs to get started!</p>
          </div>
        )}
      </div>

      {/* Refresh Button */}
      <div className="flex justify-end">
        <button
          onClick={fetchMusicData}
          className="px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium rounded-lg transition-colors"
        >
          Refresh Data
        </button>
      </div>
    </div>
  );
}

export default AdminMusicDashboard;
