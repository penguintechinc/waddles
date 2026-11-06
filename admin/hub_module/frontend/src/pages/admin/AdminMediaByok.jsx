/**
 * Media (Gemini) BYOK settings page. Gated as unserved in `unservedFeatures.json` for v3.0: the
 * key API (`/api/v1/admin/:communityId/media/byok-keys`) is served, but the UI also needs the
 * spend/usage endpoint, deferred to v3.1.x. Deleting the registry entry switches this page on.
 */
export default function AdminMediaByok() {
  return (
    <section data-testid="admin-media-byok">
      <h1>Media generation key</h1>
      <p>Set your Gemini API key through the API until the settings form ships.</p>
    </section>
  );
}
