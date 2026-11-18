import { useEffect, useRef, useState } from 'react';
import { publicPrivacyApi, type AltchaChallenge, type CaptchaConfig } from '../services/publicPrivacyApi';

/**
 * Captcha widget for the public privacy form. The webui is a thin presentation layer: provider
 * and public site key come ONLY from the captcha-config endpoint. Third-party scripts load
 * dynamically from the vendor's official URL, only for the configured provider, and only while
 * this component is mounted. Fail closed: onToken(null) until a real token exists.
 */

export interface CaptchaProps {
  /** Called with a fresh token, or null while none is valid (not ready / expired / error). */
  onToken: (token: string | null) => void;
  /** Bump to discard the current token and obtain a new one (tokens are single use). */
  resetKey?: number;
  /** Visible label for assistive tech. */
  label?: string;
}

type Phase = 'loading' | 'ready' | 'unavailable';

interface TurnstileApi {
  render: (el: HTMLElement, opts: Record<string, unknown>) => string;
  reset: (id?: string) => void;
  remove: (id?: string) => void;
}
interface RecaptchaApi {
  ready: (cb: () => void) => void;
  execute: (siteKey: string, opts: { action: string }) => Promise<string>;
}
type CaptchaWindow = Window & { turnstile?: TurnstileApi; grecaptcha?: RecaptchaApi };

const TURNSTILE_SRC = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
const RECAPTCHA_SRC = 'https://www.google.com/recaptcha/api.js';
const RECAPTCHA_REFRESH_MS = 90_000;

const SCRIPT_TIMEOUT_MS = 10_000;

/**
 * Appends a vendor script once; resolves on load, rejects on error OR after a timeout so a
 * provider that never answers fails closed ('unavailable') instead of hanging forever. A failed
 * or timed-out element is removed so a later mount retries cleanly.
 */
function loadScript(src: string, timeoutMs: number = SCRIPT_TIMEOUT_MS): Promise<void> {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${src}"]`);
    if (existing?.dataset['loaded'] === 'true') {
      resolve();
      return;
    }
    const el = existing ?? document.createElement('script');
    const timer = setTimeout(() => {
      el.remove();
      reject(new Error('captcha script timed out'));
    }, timeoutMs);
    el.addEventListener('load', () => {
      clearTimeout(timer);
      el.dataset['loaded'] = 'true';
      resolve();
    });
    el.addEventListener('error', () => {
      clearTimeout(timer);
      el.remove();
      reject(new Error('captcha script failed to load'));
    });
    if (!existing) {
      el.src = src;
      el.async = true;
      el.defer = true;
      document.head.appendChild(el);
    }
  });
}

const HASHES: Record<string, string> = { 'SHA-1': 'SHA-1', 'SHA-256': 'SHA-256', 'SHA-384': 'SHA-384', 'SHA-512': 'SHA-512' };

const hex = (buf: ArrayBuffer): string =>
  Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, '0')).join('');

/**
 * Solves an altcha proof-of-work challenge with WebCrypto (documented payload format: base64 JSON
 * of {algorithm, challenge, number, salt, signature}). Yields to the event loop between batches.
 */
export async function solveAltcha(ch: AltchaChallenge, isCancelled: () => boolean): Promise<string | null> {
  const algo = HASHES[ch.algorithm.toUpperCase()];
  if (!algo) throw new Error('unsupported altcha algorithm');
  const enc = new TextEncoder();
  for (let n = 0; n <= ch.maxnumber; n++) {
    if (isCancelled()) return null;
    const digest = hex(await crypto.subtle.digest(algo, enc.encode(ch.salt + String(n))));
    if (digest === ch.challenge) {
      return btoa(JSON.stringify({ algorithm: ch.algorithm, challenge: ch.challenge, number: n, salt: ch.salt, signature: ch.signature }));
    }
    if (n % 500 === 499) await new Promise((r) => setTimeout(r, 0));
  }
  throw new Error('altcha challenge unsolved');
}

/** Renders the configured captcha provider and reports tokens through `onToken`. */
export default function Captcha({ onToken, resetKey = 0, label = 'Security check' }: CaptchaProps) {
  const [phase, setPhase] = useState<Phase>('loading');
  const [config, setConfig] = useState<CaptchaConfig | null>(null);
  const [solving, setSolving] = useState(false);
  const hostRef = useRef<HTMLDivElement>(null);
  const onTokenRef = useRef(onToken);
  onTokenRef.current = onToken;

  // Config is fetched once per mount; a failure (503/network) fails closed.
  useEffect(() => {
    let cancelled = false;
    onTokenRef.current(null);
    publicPrivacyApi
      .getCaptchaConfig()
      .then((cfg) => {
        if (cancelled) return;
        const known = ['turnstile', 'recaptcha', 'altcha'].includes(cfg.provider);
        const needsKey = cfg.provider === 'turnstile' || cfg.provider === 'recaptcha';
        if (!known || (needsKey && !cfg.siteKey)) {
          setPhase('unavailable');
          return;
        }
        setConfig(cfg);
        setPhase('ready');
      })
      .catch(() => {
        if (!cancelled) setPhase('unavailable');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (phase !== 'ready' || !config) return undefined;
    let cancelled = false;
    let widgetId: string | undefined;
    let timer: ReturnType<typeof setInterval> | undefined;
    const w = window as CaptchaWindow;
    onTokenRef.current(null);

    const fail = () => {
      if (!cancelled) {
        onTokenRef.current(null);
        setPhase('unavailable');
      }
    };

    if (config.provider === 'turnstile') {
      loadScript(TURNSTILE_SRC)
        .then(() => {
          if (cancelled || !hostRef.current || !w.turnstile) return;
          widgetId = w.turnstile.render(hostRef.current, {
            sitekey: config.siteKey,
            action: 'dsar_request',
            callback: (t: string) => onTokenRef.current(t),
            'expired-callback': () => onTokenRef.current(null),
            'error-callback': () => onTokenRef.current(null),
          });
        })
        .catch(fail);
    } else if (config.provider === 'recaptcha') {
      const key = config.siteKey as string;
      const run = () => {
        w.grecaptcha?.ready(() => {
          w.grecaptcha
            ?.execute(key, { action: 'dsar_request' })
            .then((t) => {
              if (!cancelled) onTokenRef.current(t);
            })
            .catch(() => {
              if (!cancelled) onTokenRef.current(null);
            });
        });
      };
      loadScript(`${RECAPTCHA_SRC}?render=${encodeURIComponent(key)}`)
        .then(() => {
          if (cancelled) return;
          run();
          timer = setInterval(run, RECAPTCHA_REFRESH_MS);
        })
        .catch(fail);
    } else if (config.provider === 'altcha') {
      setSolving(true);
      publicPrivacyApi
        .getAltchaChallenge(config.challengeUrl)
        .then((ch) => solveAltcha(ch, () => cancelled))
        .then((payload) => {
          if (!cancelled && payload) onTokenRef.current(payload);
        })
        .catch(fail)
        .finally(() => {
          if (!cancelled) setSolving(false);
        });
    }

    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
      if (widgetId !== undefined) {
        try {
          w.turnstile?.remove(widgetId);
        } catch {
          /* widget already gone */
        }
      }
    };
  }, [phase, config, resetKey]);

  if (phase === 'unavailable') {
    return (
      <p role="alert" data-testid="captcha-unavailable" className="rounded border border-danger-fg p-3 text-sm">
        The security check is temporarily unavailable, so requests cannot be submitted right now. Please try again later.
      </p>
    );
  }

  return (
    <div data-testid="captcha" aria-label={label} role="group">
      {phase === 'loading' && <p role="status" className="text-sm text-fg-muted">Loading security check…</p>}
      {config?.provider === 'turnstile' && <div ref={hostRef} />}
      {config?.provider === 'recaptcha' && (
        <p className="text-xs text-fg-muted">
          Protected by reCAPTCHA; the Google{' '}
          <a href="https://policies.google.com/privacy" rel="noopener noreferrer" className="underline">Privacy Policy</a> and{' '}
          <a href="https://policies.google.com/terms" rel="noopener noreferrer" className="underline">Terms of Service</a> apply.
        </p>
      )}
      {config?.provider === 'altcha' && (
        <p role="status" className="text-sm text-fg-muted">{solving ? 'Running security check…' : 'Security check complete.'}</p>
      )}
    </div>
  );
}
