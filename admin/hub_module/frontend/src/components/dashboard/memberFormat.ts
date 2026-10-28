/** `Jan 5, 2026`; "Unknown" when the server sent no (or an unparseable) join date. */
export function formatJoined(iso: string | null): string {
  if (!iso) return 'Unknown';
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? 'Unknown'
    : date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

