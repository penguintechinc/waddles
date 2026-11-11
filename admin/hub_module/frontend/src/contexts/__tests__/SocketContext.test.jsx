/**
 * Regression tests for security.md's C4 fix as it applies to the socket.io
 * handshake -- `SocketContext` used to read the session JWT straight out
 * of `localStorage` (`auth: {token}`) to authenticate the socket. The JWT
 * now lives only in the HttpOnly `wb_session` cookie, which this page has
 * no way to read, so the handshake has to rely on the cookie being sent
 * automatically (`withCredentials: true`) and gate the connection attempt
 * on `isAuthenticated` (AuthContext's own `/me`-derived state) instead of
 * a token value that no longer exists client-side.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, act } from '@testing-library/react';

const managerHandlers = {};
const mockSocket = {
  io: { on: vi.fn((evt, cb) => { managerHandlers[evt] = cb; }) },
  on: vi.fn(),
  disconnect: vi.fn(),
  connected: false,
};
const ioMock = vi.fn(() => mockSocket);
vi.mock('socket.io-client', () => ({ io: (...args) => ioMock(...args) }));

let mockFlag = true;
vi.mock('../../lib/useFeatureFlag', () => ({
  useFeatureFlag: () => mockFlag,
}));

let mockAuth = { isAuthenticated: false };
vi.mock('../AuthContext', () => ({
  useAuth: () => mockAuth,
}));

import { SocketProvider, useSocket } from '../SocketContext';

function harness() {
  const captured = {};
  function Probe() {
    Object.assign(captured, useSocket());
    return <div data-testid="ready" />;
  }
  const view = render(
    <SocketProvider>
      <Probe />
    </SocketProvider>,
  );
  return { captured, ...view };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockFlag = true;
  mockAuth = { isAuthenticated: false };
});

describe('SocketProvider', () => {
  it('does not connect when the user is not authenticated', () => {
    mockAuth = { isAuthenticated: false };
    harness();

    expect(ioMock).not.toHaveBeenCalled();
  });

  it('connects via the cookie (withCredentials), never a localStorage token', () => {
    mockAuth = { isAuthenticated: true };
    harness();

    expect(ioMock).toHaveBeenCalledTimes(1);
    const [, options] = ioMock.mock.calls[0];
    expect(options.withCredentials).toBe(true);
    expect(options.auth).toBeUndefined();
  });

  it('disconnects when auth state flips from authenticated to logged out', () => {
    mockAuth = { isAuthenticated: true };
    const { rerender } = harness();
    expect(ioMock).toHaveBeenCalledTimes(1);

    mockAuth = { isAuthenticated: false };
    act(() => {
      rerender(
        <SocketProvider>
          <div />
        </SocketProvider>,
      );
    });

    expect(mockSocket.disconnect).toHaveBeenCalled();
  });

  it('flag OFF: never constructs a socket and reports degraded', () => {
    mockFlag = false;
    mockAuth = { isAuthenticated: true };
    const { captured } = harness();

    expect(ioMock).not.toHaveBeenCalled();
    expect(captured.socket).toBeNull();
    expect(captured.degraded).toBe(true);
  });

  it('flag ON: bounds reconnection and reports degraded after exhaustion', () => {
    mockAuth = { isAuthenticated: true };
    const { captured } = harness();

    const [, options] = ioMock.mock.calls[0];
    expect(options.reconnectionAttempts).toBe(5);
    expect(captured.degraded).toBe(false);

    act(() => {
      managerHandlers.reconnect_failed();
    });
    expect(captured.degraded).toBe(true);
  });
});
