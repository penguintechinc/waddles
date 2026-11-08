/**
 * Tests for the marketplace author link + collection badge: GitHub authors link to the
 * server-derived profile URL, plain names and legacy strings render as text, and a
 * non-GitHub URL is never emitted as an href.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import AuthorLink, { CollectionBadge } from '../AuthorLink';

describe('AuthorLink', () => {
  it('links a GitHub author to its profile in a new tab', () => {
    render(
      <AuthorLink
        author={{
          display: 'penguintechinc',
          githubHandle: 'penguintechinc',
          profileUrl: 'https://github.com/penguintechinc',
        }}
      />,
    );
    const link = screen.getByTestId('author-link');
    expect(link).toHaveAttribute('href', 'https://github.com/penguintechinc');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(link).toHaveTextContent('penguintechinc');
  });

  it('renders a plain-name author as unlinked text', () => {
    render(<AuthorLink author={{ display: 'Jane Doe', githubHandle: null, profileUrl: null }} />);
    expect(screen.getByTestId('author-name')).toHaveTextContent('Jane Doe');
    expect(screen.queryByTestId('author-link')).toBeNull();
  });

  it('renders a legacy string author as text', () => {
    render(<AuthorLink author="Waddles" />);
    expect(screen.getByTestId('author-name')).toHaveTextContent('Waddles');
  });

  it('never emits a non-GitHub href', () => {
    render(<AuthorLink author={{ display: 'x', profileUrl: 'javascript:alert(1)' }} />);
    expect(screen.queryByTestId('author-link')).toBeNull();
    expect(screen.getByTestId('author-name')).toHaveTextContent('x');
  });

  it('renders nothing without an author or display', () => {
    const { container } = render(
      <>
        <AuthorLink author={null} />
        <AuthorLink author={{ display: '' }} />
      </>,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe('CollectionBadge', () => {
  it('shows the collection slug', () => {
    render(<CollectionBadge collection="superpenguintv" />);
    expect(screen.getByTestId('collection-badge')).toHaveTextContent('superpenguintv');
  });

  it('renders nothing without a collection', () => {
    const { container } = render(<CollectionBadge collection={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
