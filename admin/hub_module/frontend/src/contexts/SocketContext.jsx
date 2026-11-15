/**
 * Socket Context
 * Manages WebSocket connection for real-time chat
 */
import { createContext, useContext, useEffect, useState, useRef } from 'react';
import { io } from 'socket.io-client';
import { useAuth } from './AuthContext';
import { useFeatureFlag } from '../lib/useFeatureFlag';

/** PostHog flag gating all realtime chat; unseen/OFF means no socket is ever created. */
export const REALTIME_FLAG = 'waddles.community.chat-realtime';
/** Max socket.io reconnection attempts before the UI is shown as degraded. */
export const MAX_RECONNECT_ATTEMPTS = 5;

const SocketContext = createContext(null);

export function SocketProvider({ children }) {
  const { isAuthenticated } = useAuth();
  const [socket, setSocket] = useState(null);
  const [connected, setConnected] = useState(false);
  const [exhausted, setExhausted] = useState(false);
  const realtimeEnabled = useFeatureFlag(REALTIME_FLAG);
  const socketRef = useRef(null);

  useEffect(() => {
    // SECURITY (security.md C4): the session JWT lives only in the HttpOnly
    // `wb_session` cookie — this page can't read it into an `auth: {token}`
    // handshake payload (that is the point). `withCredentials: true` makes
    // the socket.io handshake (both the initial HTTP request and any
    // polling fallback) send that cookie the same way axios does; the
    // server reads it from the handshake instead of `socket.handshake.
    // auth.token`. Gate the connection attempt on `isAuthenticated` (from
    // AuthContext's own `/me` check) rather than a token we can no longer
    // see client-side.
    // Flag OFF (default): never construct a socket -- no connect/retry loop.
    if (!isAuthenticated || !realtimeEnabled) {
      return;
    }
    setExhausted(false);

    // Create socket connection
    const newSocket = io(import.meta.env.VITE_API_URL || window.location.origin, {
      withCredentials: true,
      transports: ['websocket', 'polling'],
      reconnection: true,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 5000,
      reconnectionAttempts: MAX_RECONNECT_ATTEMPTS,
    });

    socketRef.current = newSocket;

    // Connection event handlers
    newSocket.on('connect', () => {
      console.log('[Socket] Connected:', newSocket.id);
      setConnected(true);
      setExhausted(false);
    });

    newSocket.on('disconnect', (reason) => {
      console.log('[Socket] Disconnected:', reason);
      setConnected(false);
    });

    newSocket.on('connect_error', (error) => {
      console.error('[Socket] Connection error:', error.message);
      setConnected(false);
    });

    newSocket.on('reconnect', (attemptNumber) => {
      console.log('[Socket] Reconnected after', attemptNumber, 'attempts');
      setConnected(true);
    });

    // Bounded retries spent: surface the degraded state, never retry forever.
    newSocket.io.on('reconnect_failed', () => {
      console.error('[Socket] Reconnection exhausted', { attempts: MAX_RECONNECT_ATTEMPTS });
      setExhausted(true);
      setConnected(false);
    });

    newSocket.on('reconnect_attempt', (attemptNumber) => {
      console.log('[Socket] Reconnection attempt:', attemptNumber);
    });

    setSocket(newSocket);

    // Cleanup on unmount, or when auth state flips (login/logout) --
    // reconnects the socket with the new cookie-derived identity.
    return () => {
      if (socketRef.current) {
        socketRef.current.disconnect();
        socketRef.current = null;
      }
      setSocket(null);
      setConnected(false);
    };
  }, [isAuthenticated, realtimeEnabled]);

  const value = {
    socket,
    connected,
    realtimeEnabled,
    // Live updates unavailable: flag OFF or reconnection attempts exhausted.
    degraded: !realtimeEnabled || exhausted,
  };

  return <SocketContext.Provider value={value}>{children}</SocketContext.Provider>;
}

export function useSocket() {
  const context = useContext(SocketContext);
  if (!context) {
    throw new Error('useSocket must be used within SocketProvider');
  }
  return context;
}
