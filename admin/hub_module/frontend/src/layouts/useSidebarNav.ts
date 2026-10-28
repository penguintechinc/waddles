import { useMemo } from 'react';
import type { ComponentType } from 'react';
import type { MenuCategory, MenuItem } from '@penguintechinc/react-libs';

/** A nav entry as the layouts declare it: a real route, optionally exact-match only. */
export interface NavEntry {
  name: string;
  href: string;
  icon?: ComponentType<{ className?: string }>;
  /** Highlight only on this exact path (e.g. an "Overview" entry that prefixes every sibling). */
  exact?: boolean;
}

export interface NavGroup {
  key: string;
  header: string;
  collapsible: boolean;
  defaultOpen?: boolean;
  items: NavEntry[];
}

export interface SidebarNavProps {
  categories: MenuCategory[];
  footerItems: MenuItem[];
  currentPath: string;
  onNavigate: (href: string) => void;
}

/** Sentinel href the shared SidebarMenu sees instead of the real route (see hook docs). */
const token = (index: number): string => `nav:${String(index).padStart(3, '0')}|`;

/** True when `pathname` is the entry's route (or, unless exact, a descendant of it). */
function matches(entry: NavEntry, pathname: string): boolean {
  if (pathname === entry.href) return true;
  if (entry.exact) return false;
  return pathname.startsWith(entry.href.endsWith('/') ? entry.href : `${entry.href}/`);
}

/**
 * Adapts layout nav declarations to `@penguintechinc/react-libs` SidebarMenu while
 * fixing its active-item rule. The library marks an item active when
 * `currentPath.startsWith(item.href)`, so a parent route ("/admin/5") highlights
 * on every child page and sibling prefixes ("/loyalty" vs "/loyalty/games") both
 * light up. Here each item gets a non-nested token href, the single best (longest)
 * route match becomes `currentPath`, and `onNavigate` translates tokens back to
 * real routes. Real paths passed to `onNavigate` are forwarded unchanged.
 */
export function useSidebarNav(
  groups: NavGroup[],
  footer: NavEntry[],
  pathname: string,
  navigate: (path: string) => void,
): SidebarNavProps {
  return useMemo(() => {
    const flat: NavEntry[] = [...groups.flatMap((g) => g.items), ...footer];
    const byToken = new Map<string, string>();
    flat.forEach((entry, index) => byToken.set(token(index), entry.href));

    let best = -1;
    flat.forEach((entry, index) => {
      if (!matches(entry, pathname)) return;
      const current = flat[best];
      if (!current || entry.href.length > current.href.length) best = index;
    });

    let cursor = 0;
    const toItem = (entry: NavEntry): MenuItem => ({
      name: entry.name,
      href: token(cursor++),
      ...(entry.icon ? { icon: entry.icon } : {}),
    });
    const categories: MenuCategory[] = groups.map((g) => ({
      key: g.key,
      header: g.header,
      collapsible: g.collapsible,
      ...(g.defaultOpen !== undefined ? { defaultOpen: g.defaultOpen } : {}),
      items: g.items.map(toItem),
    }));
    const footerItems = footer.map(toItem);

    return {
      categories,
      footerItems,
      currentPath: best >= 0 ? token(best) : '',
      onNavigate: (href: string) => navigate(byToken.get(href) ?? href),
    };
  }, [groups, footer, pathname, navigate]);
}
