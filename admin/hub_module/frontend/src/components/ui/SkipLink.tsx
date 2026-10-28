import { MAIN_CONTENT_ID } from './constants';

/**
 * "Skip to main content" link: the first tab stop on every page, visible only
 * when focused. Lets keyboard users bypass the header and sidebar (WCAG 2.4.1).
 */
export function SkipLink() {
  return (
    <a
      href={`#${MAIN_CONTENT_ID}`}
      className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[100] focus:rounded-control focus:bg-accent focus:px-4 focus:py-2 focus:text-sm focus:font-semibold focus:text-fg-inverse focus:shadow-overlay"
    >
      Skip to main content
    </a>
  );
}
