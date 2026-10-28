import { useId, useRef, type KeyboardEvent, type ReactNode } from 'react';
import { cx } from './cx';

export interface TabItem {
  id: string;
  label: ReactNode;
  /** Optional count/status rendered after the label. */
  badge?: ReactNode;
  disabled?: boolean;
}

export interface TabsProps {
  tabs: readonly TabItem[];
  value: string;
  onChange: (id: string) => void;
  /** Accessible name for the tab list (e.g. "Community settings sections"). */
  label: string;
  /** Renders the active panel; receives the ids to wire `aria-controls`/`aria-labelledby`. */
  children?: ReactNode;
  className?: string;
}

/** Builds the deterministic tab/panel ids from a shared prefix. */
function ids(prefix: string, tabId: string) {
  return { tab: `${prefix}-tab-${tabId}`, panel: `${prefix}-panel-${tabId}` };
}

/**
 * WAI-ARIA tabs with automatic activation: roving tabindex, Left/Right/Home/End
 * keys, `aria-selected`, and a labelled `tabpanel`. Replaces the hand-rolled
 * button rows that had no roles or keyboard model.
 */
export function Tabs({ tabs, value, onChange, label, children, className }: TabsProps) {
  const prefix = useId();
  const listRef = useRef<HTMLDivElement>(null);
  const enabled = tabs.filter((t) => !t.disabled);

  function move(delta: number | 'first' | 'last', currentId: string) {
    const index = enabled.findIndex((t) => t.id === currentId);
    if (index === -1 || enabled.length === 0) return;
    let next: number;
    if (delta === 'first') next = 0;
    else if (delta === 'last') next = enabled.length - 1;
    else next = (index + delta + enabled.length) % enabled.length;
    const target = enabled[next];
    if (!target) return;
    onChange(target.id);
    listRef.current?.querySelector<HTMLElement>(`[id="${ids(prefix, target.id).tab}"]`)?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLButtonElement>, tabId: string) {
    switch (event.key) {
      case 'ArrowRight':
        event.preventDefault();
        move(1, tabId);
        break;
      case 'ArrowLeft':
        event.preventDefault();
        move(-1, tabId);
        break;
      case 'Home':
        event.preventDefault();
        move('first', tabId);
        break;
      case 'End':
        event.preventDefault();
        move('last', tabId);
        break;
      default:
    }
  }

  const active = tabs.find((t) => t.id === value);

  return (
    <div className={className}>
      <div
        ref={listRef}
        role="tablist"
        aria-label={label}
        className="mb-6 flex gap-1 overflow-x-auto border-b border-line"
      >
        {tabs.map((tab) => {
          const selected = tab.id === value;
          const tabIds = ids(prefix, tab.id);
          return (
            <button
              key={tab.id}
              id={tabIds.tab}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-controls={tabIds.panel}
              tabIndex={selected ? 0 : -1}
              disabled={tab.disabled}
              onClick={() => onChange(tab.id)}
              onKeyDown={(e) => onKeyDown(e, tab.id)}
              className={cx(
                '-mb-px inline-flex items-center gap-2 whitespace-nowrap border-b-2 px-3 py-2.5 text-sm font-medium transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus',
                'disabled:cursor-not-allowed disabled:opacity-50',
                selected
                  ? 'border-accent text-fg-accent'
                  : 'border-transparent text-fg-secondary hover:border-line-strong hover:text-fg',
              )}
            >
              {tab.label}
              {tab.badge ? <span className="badge badge-gray">{tab.badge}</span> : null}
            </button>
          );
        })}
      </div>
      {active ? (
        <div
          role="tabpanel"
          id={ids(prefix, active.id).panel}
          aria-labelledby={ids(prefix, active.id).tab}
          tabIndex={0}
          className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2"
        >
          {children}
        </div>
      ) : null}
    </div>
  );
}
