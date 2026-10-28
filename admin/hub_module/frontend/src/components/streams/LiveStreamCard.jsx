import { useState } from 'react';
import PropTypes from 'prop-types';
import TwitchEmbed from './TwitchEmbed';
import { onActivate } from '../ui/a11y';

/**
 * LiveStreamCard Component
 * Displays a live stream with thumbnail, metadata, and expandable player
 */
function LiveStreamCard({ stream, onExpand }) {
  const [isExpanded, setIsExpanded] = useState(false);

  const handleToggle = () => {
    setIsExpanded(!isExpanded);
    if (onExpand) {
      onExpand(stream.entityId, !isExpanded);
    }
  };

  const formatViewerCount = (count) => {
    if (count >= 1000) {
      return `${(count / 1000).toFixed(1)}K`;
    }
    return count.toString();
  };

  return (
    <div className="live-stream-card group relative overflow-hidden rounded-lg border border-navy-700 bg-navy-900 hover:border-purple-500 transition-all duration-300">
      {/* Live indicator glow effect */}
      <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-red-500 via-purple-500 to-red-500 opacity-75 animate-pulse"></div>

      {!isExpanded ? (
        <div role="button" tabIndex={0} onKeyDown={onActivate(handleToggle)} className="cursor-pointer" onClick={handleToggle}>
          {/* Thumbnail/Preview */}
          <div className="relative aspect-video bg-navy-950 overflow-hidden">
            {stream.thumbnailUrl ? (
              <img
                src={stream.thumbnailUrl}
                alt={stream.title}
                className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
              />
            ) : (
              <div className="w-full h-full flex items-center justify-center">
                <div className="text-4xl">📺</div>
              </div>
            )}

            {/* Live badge with glow */}
            <div className="absolute top-2 left-2 flex items-center space-x-1 bg-red-600 px-2 py-1 rounded-md shadow-lg">
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-red-300 opacity-75"></span>
                <span className="relative inline-flex rounded-full h-2 w-2 bg-white"></span>
              </span>
              <span className="text-white text-xs font-bold uppercase">Live</span>
            </div>

            {/* Viewer count badge */}
            <div className="absolute top-2 right-2 bg-black bg-opacity-70 px-2 py-1 rounded-md backdrop-blur-sm">
              <div className="flex items-center space-x-1">
                <svg className="w-4 h-4 text-red-400" fill="currentColor" viewBox="0 0 20 20">
                  <path d="M10 12a2 2 0 100-4 2 2 0 000 4z" />
                  <path
                    fillRule="evenodd"
                    d="M.458 10C1.732 5.943 5.522 3 10 3s8.268 2.943 9.542 7c-1.274 4.057-5.064 7-9.542 7S1.732 14.057.458 10zM14 10a4 4 0 11-8 0 4 4 0 018 0z"
                    clipRule="evenodd"
                  />
                </svg>
                <span className="text-white text-sm font-semibold">{formatViewerCount(stream.viewerCount)}</span>
              </div>
            </div>

            {/* Hover overlay */}
            <div className="absolute inset-0 bg-gradient-to-t from-black via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-300 flex items-center justify-center">
              <div className="text-white text-sm font-semibold bg-purple-600 px-4 py-2 rounded-lg shadow-lg">
                Click to Watch
              </div>
            </div>
          </div>

          {/* Stream metadata */}
          <div className="p-4">
            <div className="flex items-start justify-between">
              <div className="flex-1 min-w-0">
                <h3 className="text-sky-100 font-semibold text-lg truncate group-hover:text-purple-300 transition-colors">
                  {stream.channelName}
                </h3>
                <p className="text-navy-400 text-sm line-clamp-2 mt-1">{stream.title || 'Live Stream'}</p>
                {stream.game && (
                  <div className="flex items-center mt-2">
                    <span className="text-xs text-gold-400 bg-gold-400 bg-opacity-10 px-2 py-1 rounded-md border border-gold-400 border-opacity-30">
                      {stream.game}
                    </span>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      ) : (
        <div>
          {/* Expanded player view */}
          <TwitchEmbed channelName={stream.channelName} height={400} muted={false} />

          {/* Metadata bar */}
          <div className="p-4 bg-navy-950 border-t border-navy-700">
            <div className="flex items-center justify-between">
              <div className="flex-1 min-w-0">
                <h3 className="text-sky-100 font-semibold text-lg truncate">{stream.channelName}</h3>
                <p className="text-navy-400 text-sm truncate">{stream.title || 'Live Stream'}</p>
              </div>
              <button
                onClick={handleToggle}
                className="ml-4 px-3 py-1 bg-navy-800 hover:bg-navy-700 text-sky-300 text-sm rounded-lg transition-colors"
              >
                Collapse
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

LiveStreamCard.propTypes = {
  stream: PropTypes.shape({
    entityId: PropTypes.string.isRequired,
    channelName: PropTypes.string.isRequired,
    viewerCount: PropTypes.number,
    title: PropTypes.string,
    game: PropTypes.string,
    thumbnailUrl: PropTypes.string,
  }).isRequired,
  onExpand: PropTypes.func,
};

export default LiveStreamCard;
