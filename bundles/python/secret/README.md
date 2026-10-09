# secret

`!secret <username> <message>` -- one-time secret messaging (feature #684).

Flow: validate + resolve target -> store via hub-api OTS create (#718) -> `chat.delete` the original
message -> `dm.send` a `<webui>/secret#<token>` link (pull page #721). The message is never deleted
before the secret is stored. Flags: `waddles.command-secret` + `waddles.secret-messaging`.

- **Platforms:** Discord only. Twitch `chat.delete`/`dm.send` are `Unsupported` stubs -> the command
  refuses up front (nothing stored, nothing deleted).
- **Config:** `hub_api_url`, `webui_url`. Service token = stage secret-ref
  `SECRET_MESSAGING_SERVICE_TOKEN` (value `Bearer <jwt>`, scope `secret_messaging:create`). The
  `net.http.fqdn:` grant host must match `hub_api_url`'s host -- adjust per deployment.
- **Identity limitation (#429 not landed):** username -> hub UUID goes through community-kv key
  `secret.target.<sha256(lower(name))>` = `{"uuid","platform_user_id"}`. Unlinked target fails loud.
- **Logs:** never the secret, username, target, or token -- op/step/platform/community_id/exc-type only.
