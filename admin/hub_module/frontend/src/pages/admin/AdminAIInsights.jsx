import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { onActivate } from '../../components/ui/a11y';

const INSIGHT_TYPES = [
  { id: 'all', name: 'All Insights', icon: '🔍', color: 'bg-sky-500/20 text-sky-300 border-sky-500/30' },
  { id: 'stream_summary', name: 'Stream Summaries', icon: '📊', color: 'bg-purple-500/20 text-purple-300 border-purple-500/30' },
  { id: 'weekly_rollup', name: 'Weekly Rollups', icon: '📅', color: 'bg-blue-500/20 text-blue-300 border-blue-500/30' },
  { id: 'bot_detection', name: 'Bot Detection', icon: '🤖', color: 'bg-red-500/20 text-red-300 border-red-500/30' },
];

function AdminAIInsights() {
  const { communityId } = useParams();
  const [insights, setInsights] = useState([]);
  const [filteredInsights, setFilteredInsights] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedType, setSelectedType] = useState('all');
  const [selectedInsight, setSelectedInsight] = useState(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [message, setMessage] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const insightsPerPage = 10;

  useEffect(() => {
    fetchInsights();
  }, [communityId]);

  useEffect(() => {
    filterInsights();
  }, [insights, selectedType, currentPage]);

  // Escape closes the detail dialog (keyboard users have no backdrop to click).
  useEffect(() => {
    if (!selectedInsight) return undefined;
    const onKeyDown = (e) => {
      if (e.key === 'Escape') closeModal();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [selectedInsight]);

  async function fetchInsights() {
    setLoading(true);
    try {
      const response = await adminApi.getAIInsights(communityId);
      if (response.data.success) {
        setInsights(response.data.insights || []);
      }
    } catch (err) {
      console.error('Failed to fetch insights:', err);
      setMessage({ type: 'error', text: 'Failed to load AI insights' });
    } finally {
      setLoading(false);
    }
  }

  function filterInsights() {
    let filtered = insights;
    if (selectedType !== 'all') {
      filtered = insights.filter(insight => insight.type === selectedType);
    }
    setFilteredInsights(filtered);
  }

  function getInsightTypeConfig(type) {
    return INSIGHT_TYPES.find(t => t.id === type) || INSIGHT_TYPES[0];
  }

  function truncateText(text, maxLength = 150) {
    if (!text || text.length <= maxLength) return text;
    return text.substring(0, maxLength) + '...';
  }

  function formatDate(dateString) {
    const date = new Date(dateString);
    return date.toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    });
  }

  async function handleViewDetails(insight) {
    setSelectedInsight(insight); // show modal immediately with list data
    setDetailLoading(true);
    try {
      const res = await adminApi.getAIInsight(communityId, insight.id);
      if (res.data?.insight) {
        setSelectedInsight(res.data.insight);
      }
    } catch (err) {
      console.error('Failed to fetch insight detail:', err);
      // keep showing list data if detail fetch fails
    } finally {
      setDetailLoading(false);
    }
  }

  function closeModal() {
    setSelectedInsight(null);
  }

  // Pagination
  const indexOfLastInsight = currentPage * insightsPerPage;
  const indexOfFirstInsight = indexOfLastInsight - insightsPerPage;
  const currentInsights = filteredInsights.slice(indexOfFirstInsight, indexOfLastInsight);
  const totalPages = Math.ceil(filteredInsights.length / insightsPerPage);

  function goToPage(pageNumber) {
    setCurrentPage(pageNumber);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-sky-100">AI Insights</h1>
          <p className="text-navy-400 mt-1">AI-generated summaries, analytics, and detections</p>
        </div>
        <button
          onClick={fetchInsights}
          className="btn btn-secondary"
        >
          <svg className="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          Refresh
        </button>
      </div>

      {message && (
        <div className={`mb-6 p-4 rounded-lg border ${
          message.type === 'success'
            ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
            : 'bg-red-500/20 text-red-300 border-red-500/30'
        }`}>
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right">×</button>
        </div>
      )}

      {/* Filter Tabs */}
      <div className="card p-6 mb-6">
        <div className="flex flex-wrap gap-3">
          {INSIGHT_TYPES.map((type) => (
            <button
              key={type.id}
              onClick={() => {
                setSelectedType(type.id);
                setCurrentPage(1);
              }}
              className={`px-4 py-2 rounded-lg border transition-all ${
                selectedType === type.id
                  ? type.color
                  : 'bg-navy-800 text-navy-400 border-navy-700 hover:border-navy-500'
              }`}
            >
              <span className="mr-2">{type.icon}</span>
              <span className="font-medium">{type.name}</span>
              {selectedType === type.id && (
                <span className="ml-2 px-2 py-0.5 bg-white/10 rounded text-xs">
                  {filteredInsights.length}
                </span>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* Insights List */}
      {currentInsights.length === 0 ? (
        <div className="card p-12 text-center">
          <div className="text-4xl mb-4">🔍</div>
          <h3 className="text-xl font-semibold text-sky-100 mb-2">No Insights Found</h3>
          <p className="text-navy-400">
            {selectedType === 'all'
              ? 'No AI insights have been generated yet.'
              : `No ${getInsightTypeConfig(selectedType).name.toLowerCase()} insights found.`}
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {currentInsights.map((insight) => {
            const typeConfig = getInsightTypeConfig(insight.type);
            return (
              <div role="button" tabIndex={0} onKeyDown={onActivate(() => handleViewDetails(insight))} key={insight.id} className="card p-6 hover:border-sky-500/50 transition-all cursor-pointer"
                onClick={() => handleViewDetails(insight)}>
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-3 mb-2">
                      <span className={`px-3 py-1 rounded-full text-sm font-medium border ${typeConfig.color}`}>
                        {typeConfig.icon} {typeConfig.name}
                      </span>
                      <span className="text-sm text-navy-400">
                        {formatDate(insight.created_at)}
                      </span>
                    </div>
                    <h3 className="text-lg font-semibold text-sky-100 mb-2">
                      {insight.title || 'Untitled Insight'}
                    </h3>
                    <p className="text-navy-300">
                      {truncateText(insight.content)}
                    </p>
                    {insight.metadata && (
                      <div className="flex flex-wrap gap-2 mt-3">
                        {insight.metadata.sentiment && (
                          <span className="px-2 py-1 bg-navy-800 rounded text-xs text-navy-300">
                            Sentiment: {insight.metadata.sentiment}
                          </span>
                        )}
                        {insight.metadata.word_count && (
                          <span className="px-2 py-1 bg-navy-800 rounded text-xs text-navy-300">
                            {insight.metadata.word_count} words
                          </span>
                        )}
                        {insight.metadata.confidence && (
                          <span className="px-2 py-1 bg-navy-800 rounded text-xs text-navy-300">
                            Confidence: {Math.round(insight.metadata.confidence * 100)}%
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                  <svg className="w-5 h-5 text-navy-400 ml-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                  </svg>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex justify-center items-center gap-2 mt-8">
          <button
            onClick={() => goToPage(currentPage - 1)} aria-label="Previous page"
            disabled={currentPage === 1}
            className="btn btn-secondary disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
          </button>

          <div className="flex gap-2">
            {[...Array(totalPages)].map((_, idx) => {
              const pageNum = idx + 1;
              // Show first, last, current, and adjacent pages
              if (
                pageNum === 1 ||
                pageNum === totalPages ||
                (pageNum >= currentPage - 1 && pageNum <= currentPage + 1)
              ) {
                return (
                  <button
                    key={pageNum}
                    onClick={() => goToPage(pageNum)}
                    className={`px-4 py-2 rounded-lg font-medium transition-all ${
                      currentPage === pageNum
                        ? 'bg-sky-500 text-white'
                        : 'bg-navy-800 text-navy-300 hover:bg-navy-700'
                    }`}
                  >
                    {pageNum}
                  </button>
                );
              } else if (
                pageNum === currentPage - 2 ||
                pageNum === currentPage + 2
              ) {
                return <span key={pageNum} className="px-2 text-navy-500">...</span>;
              }
              return null;
            })}
          </div>

          <button
            onClick={() => goToPage(currentPage + 1)} aria-label="Next page"
            disabled={currentPage === totalPages}
            className="btn btn-secondary disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
            </svg>
          </button>
        </div>
      )}

      {/* Detail Modal */}
      {selectedInsight && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
          role="presentation"
          onClick={closeModal}>
          <div className="bg-navy-900 rounded-lg border border-navy-700 max-w-4xl w-full max-h-[90vh] overflow-y-auto"
            role="dialog" aria-modal="true" aria-labelledby="insight-detail-title" tabIndex={-1}
            onClick={(e) => e.stopPropagation()}>
            <div className="sticky top-0 bg-navy-900 border-b border-navy-700 p-6 flex items-center justify-between">
              <div>
                <div className="flex items-center gap-3 mb-2">
                  <span className={`px-3 py-1 rounded-full text-sm font-medium border ${
                    getInsightTypeConfig(selectedInsight.type).color
                  }`}>
                    {getInsightTypeConfig(selectedInsight.type).icon}{' '}
                    {getInsightTypeConfig(selectedInsight.type).name}
                  </span>
                  <span className="text-sm text-navy-400">
                    {formatDate(selectedInsight.created_at)}
                  </span>
                </div>
                <h2 id="insight-detail-title" className="text-2xl font-bold text-sky-100">
                  {selectedInsight.title || 'Untitled Insight'}
                </h2>
              </div>
              <button
                aria-label="Close insight details"
                onClick={closeModal}
                className="text-navy-400 hover:text-sky-100 transition-colors"
              >
                <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            <div className="p-6">
              {detailLoading && (
                <div className="flex items-center gap-2 text-navy-400 text-sm mb-4">
                  <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-gold-400" />
                  Loading full detail...
                </div>
              )}

              {/* Content */}
              <div className="mb-6">
                <h3 className="text-lg font-semibold text-sky-100 mb-3">Content</h3>
                <div className="bg-navy-800 rounded-lg p-4 text-navy-200 whitespace-pre-wrap">
                  {selectedInsight.content}
                </div>
              </div>

              {/* Metadata */}
              {selectedInsight.metadata && (
                <div className="mb-6">
                  <h3 className="text-lg font-semibold text-sky-100 mb-3">Metadata</h3>
                  <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
                    {selectedInsight.metadata.sentiment && (
                      <div className="bg-navy-800 rounded-lg p-4">
                        <div className="text-sm text-navy-400 mb-1">Sentiment</div>
                        <div className="text-lg font-semibold text-sky-100 capitalize">
                          {selectedInsight.metadata.sentiment}
                        </div>
                      </div>
                    )}
                    {selectedInsight.metadata.confidence && (
                      <div className="bg-navy-800 rounded-lg p-4">
                        <div className="text-sm text-navy-400 mb-1">Confidence</div>
                        <div className="text-lg font-semibold text-sky-100">
                          {Math.round(selectedInsight.metadata.confidence * 100)}%
                        </div>
                      </div>
                    )}
                    {selectedInsight.metadata.word_count && (
                      <div className="bg-navy-800 rounded-lg p-4">
                        <div className="text-sm text-navy-400 mb-1">Word Count</div>
                        <div className="text-lg font-semibold text-sky-100">
                          {selectedInsight.metadata.word_count.toLocaleString()}
                        </div>
                      </div>
                    )}
                    {selectedInsight.metadata.duration && (
                      <div className="bg-navy-800 rounded-lg p-4">
                        <div className="text-sm text-navy-400 mb-1">Duration</div>
                        <div className="text-lg font-semibold text-sky-100">
                          {selectedInsight.metadata.duration}
                        </div>
                      </div>
                    )}
                    {selectedInsight.metadata.participants && (
                      <div className="bg-navy-800 rounded-lg p-4">
                        <div className="text-sm text-navy-400 mb-1">Participants</div>
                        <div className="text-lg font-semibold text-sky-100">
                          {selectedInsight.metadata.participants}
                        </div>
                      </div>
                    )}
                    {selectedInsight.metadata.platform && (
                      <div className="bg-navy-800 rounded-lg p-4">
                        <div className="text-sm text-navy-400 mb-1">Platform</div>
                        <div className="text-lg font-semibold text-sky-100 capitalize">
                          {selectedInsight.metadata.platform}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* Word Cloud Data */}
              {selectedInsight.metadata?.word_cloud && (
                <div className="mb-6">
                  <h3 className="text-lg font-semibold text-sky-100 mb-3">Top Keywords</h3>
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(selectedInsight.metadata.word_cloud)
                      .sort((a, b) => b[1] - a[1])
                      .slice(0, 20)
                      .map(([word, count]) => (
                        <span
                          key={word}
                          className="px-3 py-1 bg-sky-500/20 text-sky-300 rounded-full text-sm"
                          style={{
                            fontSize: `${Math.min(14 + count / 2, 20)}px`
                          }}
                        >
                          {word} ({count})
                        </span>
                      ))}
                  </div>
                </div>
              )}

              {/* Bot Detection Actions */}
              {selectedInsight.type === 'bot_detection' && selectedInsight.metadata?.suspected_users && (
                <div className="mb-6">
                  <h3 className="text-lg font-semibold text-sky-100 mb-3">Suspected Bot Accounts</h3>
                  <div className="space-y-2">
                    {selectedInsight.metadata.suspected_users.map((user, idx) => (
                      <div key={idx} className="flex items-center justify-between bg-navy-800 rounded-lg p-4">
                        <div>
                          <div className="font-medium text-sky-100">{user.username}</div>
                          <div className="text-sm text-navy-400">
                            Confidence: {Math.round(user.confidence * 100)}%
                          </div>
                        </div>
                        <button className="btn btn-primary text-sm">
                          Review
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Action Buttons */}
              <div className="flex justify-end gap-3 pt-4 border-t border-navy-700">
                <button onClick={closeModal} className="btn btn-secondary">
                  Close
                </button>
                {selectedInsight.type === 'stream_summary' && (
                  <button className="btn btn-primary">
                    Export Summary
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default AdminAIInsights;
