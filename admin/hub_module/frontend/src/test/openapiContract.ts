/**
 * OpenAPI-driven contract helpers: resolve a request the client sent to an operation in
 * `openapi/v1.yaml`, check the request body against the spec, and build response fixtures FROM the
 * spec so a client parser that reads a field the spec does not declare (or requires one it marks
 * optional) fails. Pure functions over a parsed spec; `loadSpec` reads the repo's real file.
 */
import { readFileSync } from 'node:fs';
import { load } from 'js-yaml';

/** A loosely-typed JSON-schema / OpenAPI node. */
export type Node = Record<string, unknown>;

/** The parts of an OpenAPI document the helpers use. */
export interface Spec {
  paths: Record<string, Record<string, Node>>;
  components?: { schemas?: Record<string, Node> };
}

function isNode(value: unknown): value is Node {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Parses `openapi/v1.yaml` (path is relative to the frontend package, where vitest runs). */
export function loadSpec(path = '../../../openapi/v1.yaml'): Spec {
  const doc = load(readFileSync(path, 'utf8'));
  if (!isNode(doc) || !isNode(doc['paths'])) throw new Error(`${path} is not an OpenAPI document (no paths)`);
  return doc as unknown as Spec;
}

/** Follows a `$ref` (`#/components/schemas/X`) until a concrete schema node is reached. */
export function deref(spec: Spec, schema: unknown): Node {
  let node = schema;
  for (let hops = 0; hops < 20; hops += 1) {
    if (!isNode(node)) throw new Error('schema is not an object');
    const ref = node['$ref'];
    if (typeof ref !== 'string') return node;
    const name = ref.replace('#/components/schemas/', '');
    node = spec.components?.schemas?.[name];
    if (node === undefined) throw new Error(`unresolved $ref ${ref}`);
  }
  throw new Error('$ref chain too deep');
}

/** A found operation plus the spec path template it came from. */
export interface FoundOperation {
  template: string;
  method: string;
  operation: Node;
}

/** Converts `/a/{id}/b` into an anchored regex matching one concrete URL. */
function templateRegex(template: string): RegExp {
  const escaped = template.replace(/[.*+?^$()|[\]\\]/g, '\\$&').replace(/\{[^}]+\}/g, '[^/]+');
  return new RegExp(`^${escaped}$`);
}

/** Finds the spec operation for a concrete request, or `null` when the path or method is not in the spec. */
export function findOperation(spec: Spec, method: string, url: string): FoundOperation | null {
  const path = url.split('?')[0] ?? url;
  for (const [template, item] of Object.entries(spec.paths)) {
    if (!templateRegex(template).test(path)) continue;
    const operation = item[method.toLowerCase()];
    if (operation !== undefined) return { template, method: method.toUpperCase(), operation };
  }
  return null;
}

/** The JSON schema of a request body, or `null` when the operation declares none. */
export function requestSchema(spec: Spec, operation: Node): Node | null {
  const body = operation['requestBody'];
  if (!isNode(body) || !isNode(body['content'])) return null;
  const json = body['content']['application/json'];
  return isNode(json) ? deref(spec, json['schema']) : null;
}

/** The JSON schema for one response status of an operation. */
export function responseSchema(spec: Spec, operation: Node, status: string): Node {
  const responses = operation['responses'];
  const response = isNode(responses) ? responses[status] : undefined;
  if (!isNode(response) || !isNode(response['content'])) throw new Error(`no ${status} JSON response in the spec`);
  const json = response['content']['application/json'];
  if (!isNode(json)) throw new Error(`no ${status} application/json response in the spec`);
  return deref(spec, json['schema']);
}

/** The first 2xx status the operation documents. */
export function successStatus(operation: Node): string {
  const responses = operation['responses'];
  const status = isNode(responses) ? Object.keys(responses).find((s) => s.startsWith('2')) : undefined;
  if (status === undefined) throw new Error('operation documents no 2xx response');
  return status;
}

/**
 * Checks a request body against the operation's schema. Returns problems (empty = conforms):
 * body sent where none is declared, a declared body missing, a key the schema does not declare,
 * or a required key absent.
 */
export function checkRequestBody(spec: Spec, operation: Node, sent: unknown): string[] {
  const schema = requestSchema(spec, operation);
  const hasBody = sent !== undefined;
  if (schema === null) return hasBody ? ['client sent a body but the spec declares none'] : [];
  if (!hasBody) return ['spec declares a request body but the client sent none'];
  if (!isNode(sent)) return ['request body is not an object'];
  const props = isNode(schema['properties']) ? Object.keys(schema['properties']) : [];
  const required = Array.isArray(schema['required']) ? (schema['required'] as string[]) : [];
  const problems: string[] = [];
  for (const key of Object.keys(sent)) if (!props.includes(key)) problems.push(`body key "${key}" is not in the spec`);
  for (const key of required) if (!(key in sent)) problems.push(`required body key "${key}" was not sent`);
  return problems;
}

/** Builds an instance of `schema`; `all` includes optional properties, otherwise only required ones. */
export function sample(spec: Spec, schema: unknown, all: boolean): unknown {
  const node = deref(spec, schema);
  const choices = node['anyOf'] ?? node['oneOf'];
  if (Array.isArray(choices)) {
    const concrete = choices.find((c) => !(isNode(c) && c['type'] === 'null')) ?? choices[0];
    return sample(spec, concrete, all);
  }
  switch (node['type']) {
    case 'object': {
      const props = isNode(node['properties']) ? node['properties'] : {};
      const required = new Set(Array.isArray(node['required']) ? (node['required'] as string[]) : []);
      const out: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(props)) {
        if (all || required.has(key)) out[key] = sample(spec, value, all);
      }
      return out;
    }
    case 'array':
      return [sample(spec, node['items'], all)];
    case 'integer':
    case 'number':
      return 1;
    case 'boolean':
      return true;
    case 'null':
      return null;
    default:
      return 'sample';
  }
}
