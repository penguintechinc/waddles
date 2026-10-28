import { describe, expect, it } from 'vitest';
import { COMMUNITY_ROLE_FILTERS, isCommunityAdminRole, roleLabel, roleTone } from '../communityRoles';
import { formatJoined } from '../memberFormat';

describe('communityRoles', () => {
  it('knows which roles may administer', () => {
    expect(isCommunityAdminRole('community-owner')).toBe(true);
    expect(isCommunityAdminRole('moderator')).toBe(true);
    expect(isCommunityAdminRole('vip')).toBe(false);
    expect(isCommunityAdminRole(undefined)).toBe(false);
  });

  it('labels and tones roles (never colour-only: the label always accompanies the tone)', () => {
    expect(roleLabel('community-admin')).toBe('Admin');
    expect(roleLabel('vip')).toBe('Vip');
    expect(roleLabel('community-')).toBe('Member');
    expect(roleTone('community-owner')).toBe('gold');
    expect(roleTone('community-admin')).toBe('sky');
    expect(roleTone('moderator')).toBe('sky');
    expect(roleTone('member')).toBe('gray');
  });

  it('offers exact single-role filters only (the server matches role exactly)', () => {
    expect(COMMUNITY_ROLE_FILTERS.every((f) => !f.value.includes(','))).toBe(true);
  });
});

describe('formatJoined', () => {
  it('formats valid dates and says Unknown otherwise', () => {
    expect(formatJoined('2026-01-05T12:00:00Z')).toBe('Jan 5, 2026');
    expect(formatJoined(null)).toBe('Unknown');
    expect(formatJoined('not a date')).toBe('Unknown');
  });
});
