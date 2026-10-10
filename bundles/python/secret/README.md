# secret

`!secret <username> <message>` -- one-time secret messaging (feature #684).

Flow: validate + resolve target -> store via hub-api OTS create (#718) -> `chat.delete` the original
message -> `dm.send` a `<webui>/secret#<token>` link (pull page #721). The message is never deleted
before the secret is stored. Flags: `waddles.command-secret` + `waddles.secret-messaging`.

- **Confirmed, not queued:** `relay.push` for `chat.delete`/`dm.send` returns only after the platform
  confirmed the op (the host waits on the outbound drain's result) and raises otherwise. If the delete
  cannot be confirmed the DM is NOT sent and the sender is told the secret was not delivered (the stored
  secret expires unshared); a DM that cannot be confirmed is reported as failed, never as delivered.
- **DM target binding:** the host delivers the DM only to a member of the guild the `!secret` message came
  from, and throttles `dm.send` per community/app and per recipient.

- **Platforms:** Discord only. Twitch `chat.delete`/`dm.send` are `Unsupported` stubs -> the command
  refuses up front (nothing stored, nothing deleted).
- **Config:** `hub_api_url`, `webui_url`. Service token = stage secret-ref
  `SECRET_MESSAGING_SERVICE_TOKEN` (value `Bearer <jwt>`, scope `secret_messaging:create`). The
  `net.http.fqdn:` grant host must match `hub_api_url`'s host -- adjust per deployment.
- **Identity limitation (#429 not landed):** username -> hub UUID goes through community-kv key
  `secret.target.<sha256(lower(name))>` = `{"uuid","platform_user_id"}`. Unlinked target fails loud.
- **Logs:** never the secret, username, target, or token -- op/step/platform/community_id/exc-type only.
