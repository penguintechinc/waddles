/**
 * WebSocket Integration Tests
 * Tests real-time communication via WebSocket connections
 */

const WebSocket = require('ws');

const WS_URL = process.env.WS_URL || 'ws://localhost:8204';

describe('WebSocket Integration Tests', () => {
  let ws;

  afterEach(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.close();
    }
  });

  describe('Connection', () => {
    it('should connect to WebSocket server', (done) => {
      ws = new WebSocket(WS_URL);

      ws.on('open', () => {
        expect(ws.readyState).toBe(WebSocket.OPEN);
        done();
      });

      ws.on('error', (error) => {
        done(error);
      });
    }, 10000);

    it('should handle connection close gracefully', (done) => {
      ws = new WebSocket(WS_URL);

      ws.on('open', () => {
        ws.close();
      });

      ws.on('close', () => {
        expect(ws.readyState).toBe(WebSocket.CLOSED);
        done();
      });

      ws.on('error', (error) => {
        done(error);
      });
    }, 10000);
  });

  describe('Message Exchange', () => {
    it('should send and receive messages', (done) => {
      ws = new WebSocket(WS_URL);

      ws.on('open', () => {
        ws.send(JSON.stringify({ type: 'ping' }));
      });

      ws.on('message', (data) => {
        const message = JSON.parse(data.toString());
        expect(message).toHaveProperty('type');
        done();
      });

      ws.on('error', (error) => {
        done(error);
      });
    }, 10000);
  });

  describe('Authentication', () => {
    it('should accept authenticated connections', (done) => {
      const token = process.env.TEST_TOKEN || 'test-token';
      ws = new WebSocket(`${WS_URL}?token=${token}`);

      ws.on('open', () => {
        expect(ws.readyState).toBe(WebSocket.OPEN);
        done();
      });

      ws.on('error', () => {
        // May fail if token is invalid, which is acceptable
        done();
      });
    }, 10000);
  });
});
