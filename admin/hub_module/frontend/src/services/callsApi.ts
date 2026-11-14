import axios from 'axios';
import { apiClient } from '../lib/apiClient';
import {
  expectRecord,
  isRecord,
  readArray,
  readBool,
  readNumber,
  readOptNumber,
  readOptString,
  readString,
  WireShapeError,
  type WireRecord,
} from '../lib/wire';

// Typed client for community calls / voice rooms, mirroring hub_api/blueprints/v1/calls.py
// (and `openapi/v1.yaml`). hub-api fronts svc-streaming's RoomsService; this module only speaks REST
// to hub-api (the webui holds no media-service ids, secrets or provider logic). Contract:
//   * room addressing is the room's UUID `room_id` -- legacy names are refused with 400 INVALID_ROOM_ID;
//     `label` is the creator-supplied display name, shown instead of any id;
//   * participants/hands carry a `participant_id` UUID plus a hub-resolved `display_name` ("guest" when
//     unknown) -- the UI shows the name, never the id;
//   * flag `waddles.streaming.rtc` OFF answers 402 `FEATURE_NOT_ENABLED`;
//   * errors are `{ success: false, error: { code, message, details? } }` (read `error.message`);
//     503/504 carry `details.retryable`. An error is surfaced as an error, never as an empty list.

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** What a failed calls request means to the UI. */
export type CallsErrorKind =
  | 'feature-disabled'
  | 'unavailable'
  | 'not-found'
  | 'forbidden'
  | 'rate-limited'
  | 'invalid'
  | 'unknown';

/** A calls request failure with the hub-api error envelope already unpacked. */
export class CallsApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly kind: CallsErrorKind;

  constructor(message: string, status: number, code: string, retryable: boolean) {
    super(message);
    this.name = 'CallsApiError';
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.kind = classify(status, code);
  }
}

/** Maps status + code to the UI-facing failure kind. */
function classify(status: number, code: string): CallsErrorKind {
  if (status === 402 || code === 'FEATURE_NOT_ENABLED') return 'feature-disabled';
  if (status === 404) return 'not-found';
  if (status === 403) return 'forbidden';
  if (status === 429) return 'rate-limited';
  if (status === 502 || status === 503 || status === 504) return 'unavailable';
  if (status === 400 || status === 409 || status === 422) return 'invalid';
  return 'unknown';
}

/**
 * Normalises anything a calls request can throw into a `CallsApiError`.
 * HTTP failures read `error.message` / `error.code` / `error.details.retryable` from hub-api's
 * envelope; transport failures (no response) are retryable; shape drift is a non-retryable client error.
 */
export function toCallsError(err: unknown): CallsApiError {
  if (err instanceof CallsApiError) return err;
  if (axios.isAxiosError(err)) {
    const response = err.response;
    if (response === undefined) {
      return new CallsApiError('Could not reach the server. Check your connection and try again.', 0, 'NETWORK_ERROR', true);
    }
    const body: unknown = response.data;
    const envelope = isRecord(body) && isRecord(body['error']) ? body['error'] : undefined;
    const message = envelope && typeof envelope['message'] === 'string' && envelope['message'].trim() !== ''
      ? envelope['message']
      : `The calls service answered with an error (HTTP ${response.status}).`;
    const code = envelope && typeof envelope['code'] === 'string' ? envelope['code'] : `ERROR_${response.status}`;
    const details = envelope && isRecord(envelope['details']) ? envelope['details'] : undefined;
    const retryable = details?.['retryable'] === true || [502, 503, 504].includes(response.status);
    return new CallsApiError(message, response.status, code, retryable);
  }
  const message = err instanceof WireShapeError || err instanceof Error ? err.message : 'Unexpected calls error.';
  return new CallsApiError(message, 0, 'CLIENT_ERROR', false);
}

/** A call room: ids and counts only; `label` is the display name (never show `roomId`). */
export interface CallRoom {
  roomId: string;
  communityId: string;
  label: string | null;
  maxPublishers: number;
  createdAt: number;
  publisherIds: string[];
  subscriberCount: number;
  locked: boolean;
}

/** A room participant: the UUID is for API calls only, `displayName` is what the UI shows. */
export interface CallParticipant {
  participantId: string;
  displayName: string;
  publisher: boolean;
  subscriber: boolean;
  muted: boolean;
  joinedAt: number | null;
}

/** Participants of one room plus its lock state. */
export interface CallParticipantList {
  participants: CallParticipant[];
  locked: boolean;
}

/** One raised hand; the queue is ordered oldest first by hub-api. */
export interface RaisedHand {
  participantId: string;
  displayName: string;
  raisedAt: number;
  acknowledgedAt: number | null;
  acknowledgedBy: string | null;
}

/** What `join` returns. The room token is only for a media client; it is never rendered. */
export interface JoinedRoom {
  token: string;
  url: string;
  whipUrl: string;
  whepUrl: string;
  tokenId: string;
  participantId: string;
  expiresAt: number;
}

/** Placeholder shown when a participant has no resolvable name. */
export const GUEST_LABEL = 'Guest';
/** Placeholder shown when a room has no label. */
export const UNTITLED_ROOM = 'Untitled room';

/** The display title of a room: its label, never its id. */
export function roomTitle(room: Pick<CallRoom, 'label'>): string {
  const label = room.label?.trim();
  return label ? label : UNTITLED_ROOM;
}

/** The display name for a participant or hand; blank names render as a generic label. */
export function personName(displayName: string): string {
  return displayName.trim() === '' ? GUEST_LABEL : displayName;
}

export function parseRoom(raw: unknown): CallRoom {
  const row = expectRecord(raw, 'room');
  return {
    roomId: readString(row, 'room_id', 'room'),
    communityId: readString(row, 'community_id', 'room'),
    label: readOptString(row, 'label'),
    maxPublishers: readNumber(row, 'max_publishers', 'room'),
    createdAt: readNumber(row, 'created_at', 'room'),
    publisherIds: readArray(row, 'publisher_ids', 'room').map((id) => {
      if (typeof id !== 'string') throw new WireShapeError('room', '"publisher_ids" holds a non-string');
      return id;
    }),
    subscriberCount: readNumber(row, 'subscriber_count', 'room'),
    locked: readBool(row, 'locked'),
  };
}

function parseParticipant(raw: unknown): CallParticipant {
  const row = expectRecord(raw, 'participants[]');
  return {
    participantId: readString(row, 'participant_id', 'participants[]'),
    displayName: personName(readString(row, 'display_name', 'participants[]')),
    publisher: readBool(row, 'publisher'),
    subscriber: readBool(row, 'subscriber'),
    muted: readBool(row, 'muted'),
    joinedAt: readOptNumber(row, 'joined_at'),
  };
}

function parseHand(raw: unknown, what = 'raised_hands[]'): RaisedHand {
  const row = expectRecord(raw, what);
  return {
    participantId: readString(row, 'participant_id', what),
    displayName: personName(readString(row, 'display_name', what)),
    raisedAt: readNumber(row, 'raised_at', what),
    acknowledgedAt: readOptNumber(row, 'acknowledged_at'),
    acknowledgedBy: readOptString(row, 'acknowledged_by'),
  };
}

function parseRooms(body: unknown): CallRoom[] {
  return readArray(expectRecord(body, 'rooms'), 'rooms', 'rooms').map(parseRoom);
}

/** Refuses a non-UUID room/participant id before any request is sent (the server would answer 400). */
function uuid(value: string, what: string): string {
  if (!UUID_RE.test(value)) {
    throw new CallsApiError(`${what} must be a UUID`, 400, 'INVALID_ROOM_ID', false);
  }
  return value;
}

/** Runs one request, mapping every failure to a `CallsApiError`. */
async function run<T>(request: () => Promise<T>): Promise<T> {
  try {
    return await request();
  } catch (err) {
    throw toCallsError(err);
  }
}

/** Which route family a call goes through: the admin console or the member voice view. */
export type CallsScope = 'admin' | 'member';

const roomsBase = (scope: CallsScope, communityId: number): string =>
  scope === 'admin' ? `/api/v1/admin/${communityId}/calls/rooms` : `/api/v1/community/${communityId}/interact/voice/rooms`;

const adminRoom = (communityId: number, roomId: string): string => {
  const id = uuid(roomId, 'Room id');
  return `/api/v1/admin/${communityId}/calls/rooms/${id}`;
};

/** The room id, guaranteed to be a UUID (throws a 400-shaped `CallsApiError` otherwise). */
const checkedRoomId = (roomId: string): string => uuid(roomId, 'Room id');

/** `GET .../rooms` (admin or member family): every live room of the community. */
export function fetchRooms(scope: CallsScope, communityId: number): Promise<CallRoom[]> {
  return run(async () => parseRooms((await apiClient.get<unknown>(roomsBase(scope, communityId))).data));
}

/** `POST .../rooms`: the caller-supplied name becomes the room `label`. */
export function createRoom(
  scope: CallsScope,
  communityId: number,
  input: { roomName: string; maxParticipants?: number },
): Promise<CallRoom> {
  return run(async () => {
    const body: { room_name: string; max_participants?: number } = { room_name: input.roomName };
    if (input.maxParticipants !== undefined) body.max_participants = input.maxParticipants;
    const { data } = await apiClient.post<unknown>(roomsBase(scope, communityId), body);
    return parseRoom(data);
  });
}

/** `GET /admin/.../rooms/:id`. */
export function fetchRoom(communityId: number, roomId: string): Promise<CallRoom> {
  return run(async () => parseRoom((await apiClient.get<unknown>(adminRoom(communityId, roomId))).data));
}

/** `DELETE /admin/.../rooms/:id`. */
export function deleteRoom(communityId: number, roomId: string): Promise<void> {
  return run(async () => {
    await apiClient.delete(adminRoom(communityId, roomId));
  });
}

/** `POST /admin/.../rooms/:id/{lock,unlock}`. */
export function setRoomLocked(communityId: number, roomId: string, locked: boolean): Promise<void> {
  return run(async () => {
    await apiClient.post(`${adminRoom(communityId, roomId)}/${locked ? 'lock' : 'unlock'}`);
  });
}

/** `GET /admin/.../rooms/:id/participants`: names resolved by hub-api. */
export function fetchParticipants(communityId: number, roomId: string): Promise<CallParticipantList> {
  return run(async () => {
    const body = expectRecord((await apiClient.get<unknown>(`${adminRoom(communityId, roomId)}/participants`)).data, 'participants');
    return {
      participants: readArray(body, 'participants', 'participants').map(parseParticipant),
      locked: readBool(body, 'locked'),
    };
  });
}

/** `POST /admin/.../rooms/:id/kick` with the participant UUID. */
export function kickParticipant(communityId: number, roomId: string, participantId: string): Promise<void> {
  return run(async () => {
    await apiClient.post(`${adminRoom(communityId, roomId)}/kick`, { identity: uuid(participantId, 'Participant id') });
  });
}

/** `POST /admin/.../rooms/:id/{mute,unmute}`; resolves to the participant's resulting muted state. */
export function setParticipantMuted(
  communityId: number,
  roomId: string,
  participantId: string,
  muted: boolean,
): Promise<boolean> {
  return run(async () => {
    const { data } = await apiClient.post<unknown>(`${adminRoom(communityId, roomId)}/${muted ? 'mute' : 'unmute'}`, {
      identity: uuid(participantId, 'Participant id'),
    });
    return readBool(expectRecord(data, 'participant mute'), 'muted');
  });
}

/** `POST /admin/.../rooms/:id/mute-all`; resolves to how many publishers were muted. */
export function muteAll(communityId: number, roomId: string): Promise<number> {
  return run(async () => {
    const { data } = await apiClient.post<unknown>(`${adminRoom(communityId, roomId)}/mute-all`);
    return readNumber(expectRecord(data, 'mute-all'), 'muted', 'mute-all');
  });
}

/** `GET /admin/.../rooms/:id/raised-hands`: the queue, oldest first. */
export function fetchRaisedHands(communityId: number, roomId: string): Promise<RaisedHand[]> {
  return run(async () => {
    const body = expectRecord((await apiClient.get<unknown>(`${adminRoom(communityId, roomId)}/raised-hands`)).data, 'raised hands');
    return readArray(body, 'raised_hands', 'raised hands').map((hand) => parseHand(hand));
  });
}

/** `POST /admin/.../rooms/:id/acknowledge-hand` (body `user_id` is the participant UUID). */
export function acknowledgeHand(communityId: number, roomId: string, participantId: string): Promise<RaisedHand> {
  return run(async () => {
    const { data } = await apiClient.post<unknown>(`${adminRoom(communityId, roomId)}/acknowledge-hand`, {
      user_id: uuid(participantId, 'Participant id'),
    });
    return parseHand(expectRecord(data, 'acknowledge hand')['hand'], 'hand');
  });
}

/** `POST /admin/.../rooms/:id/reset-hands`; resolves to how many hands were cleared. */
export function resetHands(communityId: number, roomId: string): Promise<number> {
  return run(async () => {
    const { data } = await apiClient.post<unknown>(`${adminRoom(communityId, roomId)}/reset-hands`);
    return readNumber(expectRecord(data, 'reset hands'), 'cleared', 'reset hands');
  });
}

/** `POST /community/.../voice/rooms/:id/join`: registers the caller as a participant and mints a room token. */
export function joinRoom(communityId: number, roomId: string): Promise<JoinedRoom> {
  return run(async () => {
    const id = checkedRoomId(roomId);
    const body = expectRecord((await apiClient.post<unknown>(`/api/v1/community/${communityId}/interact/voice/rooms/${id}/join`)).data, 'join');
    return {
      token: readString(body, 'token', 'join'),
      url: readString(body, 'url', 'join'),
      whipUrl: readString(body, 'whip_url', 'join'),
      whepUrl: readString(body, 'whep_url', 'join'),
      tokenId: readString(body, 'token_id', 'join'),
      participantId: readString(body, 'participant_id', 'join'),
      expiresAt: readNumber(body, 'expires_at', 'join'),
    };
  });
}

/** `POST /community/.../voice/rooms/:id/leave`. */
export function leaveRoom(communityId: number, roomId: string): Promise<void> {
  return run(async () => {
    const id = checkedRoomId(roomId);
    await apiClient.post(`/api/v1/community/${communityId}/interact/voice/rooms/${id}/leave`);
  });
}

/** `POST /community/.../voice/rooms/:id/raise-hand`: idempotent, the original queue spot is kept. */
export function raiseHand(communityId: number, roomId: string): Promise<RaisedHand> {
  return run(async () => {
    const id = checkedRoomId(roomId);
    const { data } = await apiClient.post<unknown>(`/api/v1/community/${communityId}/interact/voice/rooms/${id}/raise-hand`);
    return parseHand(expectRecord(data, 'raise hand')['hand'], 'hand');
  });
}

/** `POST /community/.../voice/rooms/:id/lower-hand`; `lowered` is false when no hand was raised. */
export function lowerHand(communityId: number, roomId: string): Promise<boolean> {
  return run(async () => {
    const id = checkedRoomId(roomId);
    const { data } = await apiClient.post<unknown>(`/api/v1/community/${communityId}/interact/voice/rooms/${id}/lower-hand`);
    return readBool(expectRecord(data, 'lower hand'), 'lowered');
  });
}

export type { WireRecord };
