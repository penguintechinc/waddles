import { useEffect, useState } from 'react';

/**
 * Minimal PostHog-style feature flag hook (S0 foundation).
 *
 * `@penguintechinc/react-libs` 1.3.4 has no PostHog/feature-flag helper
 * (verified against its published dist -- LoginPageBuilder, SidebarMenu,
 * FormBuilder/FormModalBuilder, ConsoleVersion, useBreakpoint only), so this
 * is a from-scratch minimal implementation rather than reaching into a
 * shared package that doesn't have one yet.
 *
 * TODO(hubwebui-s0): wire `resolveFlags()` to a real source once one exists.
 * hub-api has no `/api/v1/*flags*` or PostHog-proxy endpoint today (checked
 * `admin/hub_module/hub_api` -- zero matches for "posthog"/"feature flag").
 * Per critical-rules.md Feature Flags & License Tiers and client.md Secrets
 * & Credentials, prefer having hub-api expose *resolved* flag values (it
 * already holds the tenant/session context) over embedding a PostHog
 * project key in this browser bundle -- a client-side key is not a secret
 * PostHog-side, but resolving server-side keeps tenant targeting rules in
 * one place and matches "never call third-party APIs directly from
 * client". Until that endpoint exists, every flag is unseen and MUST
 * default OFF (never crash, never block render).
 */

type FlagKey = `${string}.${string}`;

const KNOWN_FLAGS: Readonly<Record<string, boolean>> = Object.freeze({});

/**
 * Placeholder resolver -- always returns the empty/unseen map until the
 * hub-api resolved-flags endpoint (TODO above) lands. Kept as a separate
 * function so that wiring it up later is a one-line change here, not a
 * call-site change in every component using the hook.
 */
function resolveFlags(): Readonly<Record<string, boolean>> {
  return KNOWN_FLAGS;
}

/**
 * Returns whether a PostHog-style feature flag is enabled. Defaults to
 * `false` (OFF) for any flag not yet known/resolved -- per house rule,
 * absence of a flag is never treated as "on".
 */
export function useFeatureFlag(key: FlagKey): boolean {
  const [enabled, setEnabled] = useState<boolean>(() => resolveFlags()[key] ?? false);

  useEffect(() => {
    setEnabled(resolveFlags()[key] ?? false);
  }, [key]);

  return enabled;
}
