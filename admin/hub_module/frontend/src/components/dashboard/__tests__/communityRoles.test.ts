import { describe, expect, it } from 'vitest';
import { roleLabel, roleTone } from '../communityRoles';
import { formatJoined } from '../memberFormat';

describe('communityRoles (display only)', () => {
  it('labels roles (never colour-only: the label always accompanies the tone)', () => {
    expect(roleLabel('community-admin')).toBe('Admin');
    expect(roleLabel('vip')).toBe('Vip');
    expect(roleLabel('community-')).toBe('Member');
  });

  it('tones by role kind code, not by role name', () => {
    expect(roleTone(1)).toBe('sky');
    expect(roleTone(3)).toBe('gray');
    expect(roleTone(null)).toBe('gray');
    expect(roleTone(undefined)).toBe('gray');
  });
});

describe('formatJoined', () => {
  it('formats valid dates and says Unknown otherwise', () => {
    expect(formatJoined('2026-01-05T12:00:00Z')).toBe('Jan 5, 2026');
    expect(formatJoined(null)).toBe('Unknown');
    expect(formatJoined('not a date')).toBe('Unknown');
  });
});
