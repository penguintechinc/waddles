/**
 * Broadcast Status Modal Component
 * Displays broadcast status and allows triggering new broadcasts for announcements
 */
import { useState, useEffect } from 'react';
import {
  XMarkIcon,
  CheckCircleIcon,
  ExclamationCircleIcon,
  ClockIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../services/api';
import { getAllPlatformOptions } from '../utils/platformConfig';

const PLATFORMS = getAllPlatformOptions()
  .filter((p) => p.id !== 'hub')
  .map((p) => ({ id: p.id, label: p.label, color: p.color }));

export default function BroadcastStatusModal({
  isOpen,
  onClose,
  announcementId,
  communityId,
  onBroadcast,
}) {
  const [broadcasts, setBroadcasts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [selectedPlatforms, setSelectedPlatforms] = useState([]);
  const [broadcasting, setBroadcasting] = useState(false);

  // Fetch broadcast status on mount or when modal opens
  useEffect(() => {
    if (isOpen && announcementId && communityId) {
      fetchBroadcastStatus();
    }
  }, [isOpen, announcementId, communityId]);

  const fetchBroadcastStatus = async () => {
    setLoading(true);
    try {
      const response = await adminApi.getBroadcastStatus(
        communityId,
        announcementId
      );
      if (response.data.success) {
        setBroadcasts(response.data.broadcasts || []);
      }
    } catch (error) {
      console.error('Failed to fetch broadcast status:', error);
      setBroadcasts([]);
    } finally {
      setLoading(false);
    }
  };

  const handlePlatformToggle = (platformId) => {
    setSelectedPlatforms((prev) =>
      prev.includes(platformId)
        ? prev.filter((id) => id !== platformId)
        : [...prev, platformId]
    );
  };

  const handleBroadcast = async () => {
    if (selectedPlatforms.length === 0) return;

    setBroadcasting(true);
    try {
      // Call the onBroadcast callback
      if (onBroadcast) {
        await onBroadcast(selectedPlatforms);
      }

      // Refresh the broadcast list
      await fetchBroadcastStatus();

      // Clear selections
      setSelectedPlatforms([]);
    } catch (error) {
      console.error('Broadcast failed:', error);
    } finally {
      setBroadcasting(false);
    }
  };

  if (!isOpen) return null;

  const getStatusBadge = (status) => {
    switch (status) {
      case 'success':
        return {
          icon: CheckCircleIcon,
          bgColor: 'bg-green-500/20',
          textColor: 'text-green-400',
          label: 'Success',
        };
      case 'failed':
        return {
          icon: ExclamationCircleIcon,
          bgColor: 'bg-red-500/20',
          textColor: 'text-red-400',
          label: 'Failed',
        };
      case 'pending':
        return {
          icon: ClockIcon,
          bgColor: 'bg-yellow-500/20',
          textColor: 'text-yellow-400',
          label: 'Pending',
        };
      default:
        return {
          icon: ClockIcon,
          bgColor: 'bg-gray-500/20',
          textColor: 'text-gray-400',
          label: 'Unknown',
        };
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-navy-800 rounded-lg max-w-2xl w-full mx-4 max-h-[90vh] overflow-y-auto">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-navy-700">
          <h2 className="text-xl font-semibold text-sky-100">
            Broadcast Status
          </h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-navy-700 rounded-lg transition-colors"
          >
            <XMarkIcon className="w-6 h-6 text-gray-400 hover:text-gray-200" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 space-y-6">
          {/* New Broadcast Section */}
          <div className="border border-navy-700 rounded-lg p-4 bg-navy-900/50">
            <h3 className="text-lg font-semibold text-sky-100 mb-4">
              New Broadcast
            </h3>

            {/* Platform Checkboxes */}
            <div className="space-y-3 mb-4">
              {PLATFORMS.map((platform) => (
                <label
                  key={platform.id}
                  className="flex items-center gap-3 cursor-pointer group"
                >
                  <input
                    type="checkbox"
                    checked={selectedPlatforms.includes(platform.id)}
                    onChange={() => handlePlatformToggle(platform.id)}
                    className="w-4 h-4 rounded border-navy-600 bg-navy-800 text-purple-500 focus:ring-purple-500"
                  />
                  <span
                    className={`${platform.color} text-white text-xs px-2 py-1 rounded font-medium`}
                  >
                    {platform.label}
                  </span>
                  <span className="text-gray-400 group-hover:text-gray-300 transition-colors">
                    {platform.label}
                  </span>
                </label>
              ))}
            </div>

            {/* Broadcast Now Button */}
            <button
              onClick={handleBroadcast}
              disabled={
                broadcasting || selectedPlatforms.length === 0
              }
              className={`w-full py-2 px-4 rounded-lg font-semibold transition-colors ${
                broadcasting || selectedPlatforms.length === 0
                  ? 'bg-navy-700 text-gray-500 cursor-not-allowed'
                  : 'bg-purple-600 hover:bg-purple-700 text-white'
              }`}
            >
              {broadcasting ? 'Broadcasting...' : 'Broadcast Now'}
            </button>
          </div>

          {/* Broadcast History Section */}
          <div>
            <h3 className="text-lg font-semibold text-sky-100 mb-4">
              Broadcast History
            </h3>

            {loading ? (
              <div className="flex items-center justify-center py-8">
                <div className="animate-spin">
                  <div className="w-8 h-8 border-4 border-navy-700 border-t-purple-500 rounded-full"></div>
                </div>
              </div>
            ) : broadcasts.length === 0 ? (
              <div className="text-center py-8 text-gray-400">
                <p>No broadcasts yet</p>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  {/* Table Header */}
                  <thead>
                    <tr className="bg-navy-700 border-b border-navy-600">
                      <th className="px-4 py-2 text-left text-gray-300 font-semibold">
                        Platform
                      </th>
                      <th className="px-4 py-2 text-left text-gray-300 font-semibold">
                        Server/Channel
                      </th>
                      <th className="px-4 py-2 text-left text-gray-300 font-semibold">
                        Status
                      </th>
                      <th className="px-4 py-2 text-left text-gray-300 font-semibold">
                        Time
                      </th>
                      <th className="px-4 py-2 text-left text-gray-300 font-semibold">
                        Error
                      </th>
                    </tr>
                  </thead>

                  {/* Table Body */}
                  <tbody className="divide-y divide-navy-700">
                    {broadcasts.map((broadcast, idx) => {
                      const statusInfo = getStatusBadge(broadcast.status);
                      const StatusIcon = statusInfo.icon;

                      return (
                        <tr
                          key={idx}
                          className="bg-navy-900/50 hover:bg-navy-900 transition-colors"
                        >
                          {/* Platform */}
                          <td className="px-4 py-3">
                            {PLATFORMS.find(
                              (p) => p.id === broadcast.platform
                            ) ? (
                              <span
                                className={`${
                                  PLATFORMS.find(
                                    (p) => p.id === broadcast.platform
                                  ).color
                                } text-white text-xs px-2 py-1 rounded font-medium inline-block`}
                              >
                                {
                                  PLATFORMS.find(
                                    (p) => p.id === broadcast.platform
                                  )?.label
                                }
                              </span>
                            ) : (
                              <span className="text-gray-400">
                                {broadcast.platform}
                              </span>
                            )}
                          </td>

                          {/* Server/Channel */}
                          <td className="px-4 py-3 text-gray-300">
                            {broadcast.serverName || broadcast.channelName || '-'}
                          </td>

                          {/* Status Badge */}
                          <td className="px-4 py-3">
                            <div
                              className={`${statusInfo.bgColor} ${statusInfo.textColor} px-2 py-1 rounded flex items-center gap-1 w-fit`}
                            >
                              <StatusIcon className="w-4 h-4" />
                              <span className="font-medium">
                                {statusInfo.label}
                              </span>
                            </div>
                          </td>

                          {/* Time */}
                          <td className="px-4 py-3 text-gray-400 text-xs">
                            {broadcast.timestamp
                              ? new Date(broadcast.timestamp).toLocaleString()
                              : '-'}
                          </td>

                          {/* Error Message */}
                          <td className="px-4 py-3 text-red-400 text-xs max-w-xs truncate">
                            {broadcast.errorMessage || '-'}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="p-6 border-t border-navy-700 flex justify-end">
          <button
            onClick={onClose}
            className="px-6 py-2 bg-navy-700 hover:bg-navy-600 text-sky-100 rounded-lg font-semibold transition-colors"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
