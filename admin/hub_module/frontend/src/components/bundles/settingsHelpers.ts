import type { ErrorSchema } from '@rjsf/utils';
import { BundleAdminError, type ConfigFieldError } from '../../services/bundleAdminApi';

/** TanStack Query keys shared by the settings grid and detail panel. */
export const settingsKeys = {
  descriptor: (c: number, a: string) => ['bundle-admin-descriptor', c, a] as const,
  config: (c: number, a: string) => ['bundle-admin-config', c, a] as const,
  history: (c: number, a: string) => ['bundle-admin-history', c, a] as const,
};

/** Human message for a gateway failure; 404 no-config is handled by the caller, not here. */
export function describeError(err: unknown): string {
  if (err instanceof BundleAdminError) {
    if (err.status === 403) return 'You do not have permission to view or change these settings.';
    if (err.code === 'bundle_not_active') return 'This app is no longer active in this community.';
    return err.message;
  }
  return 'Something went wrong talking to the server. Try again.';
}

/** Display name from an app id (`waddles.<publisher>.<category>.<name>` -> "Name"). */
export function bundleName(appId: string): string {
  const last = appId.split('.').pop() ?? appId;
  return last.replace(/[-_]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Converts hub-api's value-free 422 errors (`{path: "$.a.b", rule}`) into an
 * rjsf `extraErrors` tree so each message renders beside its field. hub-api
 * stays the validation authority; this only places its verdict in the form.
 */
export function toErrorSchema(errors: ConfigFieldError[]): ErrorSchema {
  const root: Record<string, unknown> = {};
  for (const { path, rule } of errors) {
    const segments = path
      .replace(/^\$\.?/, '')
      .split(/\.|\[|\]/)
      .filter(Boolean);
    let node = root;
    for (const seg of segments) {
      node[seg] = (node[seg] as Record<string, unknown>) ?? {};
      node = node[seg] as Record<string, unknown>;
    }
    const list = (node.__errors as string[]) ?? [];
    node.__errors = [...list, `Rejected by server: ${rule}`];
  }
  return root as ErrorSchema;
}
