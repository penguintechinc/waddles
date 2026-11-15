/**
 * Playwright Global Setup — runs once before all test projects.
 *
 * Checks whether the expected test user exists on the target environment.
 * If not, seeds the database with the admin user and a test community so
 * that E2E tests don't skip due to missing data.
 *
 * Seeding strategies (tried in order):
 *   1. kubectl exec — run seed SQL against the PostgreSQL pod
 *   2. API registration — POST /api/v1/auth/register (if signup enabled)
 *   3. Fail loudly — tell the operator to seed manually
 */
const { execFileSync } = require('child_process');

const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';
// SECURITY (CWE-798): no hardcoded fallback credential. HUB_TEST_EMAIL/PASS
// take priority (CI-supplied secret); otherwise this reuses whatever
// INITIAL_ADMIN_EMAIL/PASSWORD the target environment was actually
// provisioned with (see docker-compose.yml / config/postgres/migrations/
// 081_seed_default_hub_admin.sql). If neither is set, seeding is skipped
// entirely rather than falling back to a known/guessable password.
const TEST_EMAIL = process.env.HUB_TEST_EMAIL || process.env.INITIAL_ADMIN_EMAIL || 'admin@localhost.local';
const TEST_PASS = process.env.HUB_TEST_PASS || process.env.INITIAL_ADMIN_PASSWORD || '';

// K8s context — auto-detect from BASE_URL or explicit env var
function detectK8sContext() {
  if (process.env.K8S_CONTEXT) return process.env.K8S_CONTEXT;
  if (BASE_URL.includes('localhost:3001') || BASE_URL.includes('penguintech.cloud')) return 'dal2-beta';
  return 'local-alpha';
}

async function globalSetup() {
  console.log(`[global-setup] Checking test user at ${BASE_URL}...`);

  if (!TEST_PASS) {
    console.error(
      '[global-setup] WARNING: No test password available (set HUB_TEST_PASS or ' +
      'INITIAL_ADMIN_PASSWORD). Skipping seed -- tests requiring authentication will fail ' +
      'rather than seeding a guessable default credential.'
    );
    return;
  }

  // Try to login — if it succeeds, the user exists and we're good
  const userExists = await checkTestUser();
  if (userExists) {
    console.log('[global-setup] Test user exists — skipping seed.');
    await ensureTestCommunity();
    return;
  }

  console.log('[global-setup] Test user not found — seeding database...');

  // Strategy 1: kubectl exec seed SQL against PostgreSQL pod
  const seeded = await seedViaKubectl();
  if (seeded) {
    console.log('[global-setup] Seed via kubectl succeeded.');
    await ensureTestCommunity();
    return;
  }

  // Strategy 2: Register via API (if signup enabled)
  const registered = await seedViaApi();
  if (registered) {
    console.log('[global-setup] Seed via API registration succeeded.');
    await ensureTestCommunity();
    return;
  }

  console.error(
    '[global-setup] WARNING: Could not seed test user. Tests requiring authentication will fail.\n' +
    '  Run manually: kubectl exec -n waddlebot deploy/waddlebot-postgresql -- psql -U waddlebot -f /docker-entrypoint-initdb.d/seed_admin.sql\n' +
    '  Or: make seed-mock-data'
  );
}

// ---------------------------------------------------------------------------
// Check if test user can login
// ---------------------------------------------------------------------------

async function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function checkTestUser(retries = 2) {
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const resp = await fetch(`${BASE_URL}/api/v1/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: TEST_EMAIL, password: TEST_PASS }),
      });
      if (resp.status === 429) {
        console.log(`[global-setup] Rate limited on checkTestUser (attempt ${attempt + 1}/${retries + 1}), waiting 5s...`);
        if (attempt < retries) { await sleep(5000); continue; }
        return false;
      }
      const data = await resp.json().catch(() => ({}));
      return data.success === true;
    } catch {
      return false;
    }
  }
  return false;
}

// ---------------------------------------------------------------------------
// Seed via kubectl exec (preferred — works for any environment)
// ---------------------------------------------------------------------------

async function seedViaKubectl() {
  const context = detectK8sContext();
  const namespace = 'waddlebot';

  // SECURITY (CWE-798): no precomputed/static hash. The hash is derived
  // in-database via pgcrypto from TEST_PASS (HUB_TEST_PASS/INITIAL_ADMIN_PASSWORD),
  // which is never a hardcoded literal -- see the TEST_PASS guard in globalSetup().
  const sqlLiteral = (s) => `'${String(s).replace(/'/g, "''")}'`;

  const seedSql = [
    `CREATE EXTENSION IF NOT EXISTS pgcrypto;`,
    // Create global tenant (if not exists)
    `INSERT INTO tenants (slug, display_name, is_global) VALUES ('default', 'Waddles', TRUE) ON CONFLICT (slug) DO NOTHING;`,
    // Create admin user
    `INSERT INTO hub_users (email, username, password_hash, is_active, is_super_admin, email_verified, created_at, updated_at)`,
    `VALUES (${sqlLiteral(TEST_EMAIL)}, 'admin', crypt(${sqlLiteral(TEST_PASS)}, gen_salt('bf', 12)), true, true, true, NOW(), NOW())`,
    `ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash, is_super_admin = true, is_active = true, email_verified = true, updated_at = NOW();`,
    // Create global community
    `INSERT INTO communities (name, display_name, description, is_public, is_active, is_global, platform, member_count, tenant_id, created_at)`,
    `VALUES ('waddlebot-global', 'Waddles Global', 'Global community for all users.', true, true, true, 'global', 1, (SELECT id FROM tenants WHERE is_global = TRUE), NOW())`,
    `ON CONFLICT (name) DO UPDATE SET is_global = true, is_active = true, tenant_id = COALESCE(communities.tenant_id, (SELECT id FROM tenants WHERE is_global = TRUE));`,
    // Add admin to global community
    `INSERT INTO community_members (community_id, user_id, role, is_active, joined_at)`,
    `SELECT c.id, u.id, 'admin', true, NOW()`,
    `FROM hub_users u CROSS JOIN communities c`,
    `WHERE u.email = ${sqlLiteral(TEST_EMAIL)} AND c.name = 'waddlebot-global'`,
    `ON CONFLICT (community_id, user_id) DO UPDATE SET role = 'admin', is_active = true;`,
  ].join(' ');

  try {
    // Find the PostgreSQL pod name
    let podName;
    try {
      podName = execFileSync('kubectl', [
        '--context', context, 'get', 'pods', '-n', namespace,
        '-l', 'app=postgresql',
        '-o', 'jsonpath={.items[0].metadata.name}',
      ], { encoding: 'utf-8', timeout: 10000 }).trim();
    } catch {
      // Try alternative label
      try {
        const allPods = execFileSync('kubectl', [
          '--context', context, 'get', 'pods', '-n', namespace,
          '-o', 'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}',
        ], { encoding: 'utf-8', timeout: 10000 });
        podName = allPods.split('\n').find((p) => p.includes('postgres'));
      } catch {
        podName = null;
      }
    }

    if (!podName) {
      console.log('[global-setup] No PostgreSQL pod found — skipping kubectl seed.');
      return false;
    }

    // Execute the seed SQL via kubectl exec
    execFileSync('kubectl', [
      '--context', context, 'exec', '-n', namespace, podName,
      '--', 'psql', '-U', 'waddlebot', '-d', 'waddlebot', '-c', seedSql,
    ], { encoding: 'utf-8', timeout: 30000 });

    // Verify the user was created
    return await checkTestUser();
  } catch (err) {
    console.log(`[global-setup] kubectl seed failed: ${err.message}`);
    return false;
  }
}

// ---------------------------------------------------------------------------
// Seed via API registration (fallback)
// ---------------------------------------------------------------------------

async function seedViaApi() {
  try {
    const resp = await fetch(`${BASE_URL}/api/v1/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email: TEST_EMAIL,
        password: TEST_PASS,
        username: 'admin',
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (data.success) return true;

    // Registration might be disabled — that's OK, not an error
    console.log(`[global-setup] API registration response: ${JSON.stringify(data)}`);
    return false;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Ensure a non-global test community exists (needed by admin/module tests)
// ---------------------------------------------------------------------------

async function ensureTestCommunity() {
  try {
    // Login to get a token
    const loginResp = await fetch(`${BASE_URL}/api/v1/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: TEST_EMAIL, password: TEST_PASS }),
    });
    const loginData = await loginResp.json().catch(() => ({}));
    if (!loginData.success || !loginData.token) return;

    const token = loginData.token;

    // Check if user already has communities
    const myResp = await fetch(`${BASE_URL}/api/v1/community/my`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const myData = await myResp.json().catch(() => ({}));

    // If user has at least one non-global community, we're good
    const communities = myData.data || myData.communities || [];
    const hasNonGlobal = Array.isArray(communities) && communities.some(
      (c) => !c.is_global && c.name !== 'waddlebot-global'
    );

    if (hasNonGlobal) {
      console.log('[global-setup] Test community already exists.');
      return;
    }

    // Create a test community via the API
    console.log('[global-setup] Creating test community...');
    const createResp = await fetch(`${BASE_URL}/api/v1/community/create`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        name: 'e2e-test-community',
        display_name: 'E2E Test Community',
        description: 'Auto-created by E2E global setup for testing.',
        platform: 'discord',
        is_public: true,
      }),
    });
    const createData = await createResp.json().catch(() => ({}));

    if (createData.success || createData.data || createResp.status === 201) {
      console.log('[global-setup] Test community created.');
    } else if (createResp.status === 409 || createData?.error?.code === 'DUPLICATE') {
      console.log('[global-setup] Test community already exists (duplicate).');
    } else {
      console.log(`[global-setup] Community creation response: ${JSON.stringify(createData)}`);
    }
  } catch (err) {
    console.log(`[global-setup] ensureTestCommunity failed: ${err.message}`);
  }
}

module.exports = globalSetup;
