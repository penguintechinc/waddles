import type { SidebarColorConfig } from '@penguintechinc/react-libs';

/**
 * Waddles colors for `@penguintechinc/react-libs` SidebarMenu, expressed in the
 * semantic design tokens so the shared sidebar matches the rest of the app
 * (the library's built-in dark preset is slate + a `primary-600` fill).
 * Every text pairing here is covered by `styles/__tests__/tokens.test.ts`.
 */
export const WADDLES_SIDEBAR_COLORS: SidebarColorConfig = {
  sidebarBackground: 'bg-surface',
  sidebarBorder: 'border-line',
  logoSectionBorder: 'border-line',
  categoryHeaderText: 'text-fg-muted',
  menuItemText: 'text-fg-secondary',
  menuItemHover: 'hover:bg-surface-raised hover:text-fg',
  menuItemActive: 'bg-accent/15',
  menuItemActiveText: 'text-fg-accent',
  collapseIndicator: 'text-fg-muted',
  footerBorder: 'border-line',
  footerButtonText: 'text-fg-secondary',
  footerButtonHover: 'hover:bg-surface-raised hover:text-fg',
  scrollbarTrack: 'bg-surface',
  scrollbarThumb: 'bg-navy-600',
  scrollbarThumbHover: 'hover:bg-navy-500',
};
