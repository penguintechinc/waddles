// DRAFT — pending legal/DPO review
import Link from 'next/link';
import type { ReactNode } from 'react';

/** Titled policy section with the shared heading style. */
function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <h2 className="text-3xl font-bold mb-4 text-blue-300">{title}</h2>
      <div className="space-y-4">{children}</div>
    </div>
  );
}

/** Body paragraph in the policy text style. */
function P({ children }: { children: ReactNode }) {
  return <p className="text-gray-300 leading-relaxed">{children}</p>;
}

/** Bulleted list of plain-text items. */
function UL({ items }: { items: string[] }) {
  return (
    <ul className="list-disc list-inside text-gray-300 space-y-2 ml-4">
      {items.map((item) => (
        <li key={item}>{item}</li>
      ))}
    </ul>
  );
}

/** Responsive three-column-style table for lawful bases, categories and sub-processors. */
function Table({ head, rows }: { head: string[]; rows: string[][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm text-gray-300 border border-white/10">
        <thead className="bg-white/10 text-purple-300">
          <tr>
            {head.map((h) => (
              <th key={h} scope="col" className="p-3 font-semibold">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row[0]} className="border-t border-white/10 align-top">
              {row.map((cell) => (
                <td key={cell} className="p-3">{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function Privacy() {
  return (
    <div className="font-sans min-h-screen bg-gradient-to-br from-blue-900 via-purple-900 to-indigo-900 text-white">
      {/* DRAFT — pending legal/DPO review */}
      {/* Navigation */}
      <nav className="flex justify-between items-center p-6 bg-black/20 backdrop-blur-sm sticky top-0 z-50">
        <Link href="/" className="text-2xl font-bold bg-gradient-to-r from-blue-400 to-purple-400 bg-clip-text text-transparent">
          Waddles
        </Link>
        <div className="hidden md:flex gap-8">
          <Link href="/features" className="hover:text-blue-300 transition-colors">Features</Link>
          <Link href="/pricing" className="hover:text-blue-300 transition-colors">Pricing</Link>
          <Link href="/solutions" className="hover:text-blue-300 transition-colors">Solutions</Link>
          <a href="https://docs.waddlebot.io" className="hover:text-blue-300 transition-colors">Documentation</a>
          <Link href="/contact" className="hover:text-blue-300 transition-colors">Contact</Link>
        </div>
        <div className="flex gap-4">
          <Link href="/demo" className="px-4 py-2 bg-blue-600 hover:bg-blue-700 rounded-lg transition-colors font-semibold">
            Live Demo
          </Link>
        </div>
      </nav>

      {/* Hero Section */}
      <section className="container mx-auto px-6 py-20">
        <div className="max-w-4xl mx-auto text-center">
          <h1 className="text-5xl md:text-6xl font-bold mb-6 bg-gradient-to-r from-blue-400 to-purple-400 bg-clip-text text-transparent">
            Privacy Policy
          </h1>
          <p className="text-xl md:text-2xl mb-8 text-gray-200">
            Your privacy matters to us. Here&rsquo;s how we handle your data.
          </p>
        </div>
      </section>

      {/* Privacy Policy Content */}
      <section className="container mx-auto px-6 pb-20">
        <div className="max-w-4xl mx-auto">
          <div className="bg-white/5 backdrop-blur-sm rounded-2xl p-8 border border-white/10 space-y-8">
            {/* Last Updated */}
            <div className="text-center pb-6 border-b border-white/10">
              <p className="text-gray-400">Last updated: October 18, 2026</p>
              <p className="text-yellow-300 text-sm mt-2">Draft pending legal / DPO review. Content reflects current product behavior and may change before sign-off.</p>
            </div>

            <Section title="Introduction">
              <P>
                Waddles (&ldquo;we,&rdquo; &ldquo;our,&rdquo; or &ldquo;us&rdquo;), operated by Penguin Technologies Group LLC, provides multi-platform community management (Discord, Twitch and others). This policy explains what personal data the cloud-hosted service processes, why, on what legal basis, who receives it, and how you can exercise your rights.
              </P>
              <P>
                <strong>Roles.</strong> For community data (chat activity, moderation, reputation), the tenant (the organization or community operator that installed Waddles) is the controller and Waddles is its processor. For account, sign-in, security and website data, Waddles is the controller. If you self-host the open source version, we do not receive your data.
              </P>
            </Section>

            <Section title="Personal Data We Process">
              <UL items={[
                'Account data: e-mail, username, display name, avatar, password hash (if you use a password), passkeys, SSO subject identifiers, sessions.',
                'Linked platform identities: your Discord/Twitch (or other provider) user id, username, avatar and, where the provider supplies it, e-mail, obtained through OAuth sign-in or account linking.',
                'Community activity: chat messages and commands, activity and watch-time events, memberships and roles, and moderation actions (including bans).',
                'Reputation and economy data: reputation scores and their event history, loyalty points and other virtual-economy balances and ledgers (virtual points only; no payment card data is handled by Waddles).',
                'AI-related data: messages sent to AI features and, for the AI memory feature, semantic memories derived from chat, keyed to a pseudonymous identifier.',
                'Consent and cookie data: your cookie choices, consent version, IP address and user agent at the time of the choice.',
                'Technical and security data: IP address, user agent, audit-trail entries and operational telemetry (logs, metrics, traces).',
                'Marketplace billing data (only if you buy or sell through the marketplace): processed by the payment provider; see Sub-processors.',
              ]} />
              <P>
                Internally, most services refer to you by an opaque id rather than by e-mail or username. We do not use advertising cookies, third-party analytics cookies or session-recording scripts on the web app or this website.
              </P>
            </Section>

            <Section title="Purposes and Lawful Bases (GDPR Art. 6)">
              <Table
                head={['Purpose', 'Data', 'Lawful basis']}
                rows={[
                  ['Provide the service: accounts, sign-in, linking platform identities, community membership and commands', 'Account, identity, activity data', 'Contract (Art. 6(1)(b)); for community data processed for a tenant, the tenant’s instructions as controller'],
                  ['Community management and moderation, including bans and automated reputation-based restrictions', 'Activity, memberships, moderation actions, reputation', 'Legitimate interests of the community operator and its members in a safe community (Art. 6(1)(f))'],
                  ['Reputation and loyalty/economy features', 'Reputation, points, ledgers', 'Legitimate interests (Art. 6(1)(f)) / contract where you opt into the feature'],
                  ['Ban-evasion suppression: after erasure, one keyed one-way code for a banned account is kept so the ban can be re-applied in that tenant', 'Keyed code of platform id, restriction type, expiry', 'Legitimate interests (Art. 6(1)(f)); balancing assessment in progress'],
                  ['AI features (WaddleAI replies, AI chatter, research, translation) and AI memory', 'Messages sent to AI features; derived memories', 'Legitimate interests of the community operator (Art. 6(1)(f)); consent where the operator or the law requires it'],
                  ['Cookie preferences and proof of consent', 'Consent choices, IP, user agent', 'Legal obligation / accountability (Art. 6(1)(c), 5(2)); consent for non-essential cookies (Art. 6(1)(a))'],
                  ['Security, fraud prevention, audit logging, rate limiting', 'IP, user agent, audit entries, rate-limit counters', 'Legitimate interests (Art. 6(1)(f)); legal obligation where applicable'],
                  ['Feature-flag and license validation', 'Tenant identifiers only; no end-user data', 'Legitimate interests (Art. 6(1)(f))'],
                  ['Marketplace payments', 'Billing details handled by payment provider', 'Contract (Art. 6(1)(b)); legal obligation for tax and accounting records'],
                ]}
              />
              <P>Where we rely on legitimate interests you may object at any time (see Your Rights).</P>
            </Section>

            <Section title="California Privacy Notice (CCPA / CPRA)">
              <P>In the past 12 months we have collected the following categories of personal information. Sources are you, the platforms you connect, and your use of the service.</P>
              <Table
                head={['CCPA/CPRA category', 'Examples collected', 'Purpose']}
                rows={[
                  ['Identifiers', 'Username, e-mail, platform user ids, IP address, account ids', 'Service, security'],
                  ['Customer records / account information', 'Name or display name, avatar', 'Service'],
                  ['Internet or other electronic network activity', 'Chat activity, commands, watch time, consent and audit logs', 'Service, moderation, security'],
                  ['Commercial information', 'Virtual-economy balances; marketplace transactions', 'Service, billing'],
                  ['Inferences', 'Reputation scores and community-health aggregates', 'Moderation, community management'],
                  ['Sensitive personal information', 'Account log-in credentials (password hash, passkeys) only', 'Authentication and security; not used to infer characteristics'],
                ]}
              />
              <P>
                <strong>We do not sell or share (for cross-context behavioral advertising) your personal information.</strong> We honor the Global Privacy Control signal (<code>Sec-GPC: 1</code>), which automatically sets Do Not Sell/Share on and marketing off for your cookie preferences, and you can set the same choice in the cookie banner. We do not discriminate against you for exercising your rights. California residents may request to know, access, delete or correct their personal information, and to limit use of sensitive information, using the channels in Data Deletion and Access below. We do not knowingly sell or share the data of anyone under 16.
              </P>
            </Section>

            <Section title="Sub-processors and Service Providers">
              <P>The services below are those the platform is built to call. Which of them apply depends on the deployment and the features a tenant enables.</P>
              <Table
                head={['Provider', 'Purpose', 'Data involved']}
                rows={[
                  ['Discord, Twitch and other connected platforms (OAuth/API)', 'Sign-in, account linking and chat integration chosen by you or your community', 'Platform id, username, avatar, e-mail (if provided), chat events'],
                  ['Ollama (self-hosted LLM, default AI provider)', 'AI features', 'Message text sent to AI features; runs on infrastructure we or the deployer control'],
                  ['OpenAI or Anthropic (optional, only if a deployment is configured to use them)', 'AI features instead of Ollama', 'Message text sent to AI features'],
                  ['Stripe and PayPal (marketplace billing only)', 'Payment processing', 'Billing details, handled by the provider'],
                  ['PostHog (self-hosted) and the Penguin license server', 'Server-side feature flags and license validation', 'Tenant identifier and license key only; no end-user data'],
                  ['SeaweedFS (S3-compatible, self-hosted), PostgreSQL, Valkey, Qdrant', 'Storage of uploads, database records, caches and AI memory vectors', 'As described in this policy; run within the hosting environment, not by an outside vendor'],
                  ['Infrastructure hosting provider', 'Hosts the cloud service', 'All data, encrypted in transit and at rest. Provider and region: to be confirmed before publication'],
                  ['OpenTelemetry collector (destination set by the operator)', 'Operational telemetry', 'Operational logs, metrics and traces; exception text is kept out of logs'],
                ]}
              />
              <P>Tenants (controllers) may add their own integrations. We will update this list when sub-processors change.</P>
            </Section>

            <Section title="AI Features and Automated Decisions">
              <P>
                Some features use artificial intelligence: WaddleAI replies in chat, AI chatter, research and summaries, translation, and an AI memory that stores semantic memories of chat to give better answers. <strong>When you interact with an AI feature, you are interacting with an AI system, not a human, and AI-generated replies should be treated as such</strong> (EU AI Act Art. 50 transparency). Do not put sensitive information into AI features. Memories are kept for 90 days by default and are deleted on erasure of your account. AI model providers are listed under Sub-processors.
              </P>
              <P>
                Reputation-based policies may automatically restrict (for example ban) an account. You may object or ask for human review of an automated restriction by contacting the community or tenant administrator, who can lift it. A formal appeal process for platform-level restrictions is still being built.
              </P>
            </Section>

            <Section title="Data Sharing">
              <P>We do not sell your personal information. We share data only with the sub-processors above, with the tenant that controls the community you participate in, with your consent, to comply with legal obligations, to protect rights and safety, or in a business transfer.</P>
            </Section>

            <Section title="Data Security">
              <P>
                We use encryption in transit (TLS 1.2+) and at rest, per-tenant isolation, scope-based access control, hash-chained audit logging, HttpOnly session cookies and security scanning. No system is perfectly secure; if a personal-data breach affects you, we will notify the affected controllers and, where required, authorities, as the law requires.
              </P>
            </Section>

            <Section title="Data Retention">
              <UL items={[
                'Account and community data: kept while your account is active, until you or the tenant erase it.',
                'AI memory: 90 days by default.',
                'Cookie consent: expires after 12 months; consent and audit records are kept as proof of compliance even after erasure.',
                'Ban-evasion suppression code: deleted at expiry or when a moderator lifts the restriction.',
                'Chat and activity history: retained until erased; automatic time-based expiry for these tables is not yet in place.',
                'Deletion-request ledger and audit logs: kept for accountability; they hold counts and identifiers, not content.',
              ]} />
            </Section>

            <Section title="Your Rights">
              <UL items={[
                'Access and portability (GDPR Arts. 15, 20)',
                'Rectification (Art. 16)',
                'Erasure (Art. 17)',
                'Restriction (Art. 18)',
                'Objection, including to legitimate-interest processing and automated decisions (Arts. 21, 22)',
                'Withdraw consent at any time (cookie preferences)',
                'Complain to your supervisory authority',
                'CCPA/CPRA rights listed in the California notice',
              ]} />
              <P>These rights are available on every plan, including Free.</P>
            </Section>

            <div className="bg-green-500/10 border border-green-500/20 rounded-xl p-6">
              <h2 className="text-3xl font-bold mb-4 text-green-300">Data Deletion and Access</h2>
              <P>
                <strong>Self-service.</strong> Sign in to the Waddles web app and open <strong>Your privacy rights</strong> (<code>/privacy-rights</code>). From there you can download a copy of your data (access/portability) and use <strong>Delete my data</strong> to erase your account. If your account has a password, you will be asked to confirm it.
              </P>
              <P>
                Erasure deletes your profile, sessions, passkeys, memberships, linked platform accounts, chat and activity records, reputation, economy balances, AI memories and uploaded avatars. Your account record is anonymized rather than removed so that references elsewhere do not break. Limited items are kept by law or for accountability: consent and audit records, the deletion-request ledger, and, only if you had an active ban, a keyed one-way suppression code. Some records not attributable to one person (for example community-wide AI memory) are removed with the community. We will tell you of any other exception.
              </P>
              <P>
                <strong>If you cannot use self-service</strong> (for example you have lost access), e-mail <a href="mailto:sales@penguintech.io?subject=Data Subject Request" className="text-blue-400 hover:text-blue-300">sales@penguintech.io</a> with &ldquo;Data Subject Request&rdquo; in the subject line (interim DSAR contact; a dedicated privacy mailbox is pending). We verify your identity and respond within 30 days. For data processed for a community, you may also contact that community&rsquo;s administrator.
              </P>
            </div>

            <Section title="International Data Transfers">
              <P>
                Waddles may process data in the United States and in other countries where our hosting and sub-processors operate. For transfers of EU/UK personal data to countries without an adequacy decision we intend to rely on the EU Standard Contractual Clauses (and the UK Addendum), and on the EU-U.S. Data Privacy Framework where a recipient is certified. <strong>Legal confirmation pending:</strong> the specific mechanism per sub-processor and hosting region has not yet been confirmed and must be verified before this policy is finalized.
              </P>
            </Section>

            <Section title="Cookies">
              <P>
                The web app sets three first-party cookies: <code>wb_session</code> (login session, HttpOnly, 24 hours), <code>waddlebot_consent_id</code> (links your browser to your cookie choices, 12 months) and <code>wb_sso_bind</code> (binds an SSO sign-in attempt to your browser, short-lived). There are no advertising or third-party analytics cookies.
              </P>
            </Section>

            <Section title="Children&rsquo;s Privacy">
              <P>
                Our service is not intended for children under 13 (or under 16 where local law requires). We do not knowingly collect their personal information; if we learn we have, we will delete it.
              </P>
            </Section>

            <Section title="Changes to This Policy">
              <P>
                We may update this policy. We will post the new version here and update the &ldquo;last updated&rdquo; date, and give additional notice of material changes where the law requires.
              </P>
            </Section>

            {/* Contact Information */}
            <div className="bg-blue-500/10 border border-blue-500/20 rounded-xl p-6">
              <h2 className="text-3xl font-bold mb-4 text-blue-300">Contact Us</h2>
              <P>For questions about this policy or to exercise your privacy rights where self-service is not available:</P>
              <div className="space-y-2 text-gray-300">
                <p><strong>Email:</strong> <a href="mailto:sales@penguintech.io?subject=Privacy Policy Question" className="text-blue-400 hover:text-blue-300">sales@penguintech.io</a></p>
                <p><strong>Response Time:</strong> We aim to respond within 48 hours; formal requests within 30 days.</p>
                <p><strong>Data Protection Officer:</strong> to be designated (pending).</p>
              </div>
            </div>

          </div>
        </div>
      </section>

      {/* Footer */}
      <footer className="bg-black/40 backdrop-blur-sm py-16">
        <div className="container mx-auto px-6">
          <div className="grid md:grid-cols-4 gap-8">
            <div>
              <Link href="/" className="text-2xl font-bold mb-4 bg-gradient-to-r from-blue-400 to-purple-400 bg-clip-text text-transparent block">
                Waddles
              </Link>
              <p className="text-gray-400 mb-4">
                The future of multi-platform community management
              </p>
              <div className="flex gap-4">
                <a href="https://x.com/penguintechgrp" className="text-gray-400 hover:text-blue-400 transition-colors">
                  X (Twitter)
                </a>
                <a href="https://github.com/WaddleBot" className="text-gray-400 hover:text-blue-400 transition-colors">
                  GitHub
                </a>
              </div>
            </div>
            <div>
              <h4 className="font-semibold mb-4">Product</h4>
              <div className="space-y-2 text-gray-400">
                <div><Link href="/features" className="hover:text-white transition-colors">Features</Link></div>
                <div><Link href="/pricing" className="hover:text-white transition-colors">Pricing</Link></div>
                <div><Link href="/solutions" className="hover:text-white transition-colors">Solutions</Link></div>
                <div><a href="https://docs.waddlebot.io" className="hover:text-white transition-colors">Documentation</a></div>
              </div>
            </div>
            <div>
              <h4 className="font-semibold mb-4">Company</h4>
              <div className="space-y-2 text-gray-400">
                <div><Link href="/contact" className="hover:text-white transition-colors">Contact</Link></div>
              </div>
            </div>
            <div>
              <h4 className="font-semibold mb-4">Legal</h4>
              <div className="space-y-2 text-gray-400">
                <div><Link href="/privacy" className="hover:text-white transition-colors">Privacy Policy</Link></div>
              </div>
            </div>
          </div>
          <div className="border-t border-white/10 mt-12 pt-8 text-center text-gray-400">
            <p>© 2024 Waddles. All rights reserved. Built for the future of community management.</p>
          </div>
        </div>
      </footer>
    </div>
  );
}