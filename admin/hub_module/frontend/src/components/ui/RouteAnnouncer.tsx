import { useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { MAIN_CONTENT_ID } from './constants';

const APP_NAME = 'Waddles';
const POLL_MS = 150;
const MAX_POLLS = 8;

/**
 * Announces client-side navigation to assistive tech and keeps the tab title
 * meaningful. In a single-page app the browser never reloads, so screen readers
 * hear nothing on route change and `document.title` stays static. After each
 * navigation this waits for the page's `<h1>` (lazy chunks and data may delay
 * it), sets `document.title` from it, announces "Navigated to ..." in a polite
 * live region, and moves focus to `<main>` so keyboard users start at the top
 * of the new content. The initial page load is left alone.
 */
export function RouteAnnouncer() {
  const { pathname } = useLocation();
  const [message, setMessage] = useState('');
  const isFirstRender = useRef(true);

  useEffect(() => {
    if (isFirstRender.current) {
      isFirstRender.current = false;
      return undefined;
    }
    let polls = 0;
    let timer = 0;
    const settle = () => {
      const main = document.getElementById(MAIN_CONTENT_ID);
      const heading = main?.querySelector('h1')?.textContent?.trim();
      polls += 1;
      if (!heading && polls < MAX_POLLS) {
        timer = window.setTimeout(settle, POLL_MS);
        return;
      }
      const title = heading || 'New page';
      document.title = `${title} · ${APP_NAME}`;
      setMessage(`Navigated to ${title}`);
      main?.focus({ preventScroll: true });
    };
    timer = window.setTimeout(settle, POLL_MS);
    return () => window.clearTimeout(timer);
  }, [pathname]);

  return (
    <div role="status" aria-live="polite" aria-atomic="true" className="sr-only" data-testid="route-announcer">
      {message}
    </div>
  );
}
