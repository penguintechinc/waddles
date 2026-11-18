/**
 * Minimal standalone layout for the public privacy pages: no navigation, no auth chrome,
 * single readable column that works at phone width.
 */
export default function PublicPrivacyShell({ title, children }) {
  return (
    <div className="min-h-screen bg-surface text-fg">
      <main className="mx-auto w-full max-w-xl px-4 py-8 sm:py-12">
        <h1 className="text-2xl font-bold text-gold-400 sm:text-3xl">{title}</h1>
        <div className="mt-6 space-y-5">{children}</div>
      </main>
    </div>
  );
}
