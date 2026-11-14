/**
 * Shared fixtures for the calls page/view tests: spec-shaped wire bodies and a URL-routed stub for
 * `apiClient` so each test states which endpoint answers with what (any unexpected URL fails loudly).
 */
import { vi } from 'vitest';
import { apiClient } from '../lib/apiClient';
import { httpError, ok } from './api';

export const ROOM_ID = '3f2b8c1e-9a4d-4e57-8b21-6c0d5e7f1a90';
export const OTHER_ROOM_ID = 'c4d5e6f7-0a1b-4c2d-8e3f-9a0b1c2d3e4f';
export const ADA = '7a1d2c3b-5e6f-4a80-9b1c-2d3e4f5a6b7c';
export const GRACE = '11111111-2222-4333-8444-555555555555';

export const roomWire = (over: Record<string, unknown> = {}) => ({
  room_id: ROOM_ID,
  community_id: '7',
  label: 'Town hall',
  max_publishers: 25,
  created_at: 1760000000,
  publisher_ids: [ADA],
  subscriber_count: 2,
  locked: false,
  ...over,
});

export const participantWire = (id: string, name: string, over: Record<string, unknown> = {}) => ({
  participant_id: id,
  display_name: name,
  publisher: true,
  subscriber: false,
  muted: false,
  joined_at: 1760000100,
  ...over,
});

export const handWire = (id: string, name: string, raisedAt: number, over: Record<string, unknown> = {}) => ({
  participant_id: id,
  display_name: name,
  raised_at: raisedAt,
  acknowledged_at: null,
  acknowledged_by: null,
  ...over,
});

export const envelope = (code: string, message: string, details?: Record<string, unknown>) => ({
  success: false,
  error: { code, message, ...(details ? { details } : {}) },
});

/** A route answer: a JSON body, an Error to throw, or a function computing either. */
export type Answer = unknown;
export type Routes = Record<string, Answer | (() => Answer)>;

function answer(routes: Routes, method: string, url: string): unknown {
  const key = `${method} ${url}`;
  if (!(key in routes)) throw new Error(`Unexpected ${key}`);
  const entry = routes[key];
  const value = typeof entry === 'function' ? (entry as () => Answer)() : entry;
  if (value instanceof Error) throw value;
  return value;
}

/** Routes GET/POST/DELETE on `apiClient` through `routes` keyed `"METHOD /path"`; returns the spies. */
export function serve(routes: Routes) {
  const get = vi.spyOn(apiClient, 'get').mockImplementation(async (url) => ok(answer(routes, 'GET', url)));
  const post = vi.spyOn(apiClient, 'post').mockImplementation(async (url) => ok(answer(routes, 'POST', url)));
  const del = vi.spyOn(apiClient, 'delete').mockImplementation(async (url) => ok(answer(routes, 'DELETE', url)));
  return { get, post, del };
}

export { httpError };
