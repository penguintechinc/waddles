# Tenant data privacy (DSAR) console

Route: `/tenant/:tenantSlug/privacy` (nav "Data Privacy"; page `pages/tenant/TenantPrivacy.jsx`, parts in `pages/tenant/privacy/`).

Clients: `services/tenantDsarInboxApi.ts` (inbox), `services/tenantPrivacyApi.ts` (single-user + bulk). Enum codes and error messages: `services/dsarConstants.ts` (single UI source, mirrors [dsar-inbox.md](dsar-inbox.md)). Scope `tenant:admin`; the URL slug must equal the JWT tenant. The inbox works in every tier.

## Tabs

| Tab | What | Tier |
|---|---|---|
| Inbox | Request table, filters, detail view, admin-entered requests | every tier |
| Manual lookup | Export / erase / Do-Not-Sell for ONE user by UUID (Do-Not-Sell is the user's global opt-out, opt-out only; the user can withdraw it in their own consent controls) | every tier |
| Bulk | Same actions over many users | Enterprise |

## Inbox

- Table: type, status badge, regime, due-state badge (text label "On track" / "Due soon" / "Overdue", never colour alone; "No deadline" for closed or unverified), requested and due dates, assignee, an Open button.
- Filters (Apply filters / Clear): status, request type, regime, due state, assignee (user UUID, validated), sort (`requestedAt` | `dueAt`) and order. Page size 25 with Previous / Next and a "n-m of total" range.
- Unverified requests (public-form requesters who have not confirmed their email) are hidden by default, as the server does. "Show unverified requests" requests `status=2`. Those rows are labelled "Unverified", have no deadline, and never show an email or message.
- Detail view: fields, append-only timeline, notes, status, assignment, one-time extension, Run action.
  - Status can be set to In progress / Completed / Rejected / Withdrawn (the only admin-settable codes); In progress and Completed are not offered while unverified.
  - Extend requires a reason and is allowed once; after that, or when closed or unverified, it is replaced by an explanation.
  - Run action maps the request type to export / erase / do-not-sell. Erasure needs the typed word `ERASE`, then sends `confirm: true`. The button is replaced by an explanation when the request is unverified, closed, has no linked user, or its type has no automated action.
  - Server errors `DSAR_UNVERIFIED`, `DSAR_REQUEST_CLOSED`, `DSAR_ALREADY_EXTENDED` show their specific message and reload the request.
- "New request" enters a request received out of band (email and/or linked user UUID, optional notes and date received).
- States: loading, empty, 403 permission message, error, and a friendly "inbox is not enabled" state for `DSAR_INBOX_DISABLED` (the Manual lookup tab keeps working).

## Manual lookup and Bulk

- Manual lookup runs one user ID (UUID, validated client-side; numeric ids are not accepted) through the single-user routes (`GET .../users/{uuid}/export`, `POST .../erase`, `PUT .../do-not-sell`). Erasure always shows a confirm dialog first.
- Bulk uses `POST .../privacy/bulk` with `userIds` as UUIDs (trimmed, lower-cased, de-duplicated; caps: 25 per export, 100 per erase or Do-Not-Sell). A `402` is not an error: the tab is replaced by an Enterprise upgrade explanation (automation and at-scale tools).
- Per-user outcomes show as status badges; partial exports show an incomplete-source badge. Exported data is never rendered inline: only per-category counts plus a client-built JSON download. No user data goes into URLs, logs or the console. The outcome list covers the current session; durable history is the audit trail.

## User-facing

`pages/PrivacyRights.jsx` has a "Submit a privacy request" section (`services/userPrivacyRequestsApi.ts`: `POST/GET /api/v1/user/me/privacy-requests`) listing the caller's own requests with status and due date. The public (logged-out) form is documented in [webui-public-privacy-form.md](webui-public-privacy-form.md).

## Request detail: action results and tenant scoping

- The inbox `act` call returns HTTP 200 even when the per-user outcome failed. The detail view renders the real result status (completed, already_done, partial, not_found, conflict, forbidden, failed, audit_unavailable); "Action completed" appears only for completed/already_done; `partial` (an export where a source failed) is shown as incomplete, never as success. Export results offer the returned payload as a download.
- Tenant routes are wrapped in `TenantScope` (App.jsx): pages remount per `:tenantSlug` (cleartext exports and pending erasure IDs do not survive a slug change) and a non-superadmin whose session tenant differs from the route slug sees a not-authorized view instead of firing API calls (the server enforces the same boundary).
