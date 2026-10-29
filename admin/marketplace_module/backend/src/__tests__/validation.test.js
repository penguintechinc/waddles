import { describe, expect, it, jest } from '@jest/globals';
import { sanitizeBody, validateRequest, validators } from '../middleware/validation.js';

async function run(chains, body) {
  const req = { body };
  for (const c of [].concat(chains)) await c.run(req);
  return req;
}

function check(req) {
  const next = jest.fn();
  validateRequest(req, {}, next);
  return next.mock.calls[0][0];
}

describe('sanitizeBody', () => {
  it('strips script tags from string fields only', () => {
    const req = { body: { a: '<script>alert(1)</script>hi', n: 5 } };
    const next = jest.fn();
    sanitizeBody(req, {}, next);
    expect(req.body.a).not.toContain('<script>');
    expect(req.body.n).toBe(5);
    expect(next).toHaveBeenCalled();
  });

  it('tolerates missing bodies', () => {
    const next = jest.fn();
    sanitizeBody({}, {}, next);
    expect(next).toHaveBeenCalledTimes(1);
  });
});

describe('validators', () => {
  it('text: required/length/type', async () => {
    expect(check(await run(validators.text('f', { required: true, max: 3 }), { f: '' }))).toMatchObject({ statusCode: 400 });
    expect(check(await run(validators.text('f', { max: 3 }), { f: 'abcd' })).message).toContain('between');
    expect(check(await run(validators.text('f'), { f: 5 })).message).toContain('string');
    expect(check(await run(validators.text('f', { max: 5 }), { f: ' ab ' }))).toBeUndefined();
  });

  it('integer: bounds messages', async () => {
    const v = validators.integer('n', { min: 1, max: 5, required: true });
    expect(check(await run(v, { n: 9 })).message).toContain('<= 5');
    expect(check(await run(v, { n: 0 })).message).toContain('>= 1');
    expect(check(await run(v, { n: '' })).message).toContain('required');
    expect(check(await run(v, { n: 3 }))).toBeUndefined();
    expect(check(await run(validators.integer('n'), { n: 'x' })).message).toBe('n must be an integer');
  });

  it.each([
    ['positiveInteger', -1, 2, 'positive integer'],
    ['boolean', 'maybe', true, 'boolean'],
    ['url', 'nope', 'https://example.com', 'valid URL'],
    ['email', 'nope', 'a@b.com', 'valid email'],
    ['jsonString', '{bad', '{"a":1}', 'valid JSON'],
    ['arrayOfStrings', 'x', ['a'], 'array'],
  ])('%s validates bad and good values', async (name, bad, good, msg) => {
    const v = validators[name]('f');
    expect(check(await run(v, { f: bad })).message).toContain(msg);
    expect(check(await run(v, { f: good }))).toBeUndefined();
    expect(check(await run(validators[name]('f', true), { f: '' })).message).toContain('required');
    expect(check(await run(v, {}))).toBeUndefined();
  });

  it('arrayOfStrings rejects mixed arrays', async () => {
    expect(check(await run(validators.arrayOfStrings('f'), { f: ['a', 1] })).message).toContain('array of strings');
  });
});
