/** Captcha widget: config-driven provider paths, fail-closed unavailable state, altcha PoW. */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../services/publicPrivacyApi', () => ({
  publicPrivacyApi: { getCaptchaConfig: vi.fn(), getAltchaChallenge: vi.fn() },
}));
import { publicPrivacyApi } from '../../services/publicPrivacyApi';
import Captcha, { solveAltcha } from '../Captcha';

const api = vi.mocked(publicPrivacyApi);
type W = Window & { turnstile?: unknown; grecaptcha?: unknown };

const scriptFor = (frag: string) => document.querySelector<HTMLScriptElement>(`script[src*="${frag}"]`);

beforeEach(() => {
  vi.clearAllMocks();
  document.head.querySelectorAll('script').forEach((s) => s.remove());
  delete (window as W).turnstile;
  delete (window as W).grecaptcha;
});
afterEach(() => vi.useRealTimers());

describe('Captcha', () => {
  it('turnstile: loads only the vendor script, renders with the site key from config, reports tokens', async () => {
    api.getCaptchaConfig.mockResolvedValue({ provider: 'turnstile', siteKey: 'SITE', challengeUrl: null });
    const onToken = vi.fn();
    let opts: Record<string, (t?: string) => void> & { sitekey?: string } = {};
    (window as W).turnstile = { render: vi.fn((_el: unknown, o: typeof opts) => { opts = o; return 'w1'; }), remove: vi.fn(), reset: vi.fn() };
    render(<Captcha onToken={onToken} />);
    const s = await waitFor(() => { const el = scriptFor('challenges.cloudflare.com'); expect(el).toBeTruthy(); return el!; });
    expect(scriptFor('google.com/recaptcha')).toBeNull();
    fireEvent.load(s);
    await waitFor(() => expect(opts.sitekey).toBe('SITE'));
    act(() => opts['callback']!('tok-1'));
    expect(onToken).toHaveBeenLastCalledWith('tok-1');
    act(() => opts['expired-callback']!());
    expect(onToken).toHaveBeenLastCalledWith(null);
  });

  it('recaptcha: loads the Google script and executes with the configured key', async () => {
    api.getCaptchaConfig.mockResolvedValue({ provider: 'recaptcha', siteKey: 'RKEY', challengeUrl: null });
    const onToken = vi.fn();
    const execute = vi.fn(() => Promise.resolve('r-tok'));
    (window as W).grecaptcha = { ready: (cb: () => void) => cb(), execute };
    render(<Captcha onToken={onToken} />);
    const s = await waitFor(() => { const el = scriptFor('google.com/recaptcha'); expect(el).toBeTruthy(); return el!; });
    expect(s.src).toContain('render=RKEY');
    expect(scriptFor('challenges.cloudflare.com')).toBeNull();
    fireEvent.load(s);
    await waitFor(() => expect(onToken).toHaveBeenLastCalledWith('r-tok'));
    expect(execute).toHaveBeenCalledWith('RKEY', { action: 'dsar_request' });
  });

  it('altcha: solves the proof of work with no third-party script', async () => {
    const salt = 'abc';
    const digest = async (n: number) =>
      Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(salt + n))), (b) => b.toString(16).padStart(2, '0')).join('');
    api.getCaptchaConfig.mockResolvedValue({ provider: 'altcha', siteKey: null, challengeUrl: '/api/v1/public/privacy-requests/altcha-challenge' });
    api.getAltchaChallenge.mockResolvedValue({ algorithm: 'SHA-256', challenge: await digest(7), maxnumber: 50, salt, signature: 'sig' });
    const onToken = vi.fn();
    render(<Captcha onToken={onToken} />);
    await waitFor(() => expect(onToken.mock.calls.some((c) => typeof c[0] === 'string')).toBe(true));
    const payload = JSON.parse(atob(onToken.mock.calls.at(-1)![0]));
    expect(payload).toEqual({ algorithm: 'SHA-256', challenge: await digest(7), number: 7, salt, signature: 'sig' });
    expect(document.querySelectorAll('script').length).toBe(0);
  });

  it('altcha solver reports an unsolvable challenge', async () => {
    await expect(solveAltcha({ algorithm: 'SHA-256', challenge: 'zz', maxnumber: 3, salt: 's', signature: 'x' }, () => false)).rejects.toThrow();
  });

  it.each([
    ['config 503', () => api.getCaptchaConfig.mockRejectedValue({ status: 503 })],
    ['unknown provider', () => api.getCaptchaConfig.mockResolvedValue({ provider: 'mystery', siteKey: 'k', challengeUrl: null })],
    ['turnstile without a site key', () => api.getCaptchaConfig.mockResolvedValue({ provider: 'turnstile', siteKey: null, challengeUrl: null })],
  ])('fails closed on %s', async (_n, setup) => {
    setup();
    const onToken = vi.fn();
    render(<Captcha onToken={onToken} />);
    expect(await screen.findByTestId('captcha-unavailable')).toBeTruthy();
    expect(onToken.mock.calls.every((c) => c[0] === null)).toBe(true);
    expect(document.querySelectorAll('script').length).toBe(0);
  });

  it('fails closed when the vendor script cannot load', async () => {
    api.getCaptchaConfig.mockResolvedValue({ provider: 'turnstile', siteKey: 'S', challengeUrl: null });
    render(<Captcha onToken={vi.fn()} />);
    const s = await waitFor(() => { const el = scriptFor('challenges.cloudflare.com'); expect(el).toBeTruthy(); return el!; });
    fireEvent.error(s);
    expect(await screen.findByTestId('captcha-unavailable')).toBeTruthy();
  });

  it('fails closed (and drops the script) when the vendor script never loads', async () => {
    api.getCaptchaConfig.mockResolvedValue({ provider: 'turnstile', siteKey: 'S', challengeUrl: null });
    const onToken = vi.fn();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<Captcha onToken={onToken} />);
    await waitFor(() => expect(scriptFor('challenges.cloudflare.com')).toBeTruthy());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_500);
    });
    vi.useRealTimers();
    expect(await screen.findByTestId('captcha-unavailable')).toBeTruthy();
    expect(scriptFor('challenges.cloudflare.com')).toBeNull();
    expect(onToken).toHaveBeenLastCalledWith(null);
  });
});
