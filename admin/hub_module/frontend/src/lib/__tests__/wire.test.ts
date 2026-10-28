import { describe, expect, it } from 'vitest';
import {
  WireShapeError,
  expectRecord,
  isRecord,
  readArray,
  readBool,
  readNumber,
  readOptNumber,
  readOptString,
  readString,
} from '../wire';

describe('wire narrowing', () => {
  it('isRecord accepts only plain objects', () => {
    expect(isRecord({})).toBe(true);
    for (const value of [null, undefined, [], 'x', 1, true]) expect(isRecord(value)).toBe(false);
  });

  it('expectRecord names what it was reading when the shape is wrong', () => {
    expect(expectRecord({ a: 1 }, 'thing')).toEqual({ a: 1 });
    expect(() => expectRecord([], 'thing')).toThrow('Unexpected response from the server (thing: expected an object).');
    expect(() => expectRecord(null, 'thing')).toThrow(WireShapeError);
  });

  it('required readers throw on missing/mistyped fields', () => {
    expect(readString({ s: 'a' }, 's', 'w')).toBe('a');
    expect(() => readString({ s: 1 }, 's', 'w')).toThrow('"s" is not a string');
    expect(readNumber({ n: 2 }, 'n', 'w')).toBe(2);
    expect(() => readNumber({ n: '2' }, 'n', 'w')).toThrow('"n" is not a number');
    expect(() => readNumber({ n: Number.NaN }, 'n', 'w')).toThrow('"n" is not a number');
    expect(readArray({ a: [1] }, 'a', 'w')).toEqual([1]);
    expect(() => readArray({ a: {} }, 'a', 'w')).toThrow('"a" is not a list');
  });

  it('optional readers return null/defaults instead of throwing', () => {
    expect(readOptString({ s: 'a' }, 's')).toBe('a');
    expect(readOptString({ s: 1 }, 's')).toBeNull();
    expect(readOptString({}, 's')).toBeNull();
    expect(readOptNumber({ n: 3 }, 'n')).toBe(3);
    expect(readOptNumber({ n: Number.POSITIVE_INFINITY }, 'n')).toBeNull();
    expect(readOptNumber({}, 'n')).toBeNull();
    expect(readBool({ b: true }, 'b')).toBe(true);
    expect(readBool({ b: 'yes' }, 'b')).toBe(false);
    expect(readBool({}, 'b', true)).toBe(true);
  });
});
