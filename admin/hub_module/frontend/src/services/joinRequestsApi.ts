import { apiClient } from '../lib/apiClient';
import {
  expectRecord,
  readArray,
  readNumber,
  readOptNumber,
  readOptString,
  readString,
} from '../lib/wire';

// Typed client for community join requests, mirroring
// hub_api/blueprints/v1/join_request.py. Paths are the SERVED ones:
//   POST /api/v1/:communityId/join-requests               (member, approval-mode communities only)
//   GET  /api/v1/:communityId/join-requests/mine          (member)
//   GET  /api/v1/admin/:communityId/join-requests         (community admin)
//   PUT  /api/v1/admin/:communityId/join-requests/:id/{approve,reject}
// The member routes have no `/community` segment (an Express quirk preserved by the port),
// and approve/reject are PUT, not POST. hub-api has no direct-join route for open communities.

export type JoinRequestStatus = 'pending' | 'approved' | 'rejected' | 'cancelled';

export interface MyJoinRequest {
  id: number;
  status: string;
  message: string | null;
  createdAt: string;
  reviewedAt: string | null;
}

export interface JoinRequestRow {
  id: number;
  status: string;
  message: string | null;
  createdAt: string;
  username: string;
  email: string | null;
  avatarUrl: string | null;
}

export interface JoinRequestPage {
  requests: JoinRequestRow[];
  total: number;
  page: number;
  totalPages: number;
}

function parseMine(raw: unknown): MyJoinRequest {
  const row = expectRecord(raw, 'join request');
  return {
    id: readNumber(row, 'id', 'join request'),
    status: readString(row, 'status', 'join request'),
    message: readOptString(row, 'message'),
    createdAt: readString(row, 'created_at', 'join request'),
    reviewedAt: readOptString(row, 'reviewed_at'),
  };
}

function parseRow(raw: unknown): JoinRequestRow {
  const row = expectRecord(raw, 'join requests[]');
  return {
    id: readNumber(row, 'id', 'join requests[]'),
    status: readString(row, 'status', 'join requests[]'),
    message: readOptString(row, 'message'),
    createdAt: readString(row, 'created_at', 'join requests[]'),
    username: readOptString(row, 'username') ?? 'Unknown',
    email: readOptString(row, 'email'),
    avatarUrl: readOptString(row, 'avatarUrl'),
  };
}

/** `POST /api/v1/:id/join-requests`; resolves to the pending request that was created/reopened. */
export async function submitJoinRequest(communityId: number, message: string): Promise<MyJoinRequest> {
  const { data } = await apiClient.post<unknown>(`/api/v1/${communityId}/join-requests`, {
    message: message.trim() === '' ? null : message,
  });
  const created = expectRecord(expectRecord(data, 'join request submit')['request'], 'join request submit request');
  return {
    id: readNumber(created, 'id', 'join request submit'),
    status: readString(created, 'status', 'join request submit'),
    message: message.trim() === '' ? null : message,
    createdAt: readString(created, 'created_at', 'join request submit'),
    reviewedAt: null,
  };
}

/** `GET /api/v1/:id/join-requests/mine`; `null` when the caller has never asked to join. */
export async function fetchMyJoinRequest(communityId: number): Promise<MyJoinRequest | null> {
  const { data } = await apiClient.get<unknown>(`/api/v1/${communityId}/join-requests/mine`);
  const request = expectRecord(data, 'my join request')['request'];
  return request === null || request === undefined ? null : parseMine(request);
}

/** `GET /api/v1/admin/:id/join-requests?status=pending`. */
export async function fetchJoinRequests(communityId: number, page = 1, limit = 25): Promise<JoinRequestPage> {
  const { data } = await apiClient.get<unknown>(`/api/v1/admin/${communityId}/join-requests`, {
    params: { status: 'pending', page, limit },
  });
  const body = expectRecord(data, 'join requests');
  const pagination = expectRecord(body['pagination'], 'join requests pagination');
  return {
    requests: readArray(body, 'requests', 'join requests').map(parseRow),
    total: readNumber(pagination, 'total', 'join requests pagination'),
    page: readNumber(pagination, 'page', 'join requests pagination'),
    totalPages: readOptNumber(pagination, 'totalPages') ?? 1,
  };
}

/** `PUT /api/v1/admin/:id/join-requests/:requestId/{approve,reject}`. */
export async function reviewJoinRequest(
  communityId: number,
  requestId: number,
  decision: 'approve' | 'reject',
): Promise<void> {
  await apiClient.put(`/api/v1/admin/${communityId}/join-requests/${requestId}/${decision}`);
}
