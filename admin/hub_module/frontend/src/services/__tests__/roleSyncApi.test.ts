/** Tests for the typed role-sync API client: URL/payload shape, unwrap, error envelope. */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from '../../lib/apiClient';
import {
  COMMUNITY_ROLES,
  SUBSCRIBER_TIERS,
  SYNC_SCOPES,
  extractErrorMessage,
  roleSyncApi,
} from '../roleSyncApi';

describe('roleSyncApi', () => {
  afterEach(() => vi.restoreAllMocks());

  it('listPairings GETs the community path and unwraps pairings', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: { success: true, pairings: [{ id: 1 }] } });
    await expect(roleSyncApi.listPairings(7)).resolves.toEqual([{ id: 1 }]);
    expect(get).toHaveBeenCalledWith('/api/v1/communities/7/guild-pairings');
  });

  it('listBindings GETs role-bindings and unwraps bindings', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: { success: true, bindings: [{ id: 2 }] } });
    await expect(roleSyncApi.listBindings(7, 3)).resolves.toEqual([{ id: 2 }]);
    expect(get).toHaveBeenCalledWith('/api/v1/communities/7/guild-pairings/3/role-bindings');
  });

  it('createBinding POSTs the payload and unwraps binding', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: { success: true, binding: { id: 9 } } });
    const payload = { sync_scope: 'subscriber_tier' as const, discord_role_id: '123', subscriber_tier: 2 as const };
    await expect(roleSyncApi.createBinding(7, 3, payload)).resolves.toEqual({ id: 9 });
    expect(post).toHaveBeenCalledWith('/api/v1/communities/7/guild-pairings/3/role-bindings', payload);
  });

  it('deleteBinding DELETEs the binding path', async () => {
    const del = vi.spyOn(apiClient, 'delete').mockResolvedValue({ data: undefined });
    await roleSyncApi.deleteBinding(7, 3, 5);
    expect(del).toHaveBeenCalledWith('/api/v1/communities/7/guild-pairings/3/role-bindings/5');
  });

  it('propagates HTTP failures to the caller', async () => {
    const err = { response: { status: 403, data: { error: { message: 'nope' } } } };
    vi.spyOn(apiClient, 'get').mockRejectedValue(err);
    await expect(roleSyncApi.listPairings(1)).rejects.toBe(err);
  });

  it('exports the constraint value sets', () => {
    expect(SYNC_SCOPES).toEqual(['subscriber_tier', 'moderator', 'community_role']);
    expect(SUBSCRIBER_TIERS).toEqual([1, 2, 3]);
    expect(COMMUNITY_ROLES).not.toContain('community-owner');
  });
});

describe('extractErrorMessage', () => {
  it('returns the envelope message', () => {
    expect(extractErrorMessage({ response: { data: { error: { message: 'bad role' } } } }, 'fb')).toBe('bad role');
  });
  it.each([
    ['empty message', { response: { data: { error: { message: '' } } } }],
    ['no envelope', { response: { data: {} } }],
    ['no data', { response: {} }],
    ['non-object response', { response: 'x' }],
    ['plain Error', new Error('boom')],
    ['null', null],
    ['string', 'oops'],
  ])('falls back for %s', (_n, input) => {
    expect(extractErrorMessage(input, 'fallback')).toBe('fallback');
  });
});
