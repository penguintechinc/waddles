/**
 * Chat Input Component
 * Text input with send button and character limit
 */
import { useState, useRef } from 'react';
import { PaperAirplaneIcon } from '@heroicons/react/24/solid';

const MAX_LENGTH = 2000;

export default function ChatInput({ onSendMessage, disabled }) {
  const [message, setMessage] = useState('');
  const [isTyping, setIsTyping] = useState(false);
  const textareaRef = useRef(null);

  const handleSubmit = (e) => {
    e.preventDefault();

    const trimmed = message.trim();
    if (!trimmed || disabled) return;

    onSendMessage(trimmed);
    setMessage('');
    setIsTyping(false);

    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  const handleKeyDown = (e) => {
    // Enter to send, Shift+Enter for newline
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  const handleChange = (e) => {
    const value = e.target.value;
    if (value.length <= MAX_LENGTH) {
      setMessage(value);

      // Auto-resize textarea
      if (textareaRef.current) {
        textareaRef.current.style.height = 'auto';
        textareaRef.current.style.height = `${textareaRef.current.scrollHeight}px`;
      }

      // Typing indicator
      if (value && !isTyping) {
        setIsTyping(true);
      } else if (!value && isTyping) {
        setIsTyping(false);
      }
    }
  };

  const remaining = MAX_LENGTH - message.length;
  const showCounter = message.length > MAX_LENGTH * 0.8;

  return (
    <form onSubmit={handleSubmit} className="bg-surface-raised border-t border-line">
      <div className="p-4">
        <div className="flex gap-2">
          {/* Text input */}
          <div className="flex-1 relative">
            <textarea
              aria-label="Type a message... (Enter to send, Shift+Enter for newline)"
              ref={textareaRef}
              value={message}
              onChange={handleChange}
              onKeyDown={handleKeyDown}
              placeholder="Type a message... (Enter to send, Shift+Enter for newline)"
              disabled={disabled}
              className="w-full bg-navy-700 text-fg rounded-lg px-4 py-2 pr-16
                         focus:outline-none focus:ring-2 focus:ring-blue-500
                         resize-none min-h-[44px] max-h-32 overflow-y-auto
                         disabled:opacity-50 disabled:cursor-not-allowed"
              rows={1}
            />

            {/* Character counter */}
            {showCounter && (
              <div
                className={`absolute bottom-2 right-2 text-xs font-medium px-2 py-1 rounded ${
                  remaining < 100 ? 'text-red-400 bg-red-900/30' : 'text-fg-muted bg-surface/50'
                }`}
              >
                {remaining}
              </div>
            )}
          </div>

          {/* Send button */}
          <button
            type="submit"
            disabled={!message.trim() || disabled}
            className="bg-blue-600 hover:bg-blue-700 disabled:bg-navy-600 disabled:cursor-not-allowed
                       text-white rounded-lg px-4 py-2 transition-colors flex items-center gap-2"
          >
            <PaperAirplaneIcon className="w-5 h-5" />
            <span className="hidden sm:inline">Send</span>
          </button>
        </div>

        {/* Help text */}
        <div className="mt-2 text-xs text-fg-muted">
          Press <kbd className="px-1 bg-navy-700 rounded">Enter</kbd> to send, <kbd className="px-1 bg-navy-700 rounded">Shift</kbd> + <kbd className="px-1 bg-navy-700 rounded">Enter</kbd> for newline
        </div>
      </div>
    </form>
  );
}
