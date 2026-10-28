/**
 * Centralized Waddles theme colors for FormModalBuilder.
 *
 * Usage:
 *   import { WADDLES_COLORS, WADDLES_GOLD_COLORS } from '@/theme/waddlebotTheme';
 *
 *   <FormModalBuilder themeMode="dark" colors={WADDLES_COLORS} ... />
 *
 * WADDLES_COLORS      — navy/sky primary buttons (most pages)
 * WADDLES_GOLD_COLORS — navy/gold primary buttons (superadmin pages)
 */

/**
 * Standard Waddles dark theme -- sky-blue primary buttons.
 *
 * Contrast (WCAG 2.2 AA), measured against the modal surface:
 *  - primary button: white on sky-700 = 5.9:1 (sky-600 was 4.1:1, a fail)
 *  - field border navy-500 on navy-900 = 3.4:1 (control boundary >= 3:1)
 *  - description text navy-300 on navy-900 = 6.8:1
 * `navy-850` (used by the old footer) is not in the palette and rendered unstyled.
 */
export const WADDLES_COLORS = {
  modalBackground: 'bg-navy-900',
  headerBackground: 'bg-navy-900',
  footerBackground: 'bg-navy-900',
  overlayBackground: 'bg-black/60',
  titleText: 'text-sky-100',
  labelText: 'text-sky-100',
  descriptionText: 'text-navy-300',
  errorText: 'text-red-300',
  buttonText: 'text-white',
  fieldBackground: 'bg-navy-950',
  fieldBorder: 'border-navy-500',
  fieldText: 'text-sky-100',
  fieldPlaceholder: 'placeholder-navy-400',
  focusRing: 'focus:ring-gold-500',
  focusBorder: 'focus:border-gold-500',
  primaryButton: 'bg-sky-700',
  primaryButtonHover: 'hover:bg-sky-800',
  secondaryButton: 'bg-navy-800',
  secondaryButtonHover: 'hover:bg-navy-700',
  secondaryButtonBorder: 'border-navy-500',
  activeTab: 'text-gold-400',
  activeTabBorder: 'border-gold-500',
  inactiveTab: 'text-navy-300',
  inactiveTabHover: 'hover:text-sky-100 hover:border-navy-500',
  tabBorder: 'border-navy-700',
  errorTabText: 'text-red-300',
  errorTabBorder: 'border-red-500',
};

/** Gold-accent variant -- gold primary buttons for superadmin panels (navy-950 on gold-500 = 8.3:1) */
export const WADDLES_GOLD_COLORS = {
  ...WADDLES_COLORS,
  titleText: 'text-gold-400',
  labelText: 'text-navy-200',
  buttonText: 'text-navy-950',
  primaryButton: 'bg-gold-500',
  primaryButtonHover: 'hover:bg-gold-400',
  inactiveTabHover: 'hover:text-navy-200',
};
