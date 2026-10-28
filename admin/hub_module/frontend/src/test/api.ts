import { AxiosHeaders, type AxiosResponse } from 'axios';
import { vi, type MockInstance } from 'vitest';
import { apiClient } from '../lib/apiClient';

/** Builds a well-typed 200 response around `data` (no casts needed in tests). */
export function ok<T>(data: T, status = 200): AxiosResponse<T> {
  return { data, status, statusText: 'OK', headers: {}, config: { headers: new AxiosHeaders() } };
}

/** An axios-shaped HTTP failure: what `getErrorMessage` and the pages read. */
export function httpError(status: number, data?: unknown): Error {
  return Object.assign(new Error(`Request failed with status code ${status}`), {
    isAxiosError: true,
    response: { status, data },
  });
}

export type GetHandler = (url: string, params: unknown) => unknown;

/**
 * Routes every `apiClient.get` through `handler(url, params)`. Return the JSON body to
 * succeed, or throw (`httpError`) to fail, so a test states exactly which served endpoint
 * answers with what, and any unexpected URL fails loudly.
 */
export function mockApiGet(handler: GetHandler): MockInstance<typeof apiClient.get> {
  return vi.spyOn(apiClient, 'get').mockImplementation(async (url, config) => ok(handler(url, config?.params)));
}

/** Throws for a URL the test did not expect, naming it. */
export function unexpectedUrl(url: string): never {
  throw new Error(`Unexpected GET ${url}`);
}
