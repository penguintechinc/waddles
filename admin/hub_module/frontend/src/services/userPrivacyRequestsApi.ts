import { apiClient } from '../lib/apiClient';

/** Typed client for the signed-in user's own privacy requests (`/api/v1/user/me/privacy-requests`). */

export interface UserPrivacyRequest {
  id: string;
  requestType: number;
  status: number;
  regime: number;
  requestedAt: string;
  dueAt: string | null;
  completedAt: string | null;
}

export const userPrivacyRequestsApi = {
  /** GET the caller's own requests, newest first. */
  list: (): Promise<UserPrivacyRequest[]> =>
    apiClient.get<{ items: UserPrivacyRequest[] }>('/api/v1/user/me/privacy-requests').then((r) => r.data.items ?? []),

  /** POST a new request for the caller (tenant comes from the token). */
  create: (body: { requestType: number; regime: number }): Promise<UserPrivacyRequest> =>
    apiClient.post<{ request: UserPrivacyRequest }>('/api/v1/user/me/privacy-requests', body).then((r) => r.data.request),
};
