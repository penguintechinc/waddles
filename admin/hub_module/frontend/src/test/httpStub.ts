import type { AxiosInstance, AxiosAdapter, InternalAxiosRequestConfig } from 'axios';

/** One recorded HTTP request as it reached the (stubbed) network boundary. */
export interface SeenRequest {
  method: string;
  url: string;
  params: unknown;
  body: unknown;
}

export type HttpReply = unknown | ((req: SeenRequest) => unknown);

/**
 * Replaces the adapter (the HTTP layer) of the given axios clients so the page, the shared
 * api module and axios itself all run for real. `routes` maps `'METHOD /exact/url'` to a JSON
 * body (or a function returning one, which may throw to fail the call). Anything unlisted
 * answers 404 so a stray or mis-prefixed call fails loudly instead of passing silently.
 */
export function stubHttp(clients: AxiosInstance[], routes: Record<string, HttpReply>): { seen: SeenRequest[]; restore: () => void } {
  const seen: SeenRequest[] = [];
  const originals = clients.map((c) => c.defaults.adapter);
  const adapter: AxiosAdapter = (config: InternalAxiosRequestConfig) => {
    const req: SeenRequest = {
      method: (config.method ?? 'get').toUpperCase(),
      url: config.url ?? '',
      params: config.params,
      body: typeof config.data === 'string' ? (JSON.parse(config.data) as unknown) : config.data,
    };
    seen.push(req);
    const key = `${req.method} ${req.url}`;
    const reply = routes[key];
    if (reply === undefined) {
      return Promise.reject(Object.assign(new Error(`stubHttp: unexpected ${key}`), { isAxiosError: true, config, response: { status: 404, data: { message: 'Not found' }, statusText: 'Not Found', headers: {}, config } }));
    }
    try {
      const data = typeof reply === 'function' ? (reply as (r: SeenRequest) => unknown)(req) : reply;
      return Promise.resolve({ data, status: 200, statusText: 'OK', headers: {}, config });
    } catch (error) {
      return Promise.reject(error);
    }
  };
  for (const c of clients) c.defaults.adapter = adapter;
  return {
    seen,
    restore: () => clients.forEach((c, i) => { if (originals[i] === undefined) delete c.defaults.adapter; else c.defaults.adapter = originals[i]; }),
  };
}
