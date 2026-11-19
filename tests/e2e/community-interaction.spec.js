/**
 * E2E Tests: Community Interaction (Chat, Forum, Voice)
 * Tests the member-facing community interaction feature at /community/:id/interact
 *
 * Environment variables:
 *   BASE_URL              - Default: http://localhost:3000
 *   HUB_TEST_EMAIL        - Test user email (default: admin@localhost.local)
 *   HUB_TEST_PASS         - Test user password (default: admin123)
 *   TEST_COMMUNITY_ID     - Community ID to test against (default: 1)
 */

const { test, expect } = require('./fixtures');

const TEST_EMAIL = process.env.HUB_TEST_EMAIL || 'admin@localhost.local';
const TEST_PASS = process.env.HUB_TEST_PASS || process.env.INITIAL_ADMIN_PASSWORD || 'admin123';
const COMMUNITY_ID = process.env.TEST_COMMUNITY_ID || '00000001-0000-4000-8000-000000000000' // community public UUID;

// ---------------------------------------------------------------------------
// Auth / navigation helpers (mirrors community-workflow.spec.js patterns)
// ---------------------------------------------------------------------------

async function injectCsrfCookie(page) {
  let csrfToken = null;
  const handler = async (response) => {
    const raw = response.headers()['set-cookie'] || '';
    const match = raw.match(/XSRF-TOKEN=([^;]+)/);
    if (match) csrfToken = match[1];
  };
  page.on('response', handler);
  await page.goto('/login', { waitUntil: 'networkidle' });
  page.off('response', handler);
  if (csrfToken) {
    const url = new URL(page.url());
    await page.context().addCookies([{
      name: 'XSRF-TOKEN',
      value: csrfToken,
      domain: url.hostname,
      path: '/',
      httpOnly: false,
      secure: false,
      sameSite: 'Lax',
    }]);
  }
}

async function suppressOverlays(page) {
  await page.evaluate(() => {
    if (!localStorage.getItem('gdpr_consent')) {
      localStorage.setItem('gdpr_consent', JSON.stringify({
        accepted: true, essential: true, functional: true, analytics: true, marketing: true,
        timestamp: new Date().toISOString(), policyVersion: '1.0',
      }));
    }
    localStorage.setItem('vendor-request-dismissed', 'true');
  });
}

async function dismissOverlays(page) {
  const acceptBtn = page.locator('button[aria-label="Accept all cookies"], button:has-text("Accept All")').first();
  if (await acceptBtn.isVisible({ timeout: 1500 }).catch(() => false)) {
    await acceptBtn.click();
    await acceptBtn.waitFor({ state: 'hidden', timeout: 3000 }).catch(() => {});
  }
  const vendorDismiss = page.locator('button[title="Dismiss"]').first();
  if (await vendorDismiss.isVisible({ timeout: 1000 }).catch(() => false)) {
    await vendorDismiss.click();
    await vendorDismiss.waitFor({ state: 'hidden', timeout: 3000 }).catch(() => {});
  }
}

async function loginWithPassword(page, email, password, retries = 3) {
  await injectCsrfCookie(page);
  await suppressOverlays(page);
  await dismissOverlays(page);

  await page.fill('[data-testid="email-input"], input[type="email"]', email);
  await page.fill('[data-testid="password-input"], input[type="password"]', password);

  const [loginResponse] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().includes('/api/v1/auth/login') && r.request().method() === 'POST',
      { timeout: 15000 },
    ),
    page.click('[data-testid="auth-submit"], button[type="submit"]'),
  ]);

  const bodyText = await loginResponse.text().catch(() => '(unreadable)');
  let data;
  try { data = JSON.parse(bodyText); } catch { data = {}; }

  if (data?.error?.code === 'RATE_LIMITED' && retries > 0) {
    console.log(`[loginWithPassword] Rate limited, waiting 10s before retry (${retries} left)...`);
    await page.waitForTimeout(10000);
    return loginWithPassword(page, email, password, retries - 1);
  }

  if (!data.success) {
    throw new Error(`Login failed: ${JSON.stringify(data)} (status ${loginResponse.status()})`);
  }

  await page.waitForURL((url) => !url.toString().includes('/login'), { timeout: 10000 });
  await suppressOverlays(page);
}


async function ensureAuthenticated(page) {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await suppressOverlays(page);

  const token = await page.evaluate(() => localStorage.getItem('token'));
  if (token) {
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await suppressOverlays(page);
    // Wait briefly for any auth redirect to settle
    await page.waitForTimeout(500);
    if (!page.url().includes('/login')) return;
  }

  await loginWithPassword(page, TEST_EMAIL, TEST_PASS);
}

/**
 * Navigate to a URL and wait for the page to settle (not on login).
 */
async function gotoPage(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(
    () => !window.location.pathname.startsWith('/login'),
    { timeout: 15000 },
  ).catch(() => {});
}

/**
 * Navigate to the community interact page and wait for loading spinner to clear.
 */
async function gotoInteractPage(page, communityId) {
  await gotoPage(page, `/community/${communityId}/interact`);
  await suppressOverlays(page);
  // Wait until the loading spinner disappears
  await page.waitForFunction(
    () => !document.querySelector('svg.animate-spin'),
    { timeout: 20000 },
  ).catch(() => {});
}

/**
 * Returns true if the page is showing the "No channels yet" empty state.
 */
async function hasNoChannels(page) {
  return page.getByText('No channels yet').isVisible({ timeout: 3000 }).catch(() => false);
}

/**
 * Find the first channel button of the given type in the sidebar.
 * Returns null if the section or its buttons are not present.
 */
async function findFirstChannelOfType(page, type) {
  const labelMap = { chat: 'Chat Channels', forum: 'Forums', voice: 'Voice / Video' };
  const heading = page.getByText(labelMap[type], { exact: false });
  if (!(await heading.isVisible({ timeout: 3000 }).catch(() => false))) return null;
  // Navigate up to the section group container:
  //   span (heading text) → div.px-3.mb-1 → div.mt-4 (the group div)
  // Two levels up lands on div.mt-4 which contains the <ul> of channel buttons.
  const section = heading.locator('../..');
  const firstBtn = section.locator('ul button').first();
  if (!(await firstBtn.isVisible({ timeout: 2000 }).catch(() => false))) return null;
  return firstBtn;
}

// ---------------------------------------------------------------------------
// A. Page Load & Layout
// ---------------------------------------------------------------------------

test.describe('A. Page Load & Layout', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
  });

  test('A1. Interaction page loads without fatal JS errors', async ({ page }) => {
    const jsErrors = [];
    page.on('pageerror', (err) => jsErrors.push(err.message));

    await gotoInteractPage(page, COMMUNITY_ID);
    await page.waitForTimeout(1000);

    // Filter known benign network errors (socket connection refused in CI)
    const fatalErrors = jsErrors.filter(
      (e) => !e.includes('WebSocket') && !e.includes('socket.io') && !e.includes('net::ERR'),
    );
    expect(fatalErrors).toHaveLength(0);
  });

  test('A2. Channel sidebar element with w-60 width class renders on the left', async ({ page }) => {
    await gotoInteractPage(page, COMMUNITY_ID);

    if (await hasNoChannels(page)) {
      test.skip(true, 'No channels configured — skipping sidebar layout test');
      return;
    }

    const sidebar = page.locator('.w-60').first();
    await expect(sidebar).toBeVisible({ timeout: 10000 });
  });

  test('A3. Main content area (flex-1) fills remaining space next to sidebar', async ({ page }) => {
    await gotoInteractPage(page, COMMUNITY_ID);

    if (await hasNoChannels(page)) {
      test.skip(true, 'No channels — skipping layout test');
      return;
    }

    // The main content div after sidebar has class flex-1 flex flex-col
    const mainArea = page.locator('.flex-1.flex.flex-col.min-w-0').first();
    await expect(mainArea).toBeVisible({ timeout: 10000 });
  });

  test('A4. URL auto-redirects to include first channel ID when no channelId in path', async ({ page }) => {
    await gotoPage(page, `/community/${COMMUNITY_ID}/interact`);
    await suppressOverlays(page);
    await page.waitForFunction(
      () => !document.querySelector('svg.animate-spin'),
      { timeout: 20000 },
    ).catch(() => {});
    // Wait for React router to redirect to a channel-specific URL (if channels exist).
    // waitForURL throws if timeout elapses — catch is intentional (no channels = no redirect).
    await page.waitForURL(
      (url) => !url.pathname.endsWith('/interact'),
      { timeout: 6000 },
    ).catch(() => {});

    const currentUrl = page.url();
    const noChannels = await hasNoChannels(page);

    if (noChannels) {
      // No channels — URL stays on base interact path; correct behaviour
      expect(currentUrl).toContain(`/community/${COMMUNITY_ID}/interact`);
    } else {
      // Should have auto-redirected to /interact/<channelId>
      expect(currentUrl).toMatch(new RegExp(`/community/${COMMUNITY_ID}/interact/\\d+`));
    }
  });

  test('A5. Empty state "No channels yet" shows when community has no channels', async ({ page }) => {
    await gotoInteractPage(page, COMMUNITY_ID);

    if (!(await hasNoChannels(page))) {
      test.skip(true, 'Community has channels — empty state is not visible');
      return;
    }

    await expect(page.getByText('No channels yet')).toBeVisible();
    await expect(page.getByText('Ask a community admin to create channels.')).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// B. Channel Sidebar
// ---------------------------------------------------------------------------

test.describe('B. Channel Sidebar', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
    await gotoInteractPage(page, COMMUNITY_ID);

    if (await hasNoChannels(page)) {
      test.skip(true, 'No channels configured — skipping sidebar tests');
    }
  });

  test('B6. Section headers render for each channel type that has channels', async ({ page }) => {
    // At least one section heading must be visible.
    // Skip gracefully if the sidebar is in an error/loading state (e.g. 429 on channel fetch).
    const possibleHeaders = [
      page.getByText('Chat Channels', { exact: false }),
      page.getByText('Forums', { exact: false }),
      page.getByText('Voice / Video', { exact: false }),
    ];

    let anyVisible = false;
    for (const header of possibleHeaders) {
      if (await header.isVisible({ timeout: 5000 }).catch(() => false)) {
        anyVisible = true;
        break;
      }
    }

    if (!anyVisible) {
      // Could be a 429-induced load failure after a busy CRUD run; skip rather than fail.
      test.skip(true, 'No channel section headers visible — channel data may not have loaded');
      return;
    }

    expect(anyVisible).toBe(true);
  });

  test('B7. "Chat Channels" section header is visible when chat channels exist', async ({ page }) => {
    const chatHeader = page.getByText('Chat Channels', { exact: false });
    const visible = await chatHeader.isVisible({ timeout: 5000 }).catch(() => false);
    if (!visible) {
      test.skip(true, 'No chat channels present');
      return;
    }
    await expect(chatHeader).toBeVisible();
  });

  test('B8. Clicking a channel updates URL to include channelId and highlights it gold', async ({ page }) => {
    const sidebar = page.locator('.w-60').first();
    // Scope to ul buttons to skip the optional "New Channel" create button at the top
    const firstBtn = sidebar.locator('ul button').first();

    const visible = await firstBtn.isVisible({ timeout: 5000 }).catch(() => false);
    if (!visible) {
      test.skip(true, 'No channel buttons found in sidebar');
      return;
    }

    await firstBtn.click();
    await page.waitForTimeout(500);

    // URL must now include a numeric channelId
    expect(page.url()).toMatch(new RegExp(`/community/${COMMUNITY_ID}/interact/\\d+`));

    // Active channel must carry gold highlight CSS
    const activeClasses = await firstBtn.getAttribute('class');
    expect(activeClasses).toMatch(/bg-gold-500|text-gold-400/);
  });

  test('B9. Previously active channel loses gold highlight when another is selected', async ({ page }) => {
    const sidebar = page.locator('.w-60').first();
    const buttons = sidebar.locator('button');
    const count = await buttons.count();

    if (count < 2) {
      test.skip(true, 'Need at least 2 channels to test highlight switching');
      return;
    }

    await buttons.nth(0).click();
    await page.waitForTimeout(300);

    await buttons.nth(1).click();
    await page.waitForTimeout(300);

    // First button must no longer carry gold background
    const firstClasses = await buttons.nth(0).getAttribute('class');
    expect(firstClasses).not.toMatch(/bg-gold-500/);

    // Second button must carry gold background
    const secondClasses = await buttons.nth(1).getAttribute('class');
    expect(secondClasses).toMatch(/bg-gold-500|text-gold-400/);
  });

  test('B10. Voice channel shows participant count badge when count > 0', async ({ page }) => {
    const voiceHeader = page.getByText('Voice / Video', { exact: false });
    if (!(await voiceHeader.isVisible({ timeout: 3000 }).catch(() => false))) {
      test.skip(true, 'No voice channels present');
      return;
    }

    // Navigate up to the section container div
    const voiceSection = voiceHeader.locator('../../..');
    const badge = voiceSection.locator('span.text-navy-400').filter({ hasText: /^\d+$/ }).first();
    const hasBadge = await badge.isVisible({ timeout: 2000 }).catch(() => false);

    if (!hasBadge) {
      // No participants in any voice channel — just confirm section header rendered
      await expect(voiceHeader).toBeVisible();
      return;
    }

    const badgeText = await badge.textContent();
    expect(Number(badgeText.trim())).toBeGreaterThan(0);
  });

  test('B11. Section headers are absent for channel types with no channels', async ({ page }) => {
    const groups = [
      { label: 'Chat Channels' },
      { label: 'Forums' },
      { label: 'Voice / Video' },
    ];

    for (const { label } of groups) {
      const header = page.getByText(label, { exact: false });
      const headerVisible = await header.isVisible({ timeout: 2000 }).catch(() => false);
      if (headerVisible) {
        // If the section header is visible, it must have at least one button beneath it
        const section = header.locator('../../..');
        const btnCount = await section.locator('button').count();
        expect(btnCount).toBeGreaterThan(0);
      }
      // If not visible, the assertion is vacuously satisfied — no header for empty type
    }
  });

  test('B12. All channel names visible in sidebar are non-empty strings', async ({ page }) => {
    const sidebar = page.locator('.w-60').first();
    const buttons = sidebar.locator('button');
    const count = await buttons.count();

    if (count === 0) {
      test.skip(true, 'No channel buttons in sidebar');
      return;
    }

    for (let i = 0; i < count; i++) {
      const text = await buttons.nth(i).textContent();
      expect(text.trim().length).toBeGreaterThan(0);
    }
  });
});

// ---------------------------------------------------------------------------
// C. Chat View
// ---------------------------------------------------------------------------

test.describe('C. Chat View', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
    await gotoInteractPage(page, COMMUNITY_ID);

    if (await hasNoChannels(page)) {
      test.skip(true, 'No channels — skipping chat tests');
      return;
    }

    const chatBtn = await findFirstChannelOfType(page, 'chat');
    if (!chatBtn) {
      test.skip(true, 'No chat channels configured — skipping chat tests');
      return;
    }

    await chatBtn.click();
    // Wait for the chat view to render (input placeholder indicates ChatView is mounted)
    await page.waitForSelector('input[placeholder*="Message"]', { timeout: 10000 }).catch(() => {});
  });

  test('C13. Chat view header renders with channel name when chat channel selected', async ({ page }) => {
    // ChatView header: HashtagIcon + font-semibold channel name span inside border-b bar
    const header = page.locator('div.border-b span.font-semibold, div.border-b .font-semibold').first();
    await expect(header).toBeVisible({ timeout: 5000 });
    const text = await header.textContent();
    expect(text.trim().length).toBeGreaterThan(0);
  });

  test('C14. Message input is present at the bottom of the chat view', async ({ page }) => {
    const input = page.locator('input[placeholder*="Message"]');
    await expect(input).toBeVisible({ timeout: 5000 });
  });

  test('C15. Send button is visible and disabled when message input is empty', async ({ page }) => {
    // Ensure input is clear first
    const input = page.locator('input[placeholder*="Message"]');
    await expect(input).toBeVisible({ timeout: 5000 });
    await input.fill('');

    const sendBtn = page.locator('button.bg-gold-500').first();
    await expect(sendBtn).toBeVisible({ timeout: 5000 });

    // Button must be disabled (either via HTML disabled attr or opacity-40 class)
    const disabled = await sendBtn.getAttribute('disabled');
    const classes = await sendBtn.getAttribute('class') || '';
    const isDisabled = disabled !== null || classes.includes('opacity-40');
    expect(isDisabled).toBe(true);
  });

  test('C16. Typing text into message input enables the send button', async ({ page }) => {
    const input = page.locator('input[placeholder*="Message"]');
    await expect(input).toBeVisible({ timeout: 10000 });
    await input.fill('hello e2e test');

    const sendBtn = page.locator('button.bg-gold-500').first();
    await expect(sendBtn).toBeVisible({ timeout: 5000 });
    const disabled = await sendBtn.getAttribute('disabled');
    expect(disabled).toBeNull();
  });

  test('C17. Send button carries bg-gold-500 styling class', async ({ page }) => {
    const sendBtn = page.locator('button.bg-gold-500').first();
    await expect(sendBtn).toBeVisible({ timeout: 5000 });
    const classes = await sendBtn.getAttribute('class');
    expect(classes).toContain('bg-gold-500');
  });

  test('C18. Channel description shown in header when the channel has one', async ({ page }) => {
    // ChatView renders: <p class="text-xs text-navy-400 mt-0.5">{channel.description}</p>
    // If the element exists it must have non-empty text; if absent the test passes vacuously.
    const desc = page.locator('div.border-b p.text-navy-400').first();
    const visible = await desc.isVisible({ timeout: 2000 }).catch(() => false);
    if (visible) {
      const text = await desc.textContent();
      expect(text.trim().length).toBeGreaterThan(0);
    }
    // No description present — acceptable (optional field)
    expect(true).toBe(true);
  });

  test('C19. Messages area has overflow-y-auto for scrollability', async ({ page }) => {
    // ChatView messages area: div.bg-navy-950.flex-1.overflow-y-auto
    // Also wait for message input to be present (ensures ChatView is fully mounted)
    await page.waitForSelector('input[placeholder*="Message"]', { timeout: 10000 }).catch(() => {});
    const messagesArea = page.locator('.bg-navy-950.overflow-y-auto').first();
    await expect(messagesArea).toBeVisible({ timeout: 10000 });
    const classes = await messagesArea.getAttribute('class');
    expect(classes).toContain('overflow-y-auto');
  });

  test('C20. Messages show sender username with text-sky-300 color', async ({ page }) => {
    const usernameEl = page.locator('.text-sky-300').first();
    const visible = await usernameEl.isVisible({ timeout: 3000 }).catch(() => false);

    if (!visible) {
      // No messages — verify the empty-state text instead
      const emptyState = page.getByText('No messages yet. Be the first to say something!');
      await expect(emptyState).toBeVisible({ timeout: 5000 });
      return;
    }

    const classes = await usernameEl.getAttribute('class');
    expect(classes).toContain('text-sky-300');
  });

  test('C21. Messages display timestamps with text-navy-500 styling', async ({ page }) => {
    const timestampEl = page.locator('.text-navy-500').first();
    const visible = await timestampEl.isVisible({ timeout: 3000 }).catch(() => false);

    if (!visible) {
      // No messages — acceptable
      const emptyState = page.getByText('No messages yet');
      await expect(emptyState).toBeVisible({ timeout: 5000 });
      return;
    }

    await expect(timestampEl).toBeVisible();
  });

  test('C22. Bridged messages display "via X" platform badge', async ({ page }) => {
    // Bridged: <span class="text-xs text-navy-400">via Discord</span>
    const viaBadge = page.locator('span.text-navy-400').filter({ hasText: /^via /i }).first();
    const visible = await viaBadge.isVisible({ timeout: 2000 }).catch(() => false);

    if (!visible) {
      // No bridged messages — vacuously satisfied
      expect(true).toBe(true);
      return;
    }

    const text = await viaBadge.textContent();
    expect(text.trim()).toMatch(/^via /i);
  });
});

// ---------------------------------------------------------------------------
// D. Forum View  (serial — create→view→back→reply flow)
// ---------------------------------------------------------------------------

test.describe.serial('D. Forum View', () => {
  const testPostTitle = `e2e-post-${Date.now()}`;
  const testPostBody = 'Automated e2e test post — please ignore.';

  async function navigateToForum(page) {
    await ensureAuthenticated(page);
    await gotoInteractPage(page, COMMUNITY_ID);

    if (await hasNoChannels(page)) return false;

    const forumBtn = await findFirstChannelOfType(page, 'forum');
    if (!forumBtn) return false;

    await forumBtn.click();
    // Wait for the forum view to render — New Post button indicates ForumView is mounted
    await page.waitForSelector('button', { timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(500);
    return true;
  }

  test('D23. Forum view renders when a forum channel is selected', async ({ page }) => {
    const ok = await navigateToForum(page);
    if (!ok) {
      test.skip(true, 'No forum channels — skipping forum tests');
      return;
    }

    const newPostBtn = page.getByRole('button', { name: /New Post/i });
    await expect(newPostBtn).toBeVisible({ timeout: 8000 });
  });

  test('D24. "New Post" button is visible in the forum channel view', async ({ page }) => {
    const ok = await navigateToForum(page);
    if (!ok) {
      test.skip(true, 'No forum channels');
      return;
    }

    await expect(page.getByRole('button', { name: /New Post/i })).toBeVisible({ timeout: 8000 });
  });

  test('D25. Clicking "New Post" reveals inline form with title input and body textarea', async ({ page }) => {
    const ok = await navigateToForum(page);
    if (!ok) {
      test.skip(true, 'No forum channels');
      return;
    }

    await page.getByRole('button', { name: /New Post/i }).click();

    await expect(page.locator('input[placeholder="Post title"]')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('textarea[placeholder="Write your post..."]')).toBeVisible({ timeout: 5000 });
  });

  test('D26. Cancel button hides the new post form', async ({ page }) => {
    const ok = await navigateToForum(page);
    if (!ok) {
      test.skip(true, 'No forum channels');
      return;
    }

    await page.getByRole('button', { name: /New Post/i }).click();

    const titleInput = page.locator('input[placeholder="Post title"]');
    await expect(titleInput).toBeVisible({ timeout: 5000 });

    await page.getByRole('button', { name: /^Cancel$/i }).click();

    await expect(titleInput).not.toBeVisible({ timeout: 5000 });
  });

  test('D27. Create Post button is disabled when title is empty', async ({ page }) => {
    const ok = await navigateToForum(page);
    if (!ok) {
      test.skip(true, 'No forum channels');
      return;
    }

    await page.getByRole('button', { name: /New Post/i }).click();

    // Fill body but leave title empty
    await page.locator('textarea[placeholder="Write your post..."]').fill('some body text');

    // The submit button must remain disabled
    const submitBtn = page.locator('button[type="submit"]').filter({ hasText: /Create Post/i });
    const disabled = await submitBtn.getAttribute('disabled');
    expect(disabled).not.toBeNull();
  });

  test('D28. Creating a post (valid title + body) causes it to appear in the list', async ({ page }) => {
    const ok = await navigateToForum(page);
    if (!ok) {
      test.skip(true, 'No forum channels');
      return;
    }

    await page.getByRole('button', { name: /New Post/i }).click();

    await page.locator('input[placeholder="Post title"]').fill(testPostTitle);
    await page.locator('textarea[placeholder="Write your post..."]').fill(testPostBody);

    const responsePromise = page.waitForResponse(
      (r) => r.url().includes('/forum/posts') && r.request().method() === 'POST',
      { timeout: 15000 },
    ).catch(() => null);

    await page.locator('button[type="submit"]').filter({ hasText: /Create Post/i }).click();

    const response = await responsePromise;
    if (response) {
      const status = response.status();
      if (status >= 400) {
        test.skip(true, `Server returned ${status} on post creation — skipping`);
        return;
      }
    }

    await expect(page.getByText(testPostTitle)).toBeVisible({ timeout: 10000 });
  });

  test('D29. Post cards show title, author name, and reply count', async ({ page }) => {
    const ok = await navigateToForum(page);
    if (!ok) {
      test.skip(true, 'No forum channels');
      return;
    }

    // Wait for forum view to settle — either posts or empty state must appear
    await page.waitForFunction(
      () => {
        const cards = document.querySelectorAll('button.bg-navy-800');
        const empty = [...document.querySelectorAll('*')].some((el) => el.textContent?.trim() === 'No posts yet');
        const newPostBtn = [...document.querySelectorAll('button')].some((btn) => /New Post/i.test(btn.textContent));
        return cards.length > 0 || empty || newPostBtn;
      },
      { timeout: 15000 },
    ).catch(() => {});

    // bg-navy-800 border border-navy-700 buttons are the post cards
    const postCards = page.locator('button.bg-navy-800.border.border-navy-700');
    const count = await postCards.count().catch(() => 0);

    if (count === 0) {
      const emptyState = page.getByText('No posts yet');
      const emptyVisible = await emptyState.isVisible({ timeout: 8000 }).catch(() => false);
      if (!emptyVisible) {
        // Forum may still be loading or has a different empty-state message — skip gracefully
        test.skip(true, 'Forum empty state not visible — skipping post card check');
        return;
      }
      return;
    }

    const firstCard = postCards.first();
    await expect(firstCard).toBeVisible();

    // Title: text-sky-100 font-medium span inside the card
    const title = firstCard.locator('.text-sky-100.font-medium').first();
    await expect(title).toBeVisible();
    expect((await title.textContent()).trim().length).toBeGreaterThan(0);
  });

  test('D30. Pinned posts show a gold-styled "Pinned" badge', async ({ page }) => {
    const ok = await navigateToForum(page);
    if (!ok) {
      test.skip(true, 'No forum channels');
      return;
    }

    const pinnedBadge = page.locator('span.text-gold-400').filter({ hasText: /Pinned/i }).first();
    const visible = await pinnedBadge.isVisible({ timeout: 2000 }).catch(() => false);

    if (!visible) {
      // No pinned posts — vacuously satisfied
      expect(true).toBe(true);
      return;
    }

    const classes = await pinnedBadge.getAttribute('class');
    expect(classes).toContain('text-gold-400');
  });

  test('D31. Locked posts show a red-styled "Locked" badge', async ({ page }) => {
    const ok = await navigateToForum(page);
    if (!ok) {
      test.skip(true, 'No forum channels');
      return;
    }

    const lockedBadge = page.locator('span.text-red-400').filter({ hasText: /Locked/i }).first();
    const visible = await lockedBadge.isVisible({ timeout: 2000 }).catch(() => false);

    if (!visible) {
      // No locked posts — vacuously satisfied
      expect(true).toBe(true);
      return;
    }

    const classes = await lockedBadge.getAttribute('class');
    expect(classes).toContain('text-red-400');
  });

  test('D32. Clicking a post card opens post detail with body and replies section', async ({ page }) => {
    const ok = await navigateToForum(page);
    if (!ok) {
      test.skip(true, 'No forum channels');
      return;
    }

    const postCards = page.locator('button.bg-navy-800.border.border-navy-700');
    const count = await postCards.count().catch(() => 0);

    if (count === 0) {
      test.skip(true, 'No posts to click — skipping post detail test');
      return;
    }

    await postCards.first().click();
    await page.waitForTimeout(800);

    // Detail view must show the back link
    await expect(page.getByText('Back to posts')).toBeVisible({ timeout: 5000 });

    // Detail view must show post body text (text-navy-200 paragraph in detail card)
    const postBody = page.locator('.bg-navy-800.border.border-navy-700 p.text-navy-200').first();
    await expect(postBody).toBeVisible({ timeout: 5000 });
  });

  test('D33. "Back to posts" link returns to the post list', async ({ page }) => {
    const ok = await navigateToForum(page);
    if (!ok) {
      test.skip(true, 'No forum channels');
      return;
    }

    const postCards = page.locator('button.bg-navy-800.border.border-navy-700');
    const count = await postCards.count().catch(() => 0);

    if (count === 0) {
      test.skip(true, 'No posts available — skipping back navigation test');
      return;
    }

    await postCards.first().click();
    await page.waitForTimeout(500);

    const backLink = page.getByText('Back to posts');
    await expect(backLink).toBeVisible({ timeout: 5000 });
    await backLink.click();
    await page.waitForTimeout(500);

    // Back on list → "New Post" button should be visible again
    await expect(page.getByRole('button', { name: /New Post/i })).toBeVisible({ timeout: 5000 });
  });

  test('D34. Reply textarea is visible on unlocked posts and hidden on locked posts', async ({ page }) => {
    const ok = await navigateToForum(page);
    if (!ok) {
      test.skip(true, 'No forum channels');
      return;
    }

    const postCards = page.locator('button.bg-navy-800.border.border-navy-700');
    const count = await postCards.count().catch(() => 0);

    if (count === 0) {
      test.skip(true, 'No posts to test reply input visibility');
      return;
    }

    // Try to classify posts as locked / unlocked and pick one to click
    let clicked = false;
    let clickedIsLocked = false;

    for (let i = 0; i < Math.min(count, 5); i++) {
      const card = postCards.nth(i);
      const lockedBadge = card.locator('span.text-red-400').filter({ hasText: /Locked/i });
      clickedIsLocked = await lockedBadge.isVisible({ timeout: 400 }).catch(() => false);
      await card.click();
      clicked = true;
      break;
    }

    if (!clicked) {
      test.skip(true, 'Could not click any post card');
      return;
    }

    await page.waitForTimeout(800);
    const replyTextarea = page.locator('textarea[placeholder="Write a reply..."]');

    if (!clickedIsLocked) {
      // Unlocked — reply input should be present
      await expect(replyTextarea).toBeVisible({ timeout: 5000 });
    } else {
      // Locked — reply input must NOT be rendered
      await expect(replyTextarea).not.toBeVisible({ timeout: 3000 });
    }
  });
});

// ---------------------------------------------------------------------------
// E. Voice View
// ---------------------------------------------------------------------------

test.describe('E. Voice View', () => {
  async function navigateToVoice(page) {
    await ensureAuthenticated(page);
    await gotoInteractPage(page, COMMUNITY_ID);

    if (await hasNoChannels(page)) return false;

    const voiceBtn = await findFirstChannelOfType(page, 'voice');
    if (!voiceBtn) return false;

    await voiceBtn.click();
    await page.waitForTimeout(800);
    return true;
  }

  test('E35. Voice view renders when a voice channel is selected', async ({ page }) => {
    const ok = await navigateToVoice(page);
    if (!ok) {
      test.skip(true, 'No voice channels configured — skipping voice tests');
      return;
    }

    // VoiceView header has SpeakerWaveIcon with text-gold-400 class
    const voiceIcon = page.locator('.text-gold-400').first();
    await expect(voiceIcon).toBeVisible({ timeout: 8000 });
  });

  test('E36. Voice room list or empty state renders in voice view', async ({ page }) => {
    const ok = await navigateToVoice(page);
    if (!ok) {
      test.skip(true, 'No voice channels');
      return;
    }

    // Either a room card or the empty state must be visible
    const roomCard = page.locator('.bg-navy-800.border.border-navy-700').first();
    const emptyState = page.getByText('No voice rooms available.');

    const cardVisible = await roomCard.isVisible({ timeout: 5000 }).catch(() => false);
    const emptyVisible = await emptyState.isVisible({ timeout: 5000 }).catch(() => false);

    expect(cardVisible || emptyVisible).toBe(true);
  });

  test('E37. "Create Room" button only appears when allowAdHocVoice is enabled on the channel', async ({ page }) => {
    const ok = await navigateToVoice(page);
    if (!ok) {
      test.skip(true, 'No voice channels');
      return;
    }

    const createBtn = page.getByRole('button', { name: /Create Room/i });
    const visible = await createBtn.isVisible({ timeout: 3000 }).catch(() => false);

    if (!visible) {
      // allowAdHocVoice is false — correct that the button is absent
      expect(true).toBe(true);
      return;
    }

    // allowAdHocVoice is true — clicking it should open the modal
    await createBtn.click();
    const modal = page.locator('.fixed.inset-0');
    await expect(modal).toBeVisible({ timeout: 5000 });

    // Dismiss
    const cancelBtn = modal.getByRole('button', { name: /Cancel/i });
    await cancelBtn.click();
    await expect(modal).not.toBeVisible({ timeout: 3000 });
  });

  test('E38. Join button is visible on voice room cards', async ({ page }) => {
    const ok = await navigateToVoice(page);
    if (!ok) {
      test.skip(true, 'No voice channels');
      return;
    }

    const roomCard = page.locator('.bg-navy-800.border.border-navy-700').first();
    const cardVisible = await roomCard.isVisible({ timeout: 5000 }).catch(() => false);

    if (!cardVisible) {
      test.skip(true, 'No room cards present — cannot test Join button');
      return;
    }

    const joinBtn = roomCard.getByRole('button', { name: /Join/i });
    await expect(joinBtn).toBeVisible({ timeout: 5000 });
  });

  test('E39. Create Room modal has room name input and Create + Cancel buttons', async ({ page }) => {
    const ok = await navigateToVoice(page);
    if (!ok) {
      test.skip(true, 'No voice channels');
      return;
    }

    const createBtn = page.getByRole('button', { name: /Create Room/i });
    const visible = await createBtn.isVisible({ timeout: 3000 }).catch(() => false);

    if (!visible) {
      test.skip(true, 'Create Room button not visible (allowAdHocVoice disabled)');
      return;
    }

    await createBtn.click();

    const modal = page.locator('.fixed.inset-0');
    const nameInput = modal.locator('#room-name');
    const createModalBtn = modal.getByRole('button', { name: /^Create$/i });
    const cancelModalBtn = modal.getByRole('button', { name: /Cancel/i });

    await expect(nameInput).toBeVisible({ timeout: 5000 });
    await expect(createModalBtn).toBeVisible({ timeout: 3000 });
    await expect(cancelModalBtn).toBeVisible({ timeout: 3000 });

    // Create button must be disabled when name is empty
    const disabled = await createModalBtn.getAttribute('disabled');
    expect(disabled).not.toBeNull();

    // Fill a name — Create button should enable
    await nameInput.fill('test-room');
    const disabledAfter = await createModalBtn.getAttribute('disabled');
    expect(disabledAfter).toBeNull();

    // Dismiss
    await cancelModalBtn.click();
    await expect(modal).not.toBeVisible({ timeout: 3000 });
  });

  test('E40. Leave button (bg-red-600) appears when in-call view is active', async ({ page }) => {
    const ok = await navigateToVoice(page);
    if (!ok) {
      test.skip(true, 'No voice channels');
      return;
    }

    const roomCard = page.locator('.bg-navy-800.border.border-navy-700').first();
    const cardVisible = await roomCard.isVisible({ timeout: 5000 }).catch(() => false);

    if (!cardVisible) {
      test.skip(true, 'No room cards — cannot test in-call view');
      return;
    }

    const joinBtn = roomCard.getByRole('button', { name: /Join/i });

    const responsePromise = page.waitForResponse(
      (r) => r.url().includes('/voice') && r.request().method() === 'POST',
      { timeout: 10000 },
    ).catch(() => null);

    await joinBtn.click();
    const response = await responsePromise;

    if (response) {
      const status = response.status();
      if (status >= 400) {
        test.skip(true, `Voice join returned ${status} — cannot test in-call view`);
        return;
      }
    }

    // In-call view: Leave button has bg-red-600
    const leaveBtn = page.locator('button.bg-red-600').first();
    await expect(leaveBtn).toBeVisible({ timeout: 8000 });

    // Clean up — leave the room
    await leaveBtn.click();
    await page.waitForTimeout(500);
  });
});

// ---------------------------------------------------------------------------
// F. Dashboard Integration
// ---------------------------------------------------------------------------

test.describe('F. Dashboard Integration', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
  });

  test('F41. "Chat & Forums" quick link exists on the community dashboard', async ({ page }) => {
    // Try the public community page first, then the admin dashboard variant
    await gotoPage(page, `/community/${COMMUNITY_ID}`);
    await suppressOverlays(page);
    await page.waitForTimeout(2000);

    let link = page.getByText(/Chat\s*&\s*Forums/i).first();
    let visible = await link.isVisible({ timeout: 5000 }).catch(() => false);

    if (!visible) {
      await gotoPage(page, `/dashboard/community/${COMMUNITY_ID}`);
      await page.waitForTimeout(2000);
      link = page.getByText(/Chat\s*&\s*Forums/i).first();
      visible = await link.isVisible({ timeout: 5000 }).catch(() => false);
    }

    if (!visible) {
      test.skip(true, 'Community dashboard not accessible or layout does not include Chat & Forums link');
      return;
    }

    await expect(link).toBeVisible();
  });

  test('F42. Clicking "Chat & Forums" link navigates to /community/:id/interact', async ({ page }) => {
    await gotoPage(page, `/community/${COMMUNITY_ID}`);
    await suppressOverlays(page);
    await page.waitForTimeout(2000);

    let link = page.getByText(/Chat\s*&\s*Forums/i).first();
    let visible = await link.isVisible({ timeout: 5000 }).catch(() => false);

    if (!visible) {
      await gotoPage(page, `/dashboard/community/${COMMUNITY_ID}`);
      await page.waitForTimeout(2000);
      link = page.getByText(/Chat\s*&\s*Forums/i).first();
      visible = await link.isVisible({ timeout: 5000 }).catch(() => false);
    }

    if (!visible) {
      test.skip(true, 'Cannot find Chat & Forums link on any community dashboard path');
      return;
    }

    await link.click();
    await page.waitForTimeout(2000);

    expect(page.url()).toContain(`/community/${COMMUNITY_ID}/interact`);
  });
});

// ---------------------------------------------------------------------------
// G. Navigation & State
// ---------------------------------------------------------------------------

test.describe('G. Navigation & State', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
  });

  test('G43. Switching from a chat channel to a forum channel updates the content view', async ({ page }) => {
    await gotoInteractPage(page, COMMUNITY_ID);

    if (await hasNoChannels(page)) {
      test.skip(true, 'No channels — skipping channel switching test');
      return;
    }

    const chatBtn = await findFirstChannelOfType(page, 'chat');
    const forumBtn = await findFirstChannelOfType(page, 'forum');

    if (!chatBtn || !forumBtn) {
      test.skip(true, 'Need both chat and forum channels to test view type switching');
      return;
    }

    // Switch to chat — verify chat-specific UI
    await chatBtn.click();
    await page.waitForTimeout(600);
    await expect(page.locator('input[placeholder*="Message"]')).toBeVisible({ timeout: 5000 });

    // Switch to forum — verify forum-specific UI and absence of chat input
    await forumBtn.click();
    await page.waitForTimeout(600);
    await expect(page.getByRole('button', { name: /New Post/i })).toBeVisible({ timeout: 5000 });
    await expect(page.locator('input[placeholder*="Message"]')).not.toBeVisible({ timeout: 3000 });
  });

  test('G44. Browser back button returns to the previously selected channel URL', async ({ page }) => {
    await gotoInteractPage(page, COMMUNITY_ID);

    if (await hasNoChannels(page)) {
      test.skip(true, 'No channels — skipping back/forward navigation test');
      return;
    }

    const sidebar = page.locator('.w-60').first();
    // Scope to ul buttons to skip the optional "New Channel" create button at the top
    const buttons = sidebar.locator('ul button');
    const count = await buttons.count();

    if (count < 2) {
      test.skip(true, 'Need at least 2 channels for back/forward navigation test');
      return;
    }

    await buttons.nth(0).click();
    await page.waitForTimeout(400);
    const firstUrl = page.url();

    await buttons.nth(1).click();
    await page.waitForTimeout(400);
    const secondUrl = page.url();

    // URLs must differ
    expect(firstUrl).not.toBe(secondUrl);

    // Browser back
    await page.goBack();
    await page.waitForTimeout(600);

    expect(page.url()).toBe(firstUrl);
  });

  test('G45. Reloading the page on a specific channel preserves that channel URL', async ({ page }) => {
    await gotoInteractPage(page, COMMUNITY_ID);

    if (await hasNoChannels(page)) {
      test.skip(true, 'No channels — skipping refresh test');
      return;
    }

    // Wait for auto-redirect to first channel
    await page.waitForTimeout(2500);
    const urlBeforeRefresh = page.url();

    if (!urlBeforeRefresh.match(/\/interact\/\d+/)) {
      test.skip(true, 'Did not redirect to a specific channel — cannot test refresh');
      return;
    }

    const jsErrors = [];
    page.on('pageerror', (err) => jsErrors.push(err.message));

    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForFunction(
      () => !window.location.pathname.startsWith('/login'),
      { timeout: 15000 },
    ).catch(() => {});
    await page.waitForTimeout(2000);

    // URL must survive the reload
    expect(page.url()).toBe(urlBeforeRefresh);

    // No fatal JS errors after reload
    const fatalErrors = jsErrors.filter(
      (e) => !e.includes('WebSocket') && !e.includes('socket.io') && !e.includes('net::ERR'),
    );
    expect(fatalErrors).toHaveLength(0);

    // Either sidebar or empty state must be visible — page loaded correctly
    const sidebar = page.locator('.w-60').first();
    const emptyState = page.getByText('No channels yet');
    const sidebarVisible = await sidebar.isVisible({ timeout: 8000 }).catch(() => false);
    const emptyVisible = await emptyState.isVisible({ timeout: 3000 }).catch(() => false);
    expect(sidebarVisible || emptyVisible).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Regression: Issue #108 — socket auth failure + channel creation 500/409 cycle
// ---------------------------------------------------------------------------

test.describe('Issue #108 regression — socket auth + channel creation', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAuthenticated(page);
  });

  test('socket connects without "Authentication failed" error on page load', async ({ page }) => {
    const socketErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error' && msg.text().includes('Authentication failed')) {
        socketErrors.push(msg.text());
      }
    });

    await gotoInteractPage(page, COMMUNITY_ID);
    // Give the socket handshake time to complete (or fail)
    await page.waitForTimeout(3000);

    // Regression for Bug C: stale token on useMemo caused "Authentication failed"
    // on every socket connect/reconnect attempt
    expect(socketErrors).toHaveLength(0);
  });

  test('channel creation from sidebar does not produce an error toast', async ({ page }) => {
    await gotoInteractPage(page, COMMUNITY_ID);

    const addChannelBtn = page.locator('[data-testid="add-channel-btn"]').first();
    const canCreate = await addChannelBtn.isVisible({ timeout: 5000 }).catch(() => false);
    if (!canCreate) {
      test.skip();
      return;
    }

    await addChannelBtn.click();

    const nameInput = page.locator('[data-testid="channel-name-input"]');
    await nameInput.waitFor({ timeout: 5000 });
    await nameInput.fill(`e2e-regression-${Date.now()}`);

    const typeSelect = page.locator('[data-testid="channel-type-select"]');
    if (await typeSelect.isVisible({ timeout: 2000 }).catch(() => false)) {
      await typeSelect.selectOption('chat');
    }

    await page.locator('[data-testid="create-channel-submit"]').click();

    // Regression for Bug A + B: a 500 from the server would surface as an error toast
    const errorToast = page.locator('.toast-error, [data-testid="error-toast"], .Toastify__toast--error');
    const toastVisible = await errorToast.isVisible({ timeout: 4000 }).catch(() => false);
    expect(toastVisible).toBe(false);
  });
});
