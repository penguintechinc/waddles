/** New DSAR codes/statuses: constants, error messages and the act/update wire bodies. */
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../lib/apiClient', () => ({ apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn() } }));
import { apiClient } from '../../lib/apiClient';
import { DSAR_ERROR_MESSAGE, DSAR_STALE_CODES, RESULT_STATUS, RESULT_STATUS_LABEL, dsarErrorMessage } from '../dsarConstants';
import { tenantDsarInboxApi } from '../tenantDsarInboxApi';
import { isSuccessStatus } from '../tenantPrivacyApi';

beforeEach(() => vi.clearAllMocks());

describe('dsarConstants', () => {
  it.each(['DSAR_CONFLICT', 'DSAR_SUBJECT_LOCKED', 'DSAR_SUBJECT_OVERRIDE_REQUIRED', 'DSAR_SUBJECT_EMAIL_MISMATCH'])('%s has a specific message', (code) => {
    expect(DSAR_ERROR_MESSAGE[code]).toBeTruthy();
    const e = { response: { status: 409, data: { error: { code, message: 'raw' } } } };
    expect(dsarErrorMessage(e, 'fallback')).toBe(DSAR_ERROR_MESSAGE[code]);
  });

  it('partial is a known, incomplete (never successful) result status', () => {
    expect(RESULT_STATUS.partial).toBe('partial');
    expect(RESULT_STATUS_LABEL['partial']).toBe('Incomplete');
    expect(isSuccessStatus('partial')).toBe(false);
  });

  it('conflict and override-required reload the request', () => {
    expect(DSAR_STALE_CODES).toContain('DSAR_CONFLICT');
    expect(DSAR_STALE_CODES).toContain('DSAR_SUBJECT_OVERRIDE_REQUIRED');
  });
});

describe('tenantDsarInboxApi', () => {
  const resultBody = { request: {}, result: { userId: 'u', status: 'completed', detail: null, data: null, incomplete: null } };

  it('act omits overrideReason unless given', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: resultBody });
    await tenantDsarInboxApi.act('acme', 'r1', 'erase', true);
    expect(vi.mocked(apiClient.post).mock.calls[0]![1]).toEqual({ action: 'erase', confirm: true });
    await tenantDsarInboxApi.act('acme', 'r1', 'erase', true, 'checked');
    expect(vi.mocked(apiClient.post).mock.calls[1]![1]).toEqual({ action: 'erase', confirm: true, overrideReason: 'checked' });
  });

  it('update sends subjectUserId and subjectOverrideReason as given', async () => {
    vi.mocked(apiClient.patch).mockResolvedValue({ data: { request: {} } });
    await tenantDsarInboxApi.update('acme', 'r1', { subjectUserId: 'u', subjectOverrideReason: 'why' });
    expect(vi.mocked(apiClient.patch).mock.calls[0]![1]).toEqual({ subjectUserId: 'u', subjectOverrideReason: 'why' });
  });
});
