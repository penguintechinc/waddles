import React from 'react';

const GITHUB_PROFILE_PREFIX = 'https://github.com/';

/**
 * Renders a marketplace entry's author. hub-api returns the author as
 * `{display, githubHandle, profileUrl}` with the profile URL derived server-side from a
 * validated handle; this component only renders it (thin presentation) and links GitHub
 * authors to their profile. A plain-string author (legacy/other endpoints) renders as text.
 */
const AuthorLink = ({ author, className = '' }) => {
  if (!author) return null;
  const display = typeof author === 'string' ? author : author.display;
  if (!display) return null;
  const url = typeof author === 'string' ? null : author.profileUrl;
  // Defense in depth: never emit a non-GitHub href even if the payload were tampered with.
  if (url && url.startsWith(GITHUB_PROFILE_PREFIX)) {
    return (
      <a
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        className={`text-sky-400 hover:underline ${className}`}
        data-testid="author-link"
      >
        {display}
      </a>
    );
  }
  return (
    <span className={className} data-testid="author-name">
      {display}
    </span>
  );
};

/** Badge for a bundle's collection (publisher designation), e.g. `superpenguintv`. */
export const CollectionBadge = ({ collection, className = '' }) => {
  if (!collection) return null;
  return (
    <span
      className={`bg-purple-900/40 border border-purple-600 text-purple-300 text-xs px-2 py-1 rounded ${className}`}
      data-testid="collection-badge"
    >
      {collection}
    </span>
  );
};

export default AuthorLink;
