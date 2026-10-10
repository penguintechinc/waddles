// GENERATED FILE -- do not edit by hand.
// Source: wit/waddle-bundle/stage.wit, regenerated via
// scripts/generate-guest-types.sh (jco guest-types).

declare module 'waddle:bundle/http@1.0.0' {
  export function send(req: Request): Response;
  export interface Header {
    name: string,
    value: string,
  }
  export interface Request {
    method: string,
    url: string,
    headers: Array<Header>,
    body?: Uint8Array,
    /**
     * Secret slot -> secret reference name. The stage resolves the reference
     * host-side and injects the value; the secret value never enters the
     * component. A plain slot name is a HEADER ref: `Authorization` sends the
     * resolved value as that request header. A slot name prefixed with `?` is
     * a QUERY-PARAMETER ref: `?key` appends `key=VALUE` to the request URL's
     * query (replacing any same-named parameter the bundle supplied), for
     * APIs that authenticate by query string. A query ref is injected only on
     * the first hop, to the granted net.http.fqdn host, and is dropped on
     * every redirect hop. The value is scrubbed from error strings and from
     * response headers/body. A ref that cannot be resolved fails the call
     * (denied) instead of sending it unauthenticated.
     */
    secretRefs: Array<[string, string]>,
  }
  export interface Response {
    status: number,
    headers: Array<Header>,
    body: Uint8Array,
    truncated: boolean,
  }
  export type Error = ErrorDenied | ErrorTimeout | ErrorTooLarge | ErrorRateLimited | ErrorTransport;
  export interface ErrorDenied {
    tag: 'denied',
    val: string,
  }
  export interface ErrorTimeout {
    tag: 'timeout',
  }
  export interface ErrorTooLarge {
    tag: 'too-large',
    val: bigint,
  }
  export interface ErrorRateLimited {
    tag: 'rate-limited',
    val: number,
  }
  export interface ErrorTransport {
    tag: 'transport',
    val: string,
  }
}
