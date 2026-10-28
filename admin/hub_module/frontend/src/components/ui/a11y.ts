import type { KeyboardEvent } from 'react';

/**
 * Keyboard handler that runs `action` on Enter or Space, like a native button.
 * Use on the rare non-`<button>` element that must act as one (`role="button"`
 * + `tabIndex={0}`); it ignores keys bubbling up from focusable children so a
 * nested control keeps its own behaviour.
 */
export function onActivate(action: () => void) {
  return (event: KeyboardEvent<HTMLElement>): void => {
    if (event.target !== event.currentTarget) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      action();
    }
  };
}
