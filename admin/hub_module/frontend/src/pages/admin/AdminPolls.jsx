import { useState, useEffect, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import {
  ChartBarIcon,
  PlusIcon,
  TrashIcon,
  EyeIcon,
  ClockIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import { WADDLES_COLORS } from '../../theme/waddlebotTheme';

function AdminPolls() {
  const { communityId } = useParams();
  const [polls, setPolls] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [selectedPoll, setSelectedPoll] = useState(null);

  useEffect(() => {
    loadPolls();
  }, [communityId]);

  const loadPolls = async () => {
    try {
      setLoading(true);
      const response = await adminApi.getPolls(communityId);
      setPolls(response.data?.polls || []);
    } catch (err) {
      setError('Failed to load polls');
    } finally {
      setLoading(false);
    }
  };

  const createPoll = async (data) => {
    // options comes as string[] from multiline field (split by newline, trimmed, filtered)
    const validOptions = data.options || [];
    if (validOptions.length < 2) {
      setError('At least 2 options required');
      throw new Error('At least 2 options required');
    }
    try {
      await adminApi.createPoll(communityId, {
        title: data.title?.trim(),
        description: data.description?.trim() || '',
        options: validOptions,
        view_visibility: data.view_visibility,
        submit_visibility: data.submit_visibility,
        allow_multiple_choices: data.allow_multiple_choices || false,
        max_choices: data.allow_multiple_choices ? null : 1,
        expires_at: data.expires_at || null,
      });
      setMessage({ type: 'success', text: 'Poll created' });
      setShowCreateModal(false);
      loadPolls();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to create poll');
      throw err;
    }
  };

  const deletePoll = async (pollId) => {
    if (!window.confirm('Delete this poll?')) return;
    try {
      await adminApi.deletePoll(communityId, pollId);
      setMessage({ type: 'success', text: 'Poll deleted' });
      loadPolls();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to delete poll');
    }
  };

  const loadPollDetails = async (pollId) => {
    try {
      const response = await adminApi.getPoll(communityId, pollId);
      setSelectedPoll(response.data?.poll);
    } catch (err) {
      setError('Failed to load poll details');
    }
  };

  // Build fields for FormModalBuilder
  const pollFields = useMemo(() => [
    {
      name: 'title',
      type: 'text',
      label: 'Title',
      required: true,
      placeholder: "What's your favorite...?",
    },
    {
      name: 'description',
      type: 'textarea',
      label: 'Description (optional)',
      placeholder: 'Add more context...',
      rows: 3,
    },
    {
      name: 'options',
      type: 'multiline',
      label: 'Options',
      required: true,
      placeholder: 'Enter one option per line (minimum 2)',
      rows: 4,
      helpText: 'Enter one option per line. At least 2 options required.',
    },
    {
      name: 'view_visibility',
      type: 'select',
      label: 'Who can view',
      defaultValue: 'community',
      options: [
        { value: 'public', label: 'Public' },
        { value: 'registered', label: 'Registered Users' },
        { value: 'community', label: 'Community Members' },
        { value: 'admins', label: 'Admins Only' },
      ],
    },
    {
      name: 'submit_visibility',
      type: 'select',
      label: 'Who can vote',
      defaultValue: 'community',
      options: [
        { value: 'public', label: 'Public' },
        { value: 'registered', label: 'Registered Users' },
        { value: 'community', label: 'Community Members' },
        { value: 'admins', label: 'Admins Only' },
      ],
    },
    {
      name: 'allow_multiple_choices',
      type: 'checkbox',
      label: 'Allow multiple choices',
      defaultValue: false,
    },
    {
      name: 'expires_at',
      type: 'datetime-local',
      label: 'Expires at (optional)',
    },
  ], []);

  const getTotalVotes = (poll) => {
    if (!poll.vote_counts) return 0;
    return Object.values(poll.vote_counts).reduce((sum, count) => sum + count, 0);
  };

  if (loading) {
    return (
      <div className="flex justify-center items-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-sky-500"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white flex items-center gap-2">
          <ChartBarIcon className="h-8 w-8 text-sky-500" />
          Community Polls
        </h1>
        <button onClick={() => setShowCreateModal(true)} className="btn btn-primary">
          <PlusIcon className="h-5 w-5 mr-2" />
          Create Poll
        </button>
      </div>

      {error && (
        <div className="bg-red-500/20 border border-red-500 text-red-300 px-4 py-3 rounded">
          {error}
          <button onClick={() => setError(null)} className="float-right">&times;</button>
        </div>
      )}

      {message && (
        <div className={`px-4 py-3 rounded ${message.type === 'success' ? 'bg-green-500/20 text-green-300' : 'bg-red-500/20 text-red-300'}`}>
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right">&times;</button>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Poll List */}
        <div className="space-y-4">
          {polls.length === 0 ? (
            <div className="card p-8 text-center">
              <ChartBarIcon className="h-16 w-16 text-gray-500 mx-auto mb-4" />
              <p className="text-gray-400">No polls yet. Create your first poll!</p>
            </div>
          ) : (
            polls.map((poll) => (
              <div
                key={poll.id}
                data-click-delegate
                className={`card p-4 cursor-pointer hover:ring-2 hover:ring-sky-500 transition ${
                  selectedPoll?.id === poll.id ? 'ring-2 ring-sky-500' : ''
                }`}
                onClick={() => loadPollDetails(poll.id)}
              >
                <div className="flex justify-between items-start">
                  <div>
                    <h3 className="text-white font-medium"><button type="button" className="text-left hover:underline focus-visible:underline">{poll.title}</button></h3>
                    {poll.description && (
                      <p className="text-gray-400 text-sm mt-1 line-clamp-2">{poll.description}</p>
                    )}
                    <div className="flex items-center gap-4 mt-2 text-xs text-gray-500">
                      <span className="flex items-center gap-1">
                        <EyeIcon className="h-3 w-3" />
                        {poll.view_visibility}
                      </span>
                      {poll.expires_at && (
                        <span className="flex items-center gap-1">
                          <ClockIcon className="h-3 w-3" />
                          {new Date(poll.expires_at).toLocaleDateString()}
                        </span>
                      )}
                    </div>
                  </div>
                  <button
                    aria-label={`Delete poll ${poll.title}`}
                    onClick={(e) => { e.stopPropagation(); deletePoll(poll.id); }}
                    className="btn btn-sm btn-danger"
                  >
                    <TrashIcon className="h-4 w-4" />
                  </button>
                </div>
              </div>
            ))
          )}
        </div>

        {/* Poll Results */}
        <div className="card p-6">
          {selectedPoll ? (
            <>
              <h2 className="text-lg font-semibold text-white mb-2">{selectedPoll.title}</h2>
              {selectedPoll.description && (
                <p className="text-gray-400 text-sm mb-4">{selectedPoll.description}</p>
              )}
              <p className="text-gray-500 text-sm mb-4">
                Total votes: {getTotalVotes(selectedPoll)}
              </p>
              <div className="space-y-3">
                {selectedPoll.options?.map((opt) => {
                  const votes = selectedPoll.vote_counts?.[opt.id] || 0;
                  const total = getTotalVotes(selectedPoll);
                  const percent = total > 0 ? Math.round((votes / total) * 100) : 0;
                  return (
                    <div key={opt.id}>
                      <div className="flex justify-between text-sm mb-1">
                        <span className="text-white">{opt.text}</span>
                        <span className="text-gray-400">{votes} ({percent}%)</span>
                      </div>
                      <div className="h-2 bg-gray-700 rounded-full overflow-hidden">
                        <div
                          className="h-full bg-sky-500 transition-all"
                          style={{ width: `${percent}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            </>
          ) : (
            <p className="text-gray-400 text-center py-8">Select a poll to view results</p>
          )}
        </div>
      </div>

      {/* Create Poll Modal */}
      <FormModalBuilder
        title="Create Poll"
        fields={pollFields}
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSubmit={createPoll}
        submitButtonText="Create Poll"
        cancelButtonText="Cancel"
        width="lg"
        themeMode="dark"
        colors={WADDLES_COLORS}
      />
    </div>
  );
}

export default AdminPolls;
