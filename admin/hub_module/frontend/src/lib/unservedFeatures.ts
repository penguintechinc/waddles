/**
 * UI features whose hub-api route does not exist yet (the Node -> Python port is
 * incomplete). Each entry is rendered as a visible "not available yet" state via
 * `<ServedFeature>` / `<NotAvailableYet>` instead of calling a dead endpoint and
 * failing with a 404 or a silently empty page.
 *
 * Rules:
 *  - Only list a feature when NO served route can back it. Never invent a route.
 *  - `endpoints` use `METHOD /api/v1/path/:param` and must all be unserved: the
 *    guard test `scripts/__tests__/unserved-registry.test.mjs` fails the moment
 *    hub-api starts serving one, telling you to wire the UI and delete the entry.
 *  - Deleting an entry is what switches the gated UI back on -- no other change.
 */
export interface UnservedFeature {
  /** Human name used in the "not available yet" heading. */
  label: string;
  /** hub-api calls the UI needs that no blueprint serves. */
  endpoints: readonly string[];
}

const community = '/api/v1/admin/:communityId';

export const UNSERVED_FEATURES = {
  'mirror-groups': {
    label: 'Mirror groups',
    endpoints: [
      `GET ${community}/mirror-groups`,
      `POST ${community}/mirror-groups`,
      `GET ${community}/mirror-groups/:groupId`,
      `PUT ${community}/mirror-groups/:groupId`,
      `DELETE ${community}/mirror-groups/:groupId`,
      `POST ${community}/mirror-groups/:groupId/members`,
      `PUT ${community}/mirror-groups/:groupId/members/:memberId`,
      `DELETE ${community}/mirror-groups/:groupId/members/:memberId`,
    ],
  },
  'linked-servers': {
    label: 'Linked servers',
    endpoints: [
      `GET ${community}/servers`,
      `PUT ${community}/servers/:serverId`,
      `DELETE ${community}/servers/:serverId`,
      `GET ${community}/server-link-requests`,
      `POST ${community}/server-link-requests`,
      `POST ${community}/server-link-requests/:requestId/approve`,
      `POST ${community}/server-link-requests/:requestId/reject`,
    ],
  },
  'custom-domains': {
    label: 'Custom domains',
    endpoints: [
      `GET ${community}/domains`,
      `POST ${community}/domains`,
      `POST ${community}/domains/:domainId/verify`,
      `DELETE ${community}/domains/:domainId`,
    ],
  },
  'bot-detection': {
    label: 'Bot detection',
    endpoints: [
      `GET ${community}/bot-detection`,
      `POST ${community}/bot-detection/:resultId/review`,
      `POST ${community}/bot-detection/:resultId/mark-reviewed`,
    ],
  },
  'bot-score': {
    label: 'Bot score',
    endpoints: [`GET ${community}/bot-score`],
  },
  'community-analytics': {
    label: 'Community analytics',
    endpoints: [
      `GET ${community}/analytics/basic`,
      `GET ${community}/analytics/health-score`,
      `GET ${community}/analytics/bad-actors`,
      `GET ${community}/analytics/retention`,
    ],
  },
  'ai-researcher': {
    label: 'AI researcher',
    endpoints: [
      `GET ${community}/ai-researcher/config`,
      `PUT ${community}/ai-researcher/config`,
      `GET ${community}/ai-researcher/available-models`,
    ],
  },
  'ai-insights': {
    label: 'AI insights',
    endpoints: [`GET ${community}/ai-insights`, `GET ${community}/ai-insights/:insightId`],
  },
  translation: {
    label: 'Translation',
    endpoints: [`GET ${community}/translation/config`, `PUT ${community}/translation/config`],
  },
  'reputation-config': {
    label: 'Reputation settings',
    endpoints: [`GET ${community}/reputation/config`, `PUT ${community}/reputation/config`],
  },
  'reputation-at-risk': {
    label: 'At-risk members',
    endpoints: [`GET ${community}/reputation/at-risk`],
  },
  'software-discovery': {
    label: 'Software discovery',
    endpoints: [
      'GET /api/v1/superadmin/software/repositories',
      'POST /api/v1/superadmin/software/repositories',
      'POST /api/v1/superadmin/software/repositories/:id/scan',
      'DELETE /api/v1/superadmin/software/repositories/:id',
    ],
  },
  'service-discovery': {
    label: 'Service discovery',
    endpoints: [
      'GET /api/v1/superadmin/services',
      'POST /api/v1/superadmin/services/:id/refresh',
      'POST /api/v1/superadmin/services/refresh-all',
    ],
  },
  'member-profile': {
    label: 'Member profiles',
    endpoints: ['GET /api/v1/public/users/:userId/profile'],
  },
  'member-roster': {
    label: 'Member list',
    endpoints: ['GET /api/v1/communities/:communityId/members'],
  },
  'community-recent-activity': {
    label: 'Recent activity',
    endpoints: ['GET /api/v1/communities/:communityId/activity'],
  },
  'community-self-create': {
    label: 'Community creation',
    endpoints: ['POST /api/v1/communities/create'],
  },
} as const satisfies Record<string, UnservedFeature>;

const REGISTRY: Readonly<Record<string, UnservedFeature>> = UNSERVED_FEATURES;

/** Looks up an unserved feature by key; `undefined` once the entry is deleted. */
export function findUnservedFeature(key: string): UnservedFeature | undefined {
  return Object.prototype.hasOwnProperty.call(REGISTRY, key) ? REGISTRY[key] : undefined;
}

/**
 * True unless `key` is registered as unserved. Keys are plain strings on purpose:
 * deleting a registry entry (the signal that hub-api now serves the feature) must
 * switch the gated UI on without touching, or breaking the type-check of, its call sites.
 */
export function isFeatureServed(key: string): boolean {
  return findUnservedFeature(key) === undefined;
}
