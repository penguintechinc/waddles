/**
 * Contract-test harness for the hub-webui API helper objects (`services/api.js`).
 *
 * Replaces the axios adapter (the HTTP layer) with a recorder so each helper
 * can be called for real and its outgoing request -- method, URL, query
 * params and JSON body -- asserted, without a network or any mocked helper
 * logic.
 */
import type { AxiosInstance, InternalAxiosRequestConfig } from 'axios';
import { expect } from 'vitest';

/** Extra expectations for a row: body and/or query params (`@arg` refs an argument). */
export interface ContractExpect {
  body?: unknown;
  params?: unknown;
}

/** `[signature, 'METHOD /path/{arg}', extra?]` -- one API helper's contract. */
export type ContractRow = readonly [sig: string, route: string, extra?: ContractExpect];

const BODYISH = /^(data|config|settings|station|policy|body|payload|formData|credential)$/;

/** Deterministic, recognisable argument value for a helper parameter name. */
export function sentinel(name: string): unknown {
  if (name === 'params') return { q: 'params 1' };
  if (BODYISH.test(name)) return { d: `${name} 1` };
  return `${name} 1`;
}

/** Swaps the client's adapter for a recorder; returns the captured requests, a mutable reply body and a restore fn. */
export function recordRequests(client: AxiosInstance): {
  calls: InternalAxiosRequestConfig[];
  reply: { data: unknown };
  restore: () => void;
} {
  const calls: InternalAxiosRequestConfig[] = [];
  const reply: { data: unknown } = { data: { success: true } };
  const original = client.defaults.adapter;
  client.defaults.adapter = (config) => {
    calls.push(config);
    return Promise.resolve({ data: reply.data, status: 200, statusText: 'OK', headers: {}, config });
  };
  return {
    calls,
    reply,
    restore: () => restoreAdapter(client, original),
  };
}

/** Puts a previously saved adapter back (or removes the override if there was none). */
export function restoreAdapter(client: AxiosInstance, original: AxiosInstance['defaults']['adapter']): void {
  if (original === undefined) {
    delete client.defaults.adapter;
  } else {
    client.defaults.adapter = original;
  }
}

function resolveRefs(value: unknown, args: Record<string, unknown>): unknown {
  if (typeof value === 'string' && value.startsWith('@')) {
    const key = value.slice(1);
    if (!(key in args)) throw new Error(`contract row references unknown arg ${key}`);
    return args[key];
  }
  if (Array.isArray(value)) return value.map((v) => resolveRefs(v, args));
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, resolveRefs(v, args)]));
  }
  return value;
}

function parseSig(sig: string): { name: string; argNames: string[] } {
  const m = /^(\w+)\((.*)\)$/.exec(sig);
  if (!m || m[1] === undefined) throw new Error(`bad contract signature: ${sig}`);
  const argList = m[2] ?? '';
  return { name: m[1], argNames: argList.split(',').map((s) => s.trim()).filter(Boolean) };
}

function expandRoute(route: string, args: Record<string, unknown>): { method: string; url: string } {
  const sp = route.indexOf(' ');
  const method = route.slice(0, sp);
  const url = route.slice(sp + 1).replace(/\{(\w+)(\|enc)?\}/g, (_m, name: string, enc?: string) => {
    const v = String(args[name]);
    return enc ? encodeURIComponent(v) : v;
  });
  return { method, url };
}

/** Calls one helper with sentinel args and asserts the exact request it emitted. */
export async function assertContractRow(
  group: object,
  row: ContractRow,
  calls: InternalAxiosRequestConfig[],
): Promise<void> {
  const [sig, route, extra] = row;
  const { name, argNames } = parseSig(sig);
  const fn: unknown = Reflect.get(group, name);
  if (typeof fn !== 'function') throw new Error(`${name} is not exported`);

  const args: Record<string, unknown> = Object.fromEntries(argNames.map((n) => [n, sentinel(n)]));
  calls.length = 0;
  await fn(...argNames.map((n) => args[n]));

  expect(calls).toHaveLength(1);
  const sent = calls[0];
  expect(sent).toBeDefined();
  const { method, url } = expandRoute(route, args);
  expect(sent?.method?.toUpperCase()).toBe(method);
  expect(sent?.url).toBe(url);

  const wantParams =
    extra && 'params' in extra ? resolveRefs(extra.params, args) : argNames.includes('params') ? args.params : undefined;
  expect(sent?.params).toEqual(wantParams);

  const rawBody = typeof sent?.data === 'string' ? (JSON.parse(sent.data) as unknown) : sent?.data;
  const wantBody = extra && 'body' in extra ? resolveRefs(extra.body, args) : undefined;
  expect(rawBody).toEqual(wantBody);
}
