import { useState, useEffect, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';
import BotScoreBadge from '../../components/BotScoreBadge';
import { ShieldCheckIcon, ExclamationTriangleIcon, UserGroupIcon, ClockIcon } from '@heroicons/react/24/outline';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import { WADDLES_COLORS } from '../../theme/waddlebotTheme';

const CONFIDENCE_COLORS = {
  low: 'text-emerald-400',
  medium: 'text-yellow-400',
  high: 'text-red-400',
};

const RECOMMENDED_ACTIONS = [
  { id: 'none', name: 'No Action', color: 'bg-navy-700 text-navy-300' },
  { id: 'monitor', name: 'Monitor', color: 'bg-sky-500/20 text-sky-300 border-sky-500/30' },
  { id: 'warn', name: 'Warn', color: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30' },
  { id: 'timeout', name: 'Timeout', color: 'bg-orange-500/20 text-orange-300 border-orange-500/30' },
  { id: 'ban', name: 'Ban', color: 'bg-red-500/20 text-red-300 border-red-500/30' },
];

function AdminBotDetection() {
  const { communityId } = useParams();
  const { user } = useAuth();
  const [detections, setDetections] = useState([]);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [pagination, setPagination] = useState(null);
  const [filters, setFilters] = useState({
    status: 'unreviewed',
    minConfidence: 50,
  });
  const [selectedDetection, setSelectedDetection] = useState(null);
  const [showReviewModal, setShowReviewModal] = useState(false);
  const [botScore, setBotScore] = useState(null);
  const [botScoreLoading, setBotScoreLoading] = useState(true);
  const isPremium = user?.isPremium || false;

  useEffect(() => {
    fetchDetections();
  }, [communityId, page, filters]);

  useEffect(() => {
    fetchBotScore();
  }, [communityId]);

  async function fetchBotScore() {
    setBotScoreLoading(true);
    try {
      const response = await adminApi.getBotScore(communityId);
      if (response.data.success) {
        setBotScore(response.data.botScore);
      }
    } catch (err) {
      console.error('Failed to fetch bot score:', err);
    } finally {
      setBotScoreLoading(false);
    }
  }

  async function fetchDetections() {
    setLoading(true);
    try {
      const response = await adminApi.getBotDetections(communityId, {
        page,
        limit: 25,
        ...filters,
      });
      if (response.data.success) {
        setDetections(response.data.detections);
        setPagination(response.data.pagination);
      }
    } catch (err) {
      console.error('Failed to fetch bot detections:', err);
    } finally {
      setLoading(false);
    }
  }

  function getConfidenceLevel(score) {
    if (score < 50) return 'low';
    if (score < 85) return 'medium';
    return 'high';
  }

  function getConfidenceColor(score) {
    return CONFIDENCE_COLORS[getConfidenceLevel(score)];
  }

  function openReviewModal(detection) {
    setSelectedDetection(detection);
    setShowReviewModal(true);
  }

  function closeReviewModal() {
    setShowReviewModal(false);
    setSelectedDetection(null);
  }

  async function submitReview(data) {
    if (!selectedDetection) return;

    try {
      await adminApi.reviewBotDetection(communityId, selectedDetection.id, {
        action: data.action,
        notes: data.notes?.trim() || '',
      });
      closeReviewModal();
      fetchDetections();
    } catch (err) {
      console.error('Failed to submit review:', err);
      throw err;
    }
  }

  // Build fields for the review modal
  const reviewFields = useMemo(() => [
    {
      name: 'action',
      type: 'select',
      label: 'Action',
      defaultValue: selectedDetection?.recommendedAction || 'none',
      options: RECOMMENDED_ACTIONS.map((action) => ({
        value: action.id,
        label: action.name,
      })),
    },
    {
      name: 'notes',
      type: 'textarea',
      label: 'Notes (Optional)',
      placeholder: 'Add any notes about this review...',
      rows: 4,
    },
  ], [selectedDetection]);

  async function markAsReviewed(detectionId) {
    try {
      await adminApi.markBotDetectionReviewed(communityId, detectionId);
      fetchDetections();
    } catch (err) {
      console.error('Failed to mark as reviewed:', err);
    }
  }

  function updateFilter(key, value) {
    setFilters({ ...filters, [key]: value });
    setPage(1);
  }

  if (!isPremium) {
    return (
      <div className="text-center py-12">
        <div className="card p-8 max-w-2xl mx-auto">
          <svg className="w-16 h-16 text-gold-400 mx-auto mb-4" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clipRule="evenodd" />
          </svg>
          <h2 className="text-2xl font-bold text-sky-100 mb-2">Premium Feature</h2>
          <p className="text-navy-400 mb-6">
            Bot Detection is a premium feature. Upgrade your community to access advanced bot detection and moderation tools.
          </p>
          <button className="btn btn-primary">
            Upgrade to Premium
          </button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-bold text-sky-100">Bot Detection</h1>
            <span className="badge badge-gold text-xs">Premium</span>
            {botScore?.isCalculated && (
              <BotScoreBadge
                grade={botScore.grade}
                score={botScore.score}
                showScore={true}
                size="lg"
              />
            )}
          </div>
          <p className="text-navy-400 mt-1">Review and moderate suspected bot accounts</p>
        </div>
      </div>

      {/* Bot Score Overview */}
      {botScore?.isCalculated && (
        <div className="grid sm:grid-cols-4 gap-4 mb-6">
          <div className="card p-4 border-l-4 border-l-emerald-400">
            <div className="flex items-center gap-2 mb-2">
              <ShieldCheckIcon className="w-5 h-5 text-emerald-400" />
              <span className="text-xs text-navy-400 uppercase">Bad Actor Score</span>
            </div>
            <div className="text-2xl font-bold text-emerald-400">{botScore.factors?.badActor || 0}</div>
            <div className="text-xs text-navy-500">Higher = fewer bad actors</div>
          </div>
          <div className="card p-4 border-l-4 border-l-sky-400">
            <div className="flex items-center gap-2 mb-2">
              <UserGroupIcon className="w-5 h-5 text-sky-400" />
              <span className="text-xs text-navy-400 uppercase">Reputation Score</span>
            </div>
            <div className="text-2xl font-bold text-sky-400">{botScore.factors?.reputation || 0}</div>
            <div className="text-xs text-navy-500">Community health</div>
          </div>
          <div className="card p-4 border-l-4 border-l-purple-400">
            <div className="flex items-center gap-2 mb-2">
              <ShieldCheckIcon className="w-5 h-5 text-purple-400" />
              <span className="text-xs text-navy-400 uppercase">Security Score</span>
            </div>
            <div className="text-2xl font-bold text-purple-400">{botScore.factors?.security || 0}</div>
            <div className="text-xs text-navy-500">Content violations</div>
          </div>
          <div className="card p-4 border-l-4 border-l-gold-400">
            <div className="flex items-center gap-2 mb-2">
              <ExclamationTriangleIcon className="w-5 h-5 text-gold-400" />
              <span className="text-xs text-navy-400 uppercase">AI Behavioral</span>
            </div>
            <div className="text-2xl font-bold text-gold-400">{botScore.factors?.aiBehavioral || 0}</div>
            <div className="text-xs text-navy-500">Pattern analysis</div>
          </div>
        </div>
      )}

      {/* Bot Score Summary */}
      {botScore?.isCalculated && botScore.stats && (
        <div className="card p-4 mb-6">
          <div className="flex items-center justify-between flex-wrap gap-4">
            <div className="flex items-center gap-6">
              <div>
                <span className="text-sm text-navy-400">Suspected Bots:</span>
                <span className="ml-2 font-bold text-red-400">{botScore.stats.suspectedBotCount || 0}</span>
              </div>
              <div>
                <span className="text-sm text-navy-400">High Confidence:</span>
                <span className="ml-2 font-bold text-orange-400">{botScore.stats.highConfidenceBotCount || 0}</span>
              </div>
              <div>
                <span className="text-sm text-navy-400">Users Analyzed:</span>
                <span className="ml-2 font-bold text-sky-400">{botScore.stats.totalUsersAnalyzed || 0}</span>
              </div>
            </div>
            {botScore.calculatedAt && (
              <div className="flex items-center gap-2 text-xs text-navy-500">
                <ClockIcon className="w-4 h-4" />
                <span>Last calculated: {new Date(botScore.calculatedAt).toLocaleString()}</span>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Filters */}
      <div className="card p-4 mb-6">
        <div className="grid md:grid-cols-3 gap-4">
          <div>
            <label htmlFor="admin-bot-detection-status" className="block text-sm font-medium text-navy-300 mb-2">
              Status
            </label>
            <select
              id="admin-bot-detection-status"
              value={filters.status}
              onChange={(e) => updateFilter('status', e.target.value)}
              className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
            >
              <option value="all">All</option>
              <option value="unreviewed">Unreviewed</option>
              <option value="reviewed">Reviewed</option>
            </select>
          </div>
          <div>
            <label htmlFor="admin-bot-detection-minimum-confidence" className="block text-sm font-medium text-navy-300 mb-2">
              Minimum Confidence
            </label>
            <select
              id="admin-bot-detection-minimum-confidence"
              value={filters.minConfidence}
              onChange={(e) => updateFilter('minConfidence', parseInt(e.target.value))}
              className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
            >
              <option value="50">50% and above</option>
              <option value="70">70% and above</option>
              <option value="85">85% and above</option>
              <option value="95">95% and above</option>
            </select>
          </div>
          <div className="flex items-end">
            <button
              onClick={fetchDetections}
              className="btn btn-secondary w-full"
            >
              Apply Filters
            </button>
          </div>
        </div>
      </div>

      {/* Detections Table */}
      <div className="card overflow-hidden">
        <table>
          <thead>
            <tr>
              <th>User</th>
              <th>Platform</th>
              <th>Confidence</th>
              <th>Recommended Action</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan="6" className="p-12 text-center">
                  <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400 mx-auto"></div>
                </td>
              </tr>
            ) : detections.length === 0 ? (
              <tr>
                <td colSpan="6" className="p-12 text-center text-navy-400">
                  No bot detections found
                </td>
              </tr>
            ) : (
              detections.map((detection) => {
                const action = RECOMMENDED_ACTIONS.find(a => a.id === detection.recommendedAction) || RECOMMENDED_ACTIONS[0];
                return (
                  <tr key={detection.id}>
                    <td>
                      <div className="flex items-center space-x-3">
                        {detection.avatarUrl ? (
                          <img src={detection.avatarUrl} alt="" className="w-8 h-8 rounded-full" />
                        ) : (
                          <div className="w-8 h-8 rounded-full bg-navy-700 flex items-center justify-center text-sm">
                            {detection.username?.[0]?.toUpperCase() || '?'}
                          </div>
                        )}
                        <div>
                          <div className="font-medium text-sky-100">{detection.username || 'Unknown'}</div>
                          <div className="text-xs text-navy-500">ID: {detection.userId}</div>
                        </div>
                      </div>
                    </td>
                    <td>
                      <span className="capitalize text-sm">{detection.platform}</span>
                    </td>
                    <td>
                      <div className={`font-bold ${getConfidenceColor(detection.confidenceScore)}`}>
                        {detection.confidenceScore}%
                      </div>
                      <div className="text-xs text-navy-500 capitalize">
                        {getConfidenceLevel(detection.confidenceScore)}
                      </div>
                    </td>
                    <td>
                      <span className={`badge ${action.color}`}>
                        {action.name}
                      </span>
                    </td>
                    <td>
                      {detection.reviewed ? (
                        <div>
                          <span className="badge badge-green text-xs">Reviewed</span>
                          {detection.reviewedAt && (
                            <div className="text-xs text-navy-500 mt-1">
                              {new Date(detection.reviewedAt).toLocaleDateString()}
                            </div>
                          )}
                        </div>
                      ) : (
                        <span className="badge bg-yellow-500/20 text-yellow-300 border-yellow-500/30 text-xs">
                          Pending
                        </span>
                      )}
                    </td>
                    <td>
                      <div className="flex gap-2">
                        <button
                          onClick={() => openReviewModal(detection)}
                          className="btn btn-secondary text-xs py-1 px-3"
                        >
                          Review
                        </button>
                        {!detection.reviewed && (
                          <button
                            onClick={() => markAsReviewed(detection.id)}
                            className="btn btn-outline text-xs py-1 px-3"
                          >
                            Mark Reviewed
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>

        {pagination && pagination.totalPages > 1 && (
          <div className="flex justify-between items-center p-4 border-t border-navy-700">
            <span className="text-sm text-navy-400">
              {pagination.total} total detections
            </span>
            <div className="flex space-x-2">
              <button
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page === 1}
                className="btn btn-secondary text-sm disabled:opacity-50"
              >
                Previous
              </button>
              <button
                onClick={() => setPage(p => Math.min(pagination.totalPages, p + 1))}
                disabled={page === pagination.totalPages}
                className="btn btn-secondary text-sm disabled:opacity-50"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Review Modal */}
      <FormModalBuilder
        title="Review Bot Detection"
        fields={reviewFields}
        isOpen={showReviewModal && selectedDetection !== null}
        onClose={closeReviewModal}
        onSubmit={submitReview}
        submitButtonText="Submit Review"
        cancelButtonText="Cancel"
        width="lg"
        themeMode="dark"
        colors={WADDLES_COLORS}
        headerContent={selectedDetection && (
          <>
            {/* User Details */}
            <div className="mb-6 p-4 bg-navy-800 rounded-lg">
              <div className="flex items-center gap-4 mb-4">
                {selectedDetection.avatarUrl ? (
                  <img src={selectedDetection.avatarUrl} alt="" className="w-16 h-16 rounded-full" />
                ) : (
                  <div className="w-16 h-16 rounded-full bg-navy-700 flex items-center justify-center text-2xl">
                    {selectedDetection.username?.[0]?.toUpperCase() || '?'}
                  </div>
                )}
                <div>
                  <div className="text-lg font-semibold text-sky-100">{selectedDetection.username}</div>
                  <div className="text-sm text-navy-400">
                    {selectedDetection.platform} · User ID: {selectedDetection.userId}
                  </div>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <div className="text-xs text-navy-500 mb-1">Confidence Score</div>
                  <div className={`text-2xl font-bold ${getConfidenceColor(selectedDetection.confidenceScore)}`}>
                    {selectedDetection.confidenceScore}%
                  </div>
                </div>
                <div>
                  <div className="text-xs text-navy-500 mb-1">Detected</div>
                  <div className="text-sm text-sky-100">
                    {new Date(selectedDetection.detectedAt).toLocaleString()}
                  </div>
                </div>
              </div>
            </div>

            {/* Detection Signals */}
            {isPremium && selectedDetection.signals ? (
              <div className="mb-6">
                <h3 className="text-sm font-semibold text-sky-100 mb-3">Detection Signals</h3>
                <div className="space-y-2">
                  {Object.entries(selectedDetection.signals).map(([key, value]) => (
                    <div key={key} className="flex justify-between p-3 bg-navy-800 rounded-lg">
                      <span className="text-sm text-navy-300 capitalize">
                        {key.replace(/_/g, ' ')}
                      </span>
                      <span className="text-sm font-medium text-sky-100">
                        {typeof value === 'boolean' ? (value ? 'Yes' : 'No') : value}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="mb-6 p-4 bg-navy-800 rounded-lg">
                <div className="text-sm text-navy-400">
                  Detection signals summary available for premium users
                </div>
              </div>
            )}
          </>
        )}
      />
    </div>
  );
}

export default AdminBotDetection;
