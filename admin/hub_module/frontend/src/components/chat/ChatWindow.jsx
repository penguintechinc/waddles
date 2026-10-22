/**
 * Chat Window Component
 * Message list with auto-scroll and typing indicators
 */
import { useEffect, useRef, useState } from 'react';
import ChatMessage from './ChatMessage';

export default function ChatWindow({ messages, typingUsers, loading }) {
  const messagesEndRef = useRef(null);
  const containerRef = useRef(null);
  const [autoScroll, setAutoScroll] = useState(true);

  // Auto-scroll to bottom when new messages arrive
  useEffect(() => {
    if (autoScroll && messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, autoScroll]);

  // Check if user has scrolled up
  const handleScroll = () => {
    if (!containerRef.current) return;

    const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
    const isAtBottom = scrollHeight - scrollTop - clientHeight < 50;

    setAutoScroll(isAtBottom);
  };

  // Scroll to bottom button
  const scrollToBottom = () => {
    if (messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior: 'smooth' });
      setAutoScroll(true);
    }
  };

  return (
    <div className="flex-1 flex flex-col overflow-hidden relative">
      {/* Messages container */}
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto px-4 py-4 space-y-1"
      >
        {loading ? (
          <div className="flex items-center justify-center h-full">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500"></div>
          </div>
        ) : messages.length === 0 ? (
          <div className="flex items-center justify-center h-full text-gray-500">
            No messages yet. Start the conversation!
          </div>
        ) : (
          <>
            {messages.map((message) => (
              <ChatMessage key={message.id} message={message} />
            ))}

            {/* Typing indicators */}
            {typingUsers.length > 0 && (
              <div className="flex gap-3 p-3 text-gray-400 text-sm italic">
                <div className="flex-shrink-0 w-10 h-10"></div>
                <div className="flex-1">
                  {typingUsers.length === 1
                    ? `${typingUsers[0].username} is typing...`
                    : `${typingUsers.length} people are typing...`}
                </div>
              </div>
            )}

            {/* Scroll anchor */}
            <div ref={messagesEndRef} />
          </>
        )}
      </div>

      {/* Scroll to bottom button */}
      {!autoScroll && messages.length > 0 && (
        <button
          aria-label="Scroll to latest message"
          onClick={scrollToBottom}
          className="absolute bottom-4 right-4 bg-blue-600 hover:bg-blue-700 text-white
                     rounded-full p-3 shadow-lg transition-all transform hover:scale-110"
        >
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 14l-7 7m0 0l-7-7m7 7V3" />
          </svg>
        </button>
      )}
    </div>
  );
}
