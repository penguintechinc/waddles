/** Public privacy form + verify page: captcha gating, uniform success, error mapping, no auto-verify. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

let mockOnToken = null;
vi.mock('../../../components/Captcha', () => ({
  default: ({ onToken, resetKey }) => {
    mockOnToken = onToken;
    return <div data-testid="captcha-mock" data-reset={resetKey} />;
  },
}));
vi.mock('../../../services/publicPrivacyApi', () => ({
  publicPrivacyApi: { submit: vi.fn(), resendVerification: vi.fn(), verify: vi.fn(), previewVerification: vi.fn() },
}));
import { publicPrivacyApi as api } from '../../../services/publicPrivacyApi';
import PublicPrivacyRequest from '../PublicPrivacyRequest';
import PublicPrivacyVerify from '../PublicPrivacyVerify';

const apiErr = (status, code) => ({ status, code, message: 'm' });

function mountForm() {
  render(
    <MemoryRouter initialEntries={['/privacy-request/acme']}>
      <Routes>
        <Route path="/privacy-request/:tenantSlug" element={<PublicPrivacyRequest />} />
      </Routes>
    </MemoryRouter>,
  );
}
const fill = (email = 'me@example.test') => fireEvent.change(screen.getByLabelText(/email address/i), { target: { value: email } });
const submitBtn = () => screen.getByRole('button', { name: 'Submit request' });
const giveToken = (t = 'tok') => { mockOnToken(t); };

beforeEach(() => {
  vi.clearAllMocks();
  mockOnToken = null;
});

describe('public form', () => {
  it('submit is disabled until a captcha token exists (fail closed)', async () => {
    mountForm();
    fill();
    expect(submitBtn().disabled).toBe(true);
    fireEvent.click(submitBtn());
    expect(api.submit).not.toHaveBeenCalled();
    giveToken();
    await waitFor(() => expect(submitBtn().disabled).toBe(false));
  });

  it('sends the form with the token and shows the uniform acceptance message', async () => {
    api.submit.mockResolvedValue({ accepted: true });
    mountForm();
    fill();
    fireEvent.change(screen.getByLabelText('Request type'), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText(/privacy law/i), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText(/message/i), { target: { value: ' hello ' } });
    giveToken('abc');
    await waitFor(() => expect(submitBtn().disabled).toBe(false));
    fireEvent.click(submitBtn());
    const ok = await screen.findByTestId('public-accepted');
    expect(api.submit).toHaveBeenCalledWith('acme', { email: 'me@example.test', requestType: 2, regime: 2, message: 'hello', captchaToken: 'abc' });
    expect(ok.textContent).toMatch(/If the details are valid/);
    expect(ok.textContent).not.toMatch(/acme|me@example/);
  });

  it('rejects a malformed email locally', async () => {
    mountForm();
    fill('nope');
    giveToken();
    await waitFor(() => expect(submitBtn().disabled).toBe(false));
    fireEvent.click(submitBtn());
    expect(await screen.findByText(/valid email/i)).toBeTruthy();
    expect(api.submit).not.toHaveBeenCalled();
  });

  it.each([
    [429, 'DSAR_RATE_LIMITED', /too many requests/i],
    [400, 'DSAR_CAPTCHA_FAILED', /security check failed/i],
    [503, 'DSAR_CAPTCHA_UNAVAILABLE', /temporarily unavailable/i],
    [400, null, /check the details/i],
  ])('maps %s %s to a safe message and resets the captcha', async (status, code, re) => {
    api.submit.mockRejectedValue(apiErr(status, code));
    mountForm();
    fill();
    giveToken();
    await waitFor(() => expect(submitBtn().disabled).toBe(false));
    fireEvent.click(submitBtn());
    expect((await screen.findByTestId('public-error')).textContent).toMatch(re);
    expect(screen.getByTestId('captcha-mock').dataset.reset).toBe('1');
    expect(submitBtn().disabled).toBe(true); // single-use token consumed
  });

  it('error text never reveals tenant or email existence', async () => {
    api.submit.mockRejectedValue(apiErr(400, 'DSAR_UNKNOWN'));
    mountForm();
    fill();
    giveToken();
    await waitFor(() => expect(submitBtn().disabled).toBe(false));
    fireEvent.click(submitBtn());
    const t = (await screen.findByTestId('public-error')).textContent;
    expect(t).not.toMatch(/tenant|exist|acme|me@example/i);
  });

  it('resend uses its own captcha and a uniform message', async () => {
    api.resendVerification.mockResolvedValue({ accepted: true });
    mountForm();
    fireEvent.click(screen.getByRole('button', { name: 'Resend verification email' }));
    const form = screen.getByRole('form', { name: 'Resend verification email' });
    fireEvent.change(form.querySelector('input[type=email]'), { target: { value: 'me@example.test' } });
    expect(form.querySelector('select').value).toBe('1'); // defaults to the main form's type
    const btn = form.querySelector('button[type=submit]');
    expect(btn.disabled).toBe(true);
    giveToken('rt');
    await waitFor(() => expect(btn.disabled).toBe(false));
    fireEvent.click(btn);
    expect((await screen.findByTestId('resend-done')).textContent).toMatch(/If a request of that type is awaiting verification/);
    expect(api.resendVerification).toHaveBeenCalledWith('acme', { email: 'me@example.test', requestType: 1, captchaToken: 'rt' });
  });
});

function mountVerify(url) {
  window.history.replaceState(null, '', url);
  render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/privacy-request/verify" element={<PublicPrivacyVerify />} />
        <Route path="/privacy-request/:tenantSlug" element={<div>form page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

const PREVIEW = {
  valid: true,
  requestType: 2,
  regime: 1,
  message: null,
  contentChanged: false,
  expiresAt: '2030-01-01T00:00:00Z',
};
const confirmBtn = () => screen.findByRole('button', { name: 'Confirm your email' });

describe('verify page', () => {
  beforeEach(() => {
    api.previewVerification.mockResolvedValue(PREVIEW);
  });

  it('previews on load (non-consuming), shows the request type, and posts only on click', async () => {
    api.verify.mockResolvedValue({ verified: true });
    mountVerify('/privacy-request/verify#token=abc123');
    const btn = await confirmBtn();
    expect(api.previewVerification).toHaveBeenCalledWith('abc123');
    expect(screen.getByTestId('verify-preview').textContent).toMatch(/Erasure/);
    await new Promise((r) => setTimeout(r, 50));
    expect(api.verify).not.toHaveBeenCalled(); // never auto-POSTs
    fireEvent.click(btn);
    expect(await screen.findByTestId('verify-ok')).toBeTruthy();
    expect(api.verify).toHaveBeenCalledTimes(1);
    expect(api.verify).toHaveBeenCalledWith('abc123');
  });

  it('shows the draft regime and message as ESCAPED text and flags the change', async () => {
    api.previewVerification.mockResolvedValue({
      ...PREVIEW,
      regime: 2,
      message: '<img src=x onerror=alert(1)> hello',
      contentChanged: true,
    });
    mountVerify('/privacy-request/verify#token=abc123');
    await confirmBtn();
    const box = screen.getByTestId('verify-message');
    expect(box.textContent).toContain('<img src=x onerror=alert(1)> hello');
    expect(box.querySelector('img')).toBeNull(); // rendered as text, never parsed as HTML
    expect(screen.getByText('This link updates your request')).toBeTruthy();
  });

  it('labels the message as unverified third-party text and warns before the confirm button', async () => {
    api.previewVerification.mockResolvedValue({ ...PREVIEW, message: 'call 555-0100 http://x.test', contentChanged: false });
    mountVerify('/privacy-request/verify#token=abc123');
    await confirmBtn();
    expect(screen.getByText('Text submitted with this request by whoever filed it (not verified)')).toBeTruthy();
    expect(screen.queryByText('Your message:')).toBeNull();
    const box = screen.getByTestId('verify-message');
    expect(box.querySelector('a')).toBeNull(); // no auto-linking
    expect(screen.getByTestId('verify-warning').textContent).toBe(
      'If you did not make this request, do not confirm. Close this page and no action will be taken.',
    );
  });

  it('shows no message block or change notice when nothing differs', async () => {
    mountVerify('/privacy-request/verify#token=abc123');
    await confirmBtn();
    expect(screen.queryByTestId('verify-message')).toBeNull();
    expect(screen.queryByText('This link updates your request')).toBeNull();
  });

  it('reads the token from the query string too', async () => {
    api.verify.mockResolvedValue({ verified: true });
    mountVerify('/privacy-request/verify?token=q-tok');
    fireEvent.click(await confirmBtn());
    await waitFor(() => expect(api.verify).toHaveBeenCalledWith('q-tok'));
  });

  it('an expired / used token is caught at preview: no button, a resend path', async () => {
    api.previewVerification.mockRejectedValue(apiErr(400, 'DSAR_VERIFICATION_INVALID'));
    mountVerify('/privacy-request/verify?token=old&tenant=acme');
    const inv = await screen.findByTestId('verify-invalid');
    expect(inv.textContent).toMatch(/expired|already/i);
    expect(inv.querySelector('a').getAttribute('href')).toBe('/privacy-request/acme');
    expect(screen.queryByRole('button', { name: 'Confirm your email' })).toBeNull();
    expect(api.verify).not.toHaveBeenCalled();
  });

  it('a token that dies between preview and click shows the resend path', async () => {
    api.verify.mockRejectedValue(apiErr(400, 'DSAR_VERIFICATION_INVALID'));
    mountVerify('/privacy-request/verify?token=old&tenant=acme');
    fireEvent.click(await confirmBtn());
    expect((await screen.findByTestId('verify-invalid')).querySelector('a').getAttribute('href')).toBe('/privacy-request/acme');
  });

  it('expired token without a tenant explains how to resend', async () => {
    api.previewVerification.mockRejectedValue(apiErr(400, 'DSAR_VERIFICATION_INVALID'));
    mountVerify('/privacy-request/verify?token=old');
    expect((await screen.findByTestId('verify-invalid')).textContent).toMatch(/Resend verification email/);
  });

  it('transient failure on confirm lets the user retry', async () => {
    api.verify.mockRejectedValue(apiErr(503, 'DSAR_UNAVAILABLE'));
    mountVerify('/privacy-request/verify?token=t');
    fireEvent.click(await confirmBtn());
    expect(await screen.findByText(/could not confirm right now/i)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Confirm your email' })).toBeTruthy();
  });

  it('transient failure on preview offers a retry that previews again', async () => {
    api.previewVerification.mockRejectedValueOnce(apiErr(503, 'DSAR_UNAVAILABLE'));
    mountVerify('/privacy-request/verify?token=t');
    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }));
    expect(await confirmBtn()).toBeTruthy();
    expect(api.previewVerification).toHaveBeenCalledTimes(2);
    expect(api.verify).not.toHaveBeenCalled();
  });

  it('missing token shows an error, calls nothing and shows no button', () => {
    mountVerify('/privacy-request/verify');
    expect(screen.queryByRole('button', { name: 'Confirm your email' })).toBeNull();
    expect(screen.getByText(/missing its verification token/i)).toBeTruthy();
    expect(api.previewVerification).not.toHaveBeenCalled();
  });
});
