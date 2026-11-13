# Bring Your Own Discord App (Whitelabel Bot)

Run Waddles in your Discord server under **your own bot name and avatar**. Your tenant gets its own Discord application, so members see your brand, not a shared Waddles bot. Your data and credentials stay isolated to your tenant.

Published by Penguin Technologies, Inc.

## 1. What you get

- A bot that shows **your** name and avatar in Discord.
- Credentials stored only against your tenant, never shared with other tenants.
- The same Waddles features (commands, role sync, event sync) running through your bot.

## 2. Prerequisites

- A Discord account.
- **Manage Server** (or Administrator) permission on every server where you will install the bot.
- A Waddles tenant admin account on a plan that includes Discord bot install (Professional or higher). The shared/global tenant cannot use its own app; it uses the Waddles bot.
- Your Waddles URL: ``<your-waddles-url>``

## 3. Create the application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Enter your brand name. **This is the whitelabel name** members will see on the bot.
3. On **General Information**, upload your avatar (this becomes the bot's picture) and a description, then **Save Changes**.
4. Copy the **Application ID** (shown on General Information).

## 4. Bot tab: create the bot and enable intents

1. Open the **Bot** tab. Set the bot username if it is not already your brand name; upload the avatar here too if prompted.
2. Click **Reset Token**, then copy the token immediately. Discord shows it only once.
3. Under **Privileged Gateway Intents**, turn ON:
   - **Server Members Intent** (required: role sync; without it the gateway closes with code 4014)
   - **Message Content Intent** (required: Waddles requests it to read command text)
4. Waddles connects with these gateway intents: Guilds, Guild Members, Guild Messages, Message Content. Presence is not used and need not be enabled.
5. Click **Save Changes**.

## 5. OAuth2: Client Secret and redirect URI

1. Open **OAuth2**. Copy the **Client ID** (same value as the Application ID).
2. Click **Reset Secret** and copy the **Client Secret** (shown once).
3. Under **Redirects**, click **Add Redirect** and enter this **exact** string, with no trailing slash:

```
<identity-callback-base-url>/api/v1/discord/install/callback
```

The path is always `/api/v1/discord/install/callback`; the prefix is your Waddles identity callback base URL (the operator of your Waddles environment will give you the exact value, including any port, e.g. `:8443`). Click **Save Changes**. A mismatch of even one character (scheme, host, port, path) makes the install fail.

## 6. Enter the values in Waddles

In Waddles, go to **Tenant settings → Discord app** and enter:

| Field | Value |
|---|---|
| Application ID | from step 3 / 5 |
| Client Secret | from step 5 |
| Bot Token | from step 4 (optional at this step if your admin supplies it separately) |

Then submit to continue to Discord for authorization. **Never send the secret or token by email or chat**; enter them only in this form (or have your admin enter them).

## 7. Install the bot into each server

Submitting the form returns you to Discord's authorization page (scopes `bot` and `applications.commands`). Pick the server, review the permissions, and click **Authorize**. Discord then redirects to the callback above and Waddles saves your credentials. Repeat for every server.

Permissions requested (integer `1108906961990`):

- View Channels
- Send Messages
- Embed Links
- Read Message History
- Add Reactions
- Manage Messages
- Manage Roles
- Manage Webhooks
- Manage Events
- Kick Members
- Ban Members
- Moderate Members (timeouts)

Administrator is **not** requested.

## 8. Role order

Discord only lets a bot manage roles **below** its own role. In **Server Settings → Roles**, drag the bot's role (it has your bot's name) **above** every role Waddles should assign or remove. Role sync fails silently on roles above or equal to the bot's.

## 9. Verify it works

1. After authorizing, the callback reports success and the bot appears in the server's member list with your name and avatar.
2. Run a Waddles slash command (type `/` and pick one from your bot) and confirm it replies.
3. If you use role sync, change a test member's role mapping and confirm the bot applies it.

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Discord says **Invalid OAuth2 redirect_uri** | The redirect in the Developer Portal does not exactly match the string in step 5. Re-copy it. |
| Authorization succeeds but Waddles shows a 502 "token exchange failed" | Wrong Client Secret, or redirect mismatch. Reset the secret in the portal and re-enter it. |
| 400 "invalid, expired, already-used, or foreign-browser install state" | The install link expired, was reused, or was opened in a different browser than the one that started it. Start again from Tenant settings and finish in the same browser. |
| 402 "requires a Professional plan or higher" | Your plan does not include bring-your-own Discord app. |
| 400 "global tenant cannot install its own Discord app" | Only private tenants can. |
| 403 on submit | Your Waddles account lacks the tenant Discord-install permission; ask your tenant admin. |
| 409 | A conflict with existing data (for example a duplicate). Re-check the values, and contact support with the request time if it persists. |
| Bot online but no role changes / gateway closes with 4014 | Server Members Intent (or Message Content Intent) is off. Enable it in the Bot tab. |
| Role changes fail or are ignored | The bot's role is below the target roles. See step 8. |
| Bot cannot post in a channel | Channel-level permission overrides deny it View/Send. Adjust the channel. |

## 11. Rotating the token and secret

1. In the Developer Portal: **Bot → Reset Token** and/or **OAuth2 → Reset Secret**. The old value stops working at once.
2. Open **Tenant settings → Discord app** in Waddles, enter the new values, and complete the authorize step again.
3. Confirm the bot reconnects. Rotate immediately if a value is ever exposed.

## 12. Security notes

- Treat the Bot Token and Client Secret like passwords. Never email, chat, screenshot, or commit them.
- Waddles stores them encrypted and scoped to your tenant; they are never shown again after saving.
- Grant the bot only the permissions listed above; do not give it Administrator.
- Limit who has Waddles tenant admin and who can access your Developer Portal application (use a team-owned Discord application where possible).
- Remove the bot from servers you no longer use, and reset credentials when an admin leaves.

## Environment URLs

| Environment | Redirect URI to register |
|---|---|
| Alpha | `https://waddles-alpha.penguintech.cloud:8443/api/v1/discord/install/callback` |
| Other | `<identity-callback-base-url>/api/v1/discord/install/callback` (ask your Waddles contact) |
