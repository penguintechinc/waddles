# `src/services/` convention

`api.js` is shared, legacy-pattern (raw axios + hand-written helper objects)
and is used by every existing page. **Do not edit `api.js`** to add new
endpoints for a new feature slice -- every future slice adds its own file
instead:

```
src/services/<domain>Api.ts
```

Rules for a new `<domain>Api.ts`:

- `.ts`, strict mode (see `tsconfig.json`) -- no `any`.
- Built on `src/lib/apiClient.ts` (typed axios instance, same HttpOnly-cookie
  auth as `api.js`), not a new `axios.create()`.
- All server state consumed through TanStack Query (`src/lib/queryClient.ts`)
  -- `useQuery`/`useMutation` wrapping this module's functions, never raw
  `useState`/`useEffect` for API data.
- Request/response types defined in the same file (or a colocated
  `<domain>Api.types.ts`) -- no `unknown`/untyped payloads.
- Tenant-scoped: reuse `AuthContext`, never read/trust a tenant id from a
  route param alone without the session confirming membership.

Example shape:

```ts
// src/services/bundleCatalogApi.ts
import { apiClient } from '../lib/apiClient';

export interface BundleCatalogEntry {
  id: string;
  name: string;
}

export const bundleCatalogApi = {
  list: (tenantId: string) =>
    apiClient.get<BundleCatalogEntry[]>(`/api/v1/tenant/${tenantId}/bundles`),
};
```

Existing pages are converted to this pattern **on touch**, not in bulk --
don't migrate an unrelated page's calls while adding a new domain module.
