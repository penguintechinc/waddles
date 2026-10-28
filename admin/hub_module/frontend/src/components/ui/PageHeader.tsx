import { Link } from 'react-router-dom';
import type { ReactNode } from 'react';
import { cx } from './cx';

export interface Crumb {
  label: string;
  /** Omit on the current page; it is rendered as plain text with `aria-current`. */
  to?: string;
}

export interface PageHeaderProps {
  title: string;
  description?: ReactNode;
  /** Page-level actions, right-aligned (wraps below the title on narrow screens). */
  actions?: ReactNode;
  breadcrumbs?: Crumb[];
  className?: string;
}

/**
 * Standard page title block: optional breadcrumb trail, the page's single `h1`,
 * a one-line description and primary actions. Use once per route.
 */
export function PageHeader({ title, description, actions, breadcrumbs, className }: PageHeaderProps) {
  return (
    <header className={cx('mb-8', className)}>
      {breadcrumbs && breadcrumbs.length > 0 ? (
        <nav aria-label="Breadcrumb" className="mb-3">
          <ol className="flex flex-wrap items-center gap-1.5 text-sm text-fg-muted">
            {breadcrumbs.map((crumb, index) => (
              <li key={`${crumb.label}-${index}`} className="flex items-center gap-1.5">
                {index > 0 ? <span aria-hidden="true">/</span> : null}
                {crumb.to ? (
                  <Link to={crumb.to} className="rounded hover:underline">
                    {crumb.label}
                  </Link>
                ) : (
                  <span aria-current="page" className="text-fg-secondary">
                    {crumb.label}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </nav>
      ) : null}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="page-title">{title}</h1>
          {description ? <p className="page-subtitle max-w-2xl">{description}</p> : null}
        </div>
        {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
    </header>
  );
}
