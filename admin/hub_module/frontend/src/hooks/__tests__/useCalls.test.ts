/** Calls polling policy: refresh on an interval, but never keep hammering a failed read. */
import { describe, expect, it } from 'vitest';
import { CALLS_POLL_MS, callKeys, pollUnlessFailed } from '../useCalls';

describe('useCalls', () => {
  it('polls on the calls interval while a read is healthy or pending', () => {
    expect(pollUnlessFailed({ status: 'success' })).toBe(CALLS_POLL_MS);
    expect(pollUnlessFailed({ status: 'pending' })).toBe(CALLS_POLL_MS);
  });

  it('stops polling once a read has failed so the error state stays until the user retries', () => {
    expect(pollUnlessFailed({ status: 'error' })).toBe(false);
  });

  it('scopes query keys by route family, community and room', () => {
    expect(callKeys.rooms('admin', 7)).toEqual(['calls', 'admin', 7, 'rooms']);
    expect(callKeys.rooms('member', 7)).toEqual(['calls', 'member', 7, 'rooms']);
    expect(callKeys.hands(7, 'r')).toEqual(['calls', 'admin', 7, 'rooms', 'r', 'hands']);
    expect(callKeys.participants(7, 'r')).toEqual(['calls', 'admin', 7, 'rooms', 'r', 'participants']);
  });
});
