import { useState, useEffect, useCallback } from 'react';
import {
  ChatBubbleLeftRightIcon,
  LockClosedIcon,
  MapPinIcon,
  PlusIcon,
  ArrowLeftIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline';
import { interactionApi } from '../../services/api';

function relativeTime(dateStr) {
  if (!dateStr) return '';
  const diff = Date.now() - new Date(dateStr).getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(dateStr).toLocaleDateString();
}

export default function ForumView({ channel }) {
  const { id: channelId, name: channelName, communityId } = channel;

  const [posts, setPosts] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [selectedPost, setSelectedPost] = useState(null);
  const [loading, setLoading] = useState(false);
  const [postLoading, setPostLoading] = useState(false);
  const [error, setError] = useState(null);

  const [showNewPostForm, setShowNewPostForm] = useState(false);
  const [newPostTitle, setNewPostTitle] = useState('');
  const [newPostBody, setNewPostBody] = useState('');
  const [submittingPost, setSubmittingPost] = useState(false);

  const [replyText, setReplyText] = useState('');
  const [submittingReply, setSubmittingReply] = useState(false);

  const fetchPosts = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await interactionApi.getForumPosts(communityId, channelId, {});
      setPosts(res.data.posts || []);
      setPagination(res.data.pagination || null);
    } catch (err) {
      setError('Failed to load posts.');
    } finally {
      setLoading(false);
    }
  }, [communityId, channelId]);

  const fetchPost = useCallback(async (postId) => {
    setPostLoading(true);
    setError(null);
    try {
      const res = await interactionApi.getForumPost(communityId, channelId, postId);
      setSelectedPost(res.data.post);
    } catch (err) {
      setError('Failed to load post.');
    } finally {
      setPostLoading(false);
    }
  }, [communityId, channelId]);

  useEffect(() => {
    fetchPosts();
  }, [fetchPosts]);

  const handleSelectPost = (post) => {
    fetchPost(post.id);
  };

  const handleBack = () => {
    setSelectedPost(null);
    setReplyText('');
    setError(null);
  };

  const handleCreatePost = async (e) => {
    e.preventDefault();
    if (!newPostTitle.trim() || !newPostBody.trim()) return;
    setSubmittingPost(true);
    setError(null);
    try {
      await interactionApi.createForumPost(communityId, channelId, {
        title: newPostTitle.trim(),
        body: newPostBody.trim(),
      });
      setNewPostTitle('');
      setNewPostBody('');
      setShowNewPostForm(false);
      await fetchPosts();
    } catch (err) {
      setError('Failed to create post.');
    } finally {
      setSubmittingPost(false);
    }
  };

  const handleCreateReply = async (e) => {
    e.preventDefault();
    if (!replyText.trim() || !selectedPost) return;
    setSubmittingReply(true);
    setError(null);
    try {
      await interactionApi.createForumReply(communityId, selectedPost.id, {
        body: replyText.trim(),
      });
      setReplyText('');
      await fetchPost(selectedPost.id);
    } catch (err) {
      setError('Failed to submit reply.');
    } finally {
      setSubmittingReply(false);
    }
  };

  if (selectedPost) {
    return (
      <div className="flex flex-col h-full">
        <div className="flex items-center gap-3 mb-4">
          <button
            onClick={handleBack}
            className="flex items-center gap-1 text-navy-400 hover:text-navy-200 text-sm"
          >
            <ArrowLeftIcon className="w-4 h-4" />
            Back to posts
          </button>
        </div>

        {error && (
          <div className="mb-4 p-3 bg-red-900/30 border border-red-700 rounded-lg text-red-300 text-sm">
            {error}
          </div>
        )}

        {postLoading ? (
          <div className="flex items-center justify-center py-12">
            <ArrowPathIcon className="w-6 h-6 text-navy-400 animate-spin" />
          </div>
        ) : (
          <div className="flex flex-col gap-4 overflow-y-auto flex-1">
            <div className="bg-navy-800 border border-navy-700 rounded-lg p-5">
              <div className="flex items-start justify-between gap-3 mb-2">
                <h2 className="text-sky-100 font-semibold text-lg leading-tight">
                  {selectedPost.title}
                </h2>
                <div className="flex items-center gap-2 flex-shrink-0">
                  {selectedPost.pinned && (
                    <span className="bg-gold-500/20 text-gold-400 text-xs px-2 py-0.5 rounded flex items-center gap-1">
                      <MapPinIcon className="w-3 h-3" />
                      Pinned
                    </span>
                  )}
                  {selectedPost.locked && (
                    <span className="bg-red-500/20 text-red-400 text-xs px-2 py-0.5 rounded flex items-center gap-1">
                      <LockClosedIcon className="w-3 h-3" />
                      Locked
                    </span>
                  )}
                </div>
              </div>

              <div className="flex items-center gap-2 mb-3 text-sm">
                <span className="text-sky-100">{selectedPost.author?.name || selectedPost.authorName || 'Unknown'}</span>
                {selectedPost.author?.platform && selectedPost.author.platform !== 'hub' && (
                  <span className="text-xs text-navy-400">via {selectedPost.author.platform}</span>
                )}
                <span className="text-navy-500">·</span>
                <span className="text-navy-400 text-xs">{relativeTime(selectedPost.createdAt)}</span>
              </div>

              {selectedPost.tags && selectedPost.tags.length > 0 && (
                <div className="flex flex-wrap gap-1 mb-3">
                  {selectedPost.tags.map((tag) => (
                    <span key={tag} className="bg-navy-700 text-navy-300 text-xs px-2 py-0.5 rounded">
                      {tag}
                    </span>
                  ))}
                </div>
              )}

              <p className="text-navy-200 text-sm leading-relaxed whitespace-pre-wrap">
                {selectedPost.body}
              </p>
            </div>

            <div>
              <h3 className="text-navy-300 text-sm font-medium mb-3 flex items-center gap-2">
                <ChatBubbleLeftRightIcon className="w-4 h-4" />
                {selectedPost.replies?.length || 0} {selectedPost.replies?.length === 1 ? 'Reply' : 'Replies'}
              </h3>

              {selectedPost.replies && selectedPost.replies.length > 0 ? (
                <div className="flex flex-col gap-3">
                  {selectedPost.replies.map((reply) => (
                    <div
                      key={reply.id}
                      className="bg-navy-850 border-l-2 border-navy-600 pl-4 py-3"
                    >
                      <div className="flex items-center gap-2 mb-1 text-sm">
                        <span className="text-sky-100">{reply.author?.name || reply.authorName || 'Unknown'}</span>
                        {reply.author?.platform && reply.author.platform !== 'hub' && (
                          <span className="text-xs text-navy-400">via {reply.author.platform}</span>
                        )}
                        <span className="text-navy-500">·</span>
                        <span className="text-navy-400 text-xs">{relativeTime(reply.createdAt)}</span>
                      </div>
                      <p className="text-navy-200 text-sm leading-relaxed whitespace-pre-wrap">
                        {reply.body}
                      </p>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-navy-500 text-sm italic">No replies yet.</p>
              )}
            </div>

            {!selectedPost.locked && (
              <form onSubmit={handleCreateReply} className="mt-2 flex flex-col gap-2">
                <textarea
                  aria-label="Write a reply"
                  value={replyText}
                  onChange={(e) => setReplyText(e.target.value)}
                  placeholder="Write a reply..."
                  rows={3}
                  className="bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 text-sm resize-none focus:border-gold-500 placeholder-navy-500"
                />
                <div className="flex items-center gap-3">
                  <button
                    type="submit"
                    disabled={submittingReply || !replyText.trim()}
                    className="bg-gold-500 text-navy-900 hover:bg-gold-400 rounded-lg px-4 py-2 font-medium text-sm disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    {submittingReply ? 'Posting...' : 'Post Reply'}
                  </button>
                </div>
              </form>
            )}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <ChatBubbleLeftRightIcon className="w-5 h-5 text-gold-400" />
          <h2 className="text-sky-100 font-semibold">{channelName}</h2>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={fetchPosts}
            disabled={loading}
            className="text-navy-400 hover:text-navy-200 p-1 rounded transition-colors"
            title="Refresh"
          >
            <ArrowPathIcon className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
          {!showNewPostForm && (
            <button
              onClick={() => setShowNewPostForm(true)}
              className="bg-gold-500 text-navy-900 hover:bg-gold-400 rounded-lg px-3 py-1.5 font-medium text-sm flex items-center gap-1 transition-colors"
            >
              <PlusIcon className="w-4 h-4" />
              New Post
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-900/30 border border-red-700 rounded-lg text-red-300 text-sm">
          {error}
        </div>
      )}

      {showNewPostForm && (
        <form
          onSubmit={handleCreatePost}
          className="mb-4 bg-navy-800 border border-navy-700 rounded-lg p-4 flex flex-col gap-3"
        >
          <h3 className="text-sky-100 font-medium text-sm">New Post</h3>
          <input
            aria-label="Post title"
            type="text"
            value={newPostTitle}
            onChange={(e) => setNewPostTitle(e.target.value)}
            placeholder="Post title"
            className="bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 text-sm focus:border-gold-500 placeholder-navy-500"
          />
          <textarea
            aria-label="Write your post"
            value={newPostBody}
            onChange={(e) => setNewPostBody(e.target.value)}
            placeholder="Write your post..."
            rows={4}
            className="bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 text-sm resize-none focus:border-gold-500 placeholder-navy-500"
          />
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={submittingPost || !newPostTitle.trim() || !newPostBody.trim()}
              className="bg-gold-500 text-navy-900 hover:bg-gold-400 rounded-lg px-4 py-2 font-medium text-sm disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {submittingPost ? 'Creating...' : 'Create Post'}
            </button>
            <button
              type="button"
              onClick={() => {
                setShowNewPostForm(false);
                setNewPostTitle('');
                setNewPostBody('');
              }}
              className="text-navy-400 hover:text-navy-200 text-sm transition-colors"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-12">
          <ArrowPathIcon className="w-6 h-6 text-navy-400 animate-spin" />
        </div>
      ) : posts.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-navy-500">
          <ChatBubbleLeftRightIcon className="w-10 h-10 mb-2" />
          <p className="text-sm">No posts yet. Start the conversation!</p>
        </div>
      ) : (
        <div className="flex flex-col gap-3 overflow-y-auto flex-1">
          {posts.map((post) => (
            <button
              key={post.id}
              onClick={() => handleSelectPost(post)}
              className="bg-navy-800 border border-navy-700 rounded-lg p-4 hover:border-navy-600 cursor-pointer text-left transition-colors w-full"
            >
              <div className="flex items-start justify-between gap-3 mb-1">
                <span className="text-sky-100 font-medium text-sm leading-snug">
                  {post.title}
                </span>
                <div className="flex items-center gap-1.5 flex-shrink-0">
                  {post.pinned && (
                    <span className="bg-gold-500/20 text-gold-400 text-xs px-2 py-0.5 rounded flex items-center gap-1">
                      <MapPinIcon className="w-3 h-3" />
                      Pinned
                    </span>
                  )}
                  {post.locked && (
                    <span className="bg-red-500/20 text-red-400 text-xs px-2 py-0.5 rounded flex items-center gap-1">
                      <LockClosedIcon className="w-3 h-3" />
                      Locked
                    </span>
                  )}
                </div>
              </div>

              <div className="flex items-center gap-2 mb-2 text-xs">
                <span className="text-navy-300">{post.author?.name || post.authorName || 'Unknown'}</span>
                {post.author?.platform && post.author.platform !== 'hub' && (
                  <span className="text-xs text-navy-400">via {post.author.platform}</span>
                )}
                <span className="text-navy-600">·</span>
                <span className="text-navy-400">{relativeTime(post.createdAt)}</span>
                <span className="text-navy-600">·</span>
                <span className="text-navy-400 text-sm flex items-center gap-1">
                  <ChatBubbleLeftRightIcon className="w-3.5 h-3.5" />
                  {post.replyCount ?? post.replies?.length ?? 0}
                </span>
              </div>

              {post.tags && post.tags.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {post.tags.map((tag) => (
                    <span key={tag} className="bg-navy-700 text-navy-300 text-xs px-2 py-0.5 rounded">
                      {tag}
                    </span>
                  ))}
                </div>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
