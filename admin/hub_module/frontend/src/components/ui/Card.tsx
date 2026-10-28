import type { HTMLAttributes, ReactNode } from 'react';
import { cx } from './cx';

export interface CardProps extends HTMLAttributes<HTMLElement> {
  /** Rendered element; use `section`/`article` when the card is a landmark-worthy unit. */
  as?: 'div' | 'section' | 'article';
  /** Adds hover affordance. Only for cards that are (or contain) a single link target. */
  interactive?: boolean;
}

/** Surface container: the default grouping primitive for page content. */
export function Card({ as: Tag = 'div', interactive = false, className, children, ...rest }: CardProps) {
  return (
    <Tag className={cx('card', interactive && 'card-interactive', className)} {...rest}>
      {children}
    </Tag>
  );
}

export interface CardHeaderProps {
  title: ReactNode;
  description?: ReactNode;
  /** Right-aligned controls (buttons, toggles). */
  actions?: ReactNode;
  /** Heading level; defaults to h2. Pick the level that fits the page outline. */
  headingLevel?: 2 | 3 | 4;
  className?: string;
}

/** Titled card header with an optional description and action slot. */
export function CardHeader({ title, description, actions, headingLevel = 2, className }: CardHeaderProps) {
  const Heading = `h${headingLevel}` as const;
  return (
    <div className={cx('card-header flex flex-wrap items-start justify-between gap-3', className)}>
      <div className="min-w-0">
        <Heading className="section-title">{title}</Heading>
        {description ? <p className="mt-0.5 text-sm text-fg-secondary">{description}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </div>
  );
}

/** Padded card content region. */
export function CardBody({ className, children, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cx('card-body', className)} {...rest}>
      {children}
    </div>
  );
}
