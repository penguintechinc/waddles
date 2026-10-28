/**
 * Tests for the `services/api.js` helpers that the table-driven contract
 * tests cannot express: multipart uploads, default-valued parameters,
 * URL-encoded path segments and query-string builders.
 */
import type { InternalAxiosRequestConfig } from 'axios';
import { afterAll, beforeEach, describe, expect, it } from 'vitest';

import api, { adminApi, analyticsApi, bundleApi, userApi } from '../api';
import { recordRequests } from '../../test/apiContract';

const rec = recordRequests(api);
afterAll(rec.restore);
beforeEach(() => {
  rec.calls.length = 0;
});

function lastCall(): InternalAxiosRequestConfig {
  const call = rec.calls[rec.calls.length - 1];
  if (!call) throw new Error('no request was recorded');
  return call;
}

function formEntry(config: InternalAxiosRequestConfig, key: string): ReturnType<FormData['get']> {
  if (!(config.data instanceof FormData)) throw new Error('request body is not FormData');
  return config.data.get(key);
}

describe('multipart upload helpers', () => {
  const file = new File(['png-bytes'], 'pic.png', { type: 'image/png' });

  it.each([
    ['uploadCommunityLogo', () => adminApi.uploadCommunityLogo(7, file), '/api/v1/admin/7/logo', 'logo'],
    ['uploadCommunityBanner', () => adminApi.uploadCommunityBanner(7, file), '/api/v1/admin/7/banner', 'banner'],
    ['uploadAvatar', () => userApi.uploadAvatar(file), '/api/v1/user/profile/avatar', 'avatar'],
  ])('%s POSTs the file as a multipart %s field', async (_name, call, url, field) => {
    await call();

    const sent = lastCall();
    expect(sent.method).toBe('post');
    expect(sent.url).toBe(url);
    expect(sent.headers.get('Content-Type')).toBe('multipart/form-data');
    const entry = formEntry(sent, field);
    if (!(entry instanceof File)) throw new Error(`${field} is not a File entry`);
    expect(entry.name).toBe('pic.png');
  });

  it('bundleApi.createVersion POSTs the caller-built FormData to the encoded app path', async () => {
    const fd = new FormData();
    fd.append('manifest', 'name: demo');

    await bundleApi.createVersion('waddles.integrations.vendor/42', fd);

    const sent = lastCall();
    expect(sent.method).toBe('post');
    expect(sent.url).toBe('/api/v1/apps/waddles.integrations.vendor%2F42/versions');
    expect(sent.data).toBe(fd);
    expect(sent.headers.get('Content-Type')).toBe('multipart/form-data');
  });
});

describe('default-valued parameters', () => {
  it('verifyTicket defaults perform_checkin to true and honours false', async () => {
    await adminApi.verifyTicket('TKT-1');
    expect(JSON.parse(String(lastCall().data))).toEqual({ ticket_code: 'TKT-1', perform_checkin: true });

    await adminApi.verifyTicket('TKT-1', false);
    expect(JSON.parse(String(lastCall().data))).toEqual({ ticket_code: 'TKT-1', perform_checkin: false });
  });

  it('exportAttendance defaults to json and passes an explicit format', async () => {
    await adminApi.exportAttendance(3, 9);
    expect(lastCall().params).toEqual({ format: 'json' });
    expect(lastCall().url).toBe('/api/v1/admin/3/calendar/events/9/attendance/export');

    await adminApi.exportAttendance(3, 9, 'csv');
    expect(lastCall().params).toEqual({ format: 'csv' });
  });

  it('getPlatformGrowth defaults to a 30d period and honours an override', async () => {
    await analyticsApi.getPlatformGrowth();
    expect(lastCall().url).toBe('/api/v1/analytics/platform/growth?period=30d');

    await analyticsApi.getPlatformGrowth('7d');
    expect(lastCall().url).toBe('/api/v1/analytics/platform/growth?period=7d');
  });

  it('getCommunityHealth defaults to 50 rows and honours an override', async () => {
    await analyticsApi.getCommunityHealth();
    expect(lastCall().url).toBe('/api/v1/analytics/platform/community-health?limit=50');

    await analyticsApi.getCommunityHealth(5);
    expect(lastCall().url).toBe('/api/v1/analytics/platform/community-health?limit=5');
  });
});

describe('path-segment encoding', () => {
  it('encodes call-room names that contain spaces and slashes', async () => {
    await adminApi.getCallRoom(7, 'team room/1');
    expect(lastCall().url).toBe('/api/v1/admin/7/calls/rooms/team%20room%2F1');

    await adminApi.kickCallParticipant(7, 'team room/1', 'user-9');
    expect(lastCall().url).toBe('/api/v1/admin/7/calls/rooms/team%20room%2F1/kick');
    expect(JSON.parse(String(lastCall().data))).toEqual({ identity: 'user-9' });
  });

  it('encodes both bundle app id and version', async () => {
    await bundleApi.getPermissions('a/b', '1.0.0+build 5');
    expect(lastCall().url).toBe('/api/v1/apps/a%2Fb/versions/1.0.0%2Bbuild%205/permissions');
  });
});
