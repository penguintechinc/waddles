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
     * Header name -> secret reference name. The stage resolves the reference
     * and injects the header; the secret value never enters the component.
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
