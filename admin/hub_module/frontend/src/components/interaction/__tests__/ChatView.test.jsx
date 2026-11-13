/**
 * ChatView degraded-state tests: when realtime is unavailable the view shows
 * the visible notice, loads REST history, and disables sending.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

const getMock = vi.fn();
vi.mock('../../../services/api', () => ({ default: { get: (...a) => getMock(...a) } }));

import ChatView from '../ChatView';

const channel = { id: 7, name: 'general', communityId: 3 };

beforeEach(() => {
  vi.clearAllMocks();
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  getMock.mockResolvedValue({
    data: { messages: [{ id: 1, content: 'hello there', senderUsername: 'u' }] },
  });
});

describe('ChatView', () => {
  it('degraded: shows banner, REST history, disabled send, no socket use', async () => {
    const socket = { emit: vi.fn(), on: vi.fn(), off: vi.fn() };
    render(<ChatView channel={channel} socket={socket} degraded />);

    expect(screen.getByTestId('realtime-unavailable-banner')).toHaveTextContent(
      'Live chat updates are unavailable; refresh to see new messages',
    );
    expect(await screen.findByText('hello there')).toBeInTheDocument();
    expect(getMock).toHaveBeenCalledWith('/api/v1/community/3/chat/history', {
      params: { channelName: 'hub-channel-7' },
    });
    expect(screen.getByLabelText('Message #general')).toBeDisabled();
    expect(screen.getByLabelText('Send message')).toBeDisabled();
    expect(socket.emit).not.toHaveBeenCalled();
  });

  it('live: no banner and joins via the socket', async () => {
    const socket = { emit: vi.fn(), on: vi.fn(), off: vi.fn() };
    render(<ChatView channel={channel} socket={socket} />);

    expect(screen.queryByTestId('realtime-unavailable-banner')).toBeNull();
    await waitFor(() => expect(socket.emit).toHaveBeenCalledWith('chat:join', expect.anything()));
    expect(getMock).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Message #general')).not.toBeDisabled();
  });
});
