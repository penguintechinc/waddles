/**
 * Tests for the shared platform registry: lookups for known platforms,
 * the Unknown fallback, and the list/option helpers other screens render from.
 */
import { describe, expect, it } from 'vitest';

import {
  defaultPlatform,
  getAllPlatformOptions,
  getAllPlatforms,
  getPlatformColor,
  getPlatformConfig,
  getPlatformIcon,
  getPlatformLabel,
  platformConfig,
} from '../platformConfig';

describe('platformConfig lookups', () => {
  it.each([
    ['discord', 'Discord', '#5865F2'],
    ['twitch', 'Twitch', '#9146FF'],
    ['kick', 'KICK', '#53FC18'],
    ['hub', 'Hub Chat', '#38BDF8'],
  ])('%s resolves label and full config', (id, label, hex) => {
    expect(getPlatformLabel(id)).toBe(label);
    expect(getPlatformConfig(id)).toBe(platformConfig[id]);
    expect(getPlatformConfig(id).hex).toBe(hex);
    expect(getPlatformIcon(id)).toBe(platformConfig[id].icon);
    expect(getPlatformColor(id)).toBe(platformConfig[id].color);
  });

  it.each([['myspace'], [''], [undefined], [null]])('unknown platform %p falls back to the default', (id) => {
    expect(getPlatformConfig(id)).toBe(defaultPlatform);
    expect(getPlatformLabel(id)).toBe('Unknown');
    expect(getPlatformIcon(id)).toBe(defaultPlatform.icon);
    expect(getPlatformColor(id)).toBe(defaultPlatform.color);
  });
});

describe('platformConfig listings', () => {
  it('getAllPlatforms returns every registered key', () => {
    const keys = getAllPlatforms();
    expect(keys).toEqual(Object.keys(platformConfig));
    expect(keys).toEqual(expect.arrayContaining(['discord', 'twitch', 'slack', 'youtube', 'hub']));
  });

  it('getAllPlatformOptions adds the id to each config entry', () => {
    const options = getAllPlatformOptions();
    expect(options).toHaveLength(getAllPlatforms().length);
    for (const option of options) {
      expect(option).toEqual({ id: option.id, ...platformConfig[option.id] });
      expect(option).toEqual(expect.objectContaining({ icon: expect.any(String), label: expect.any(String), color: expect.any(String), hex: expect.stringMatching(/^#[0-9A-F]{6}$/i) }));
    }
  });
});
