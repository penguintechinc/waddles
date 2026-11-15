//! Manifest hygiene: bundle.yaml and hub-manifest.yaml agree with each other and with the code
//! (consumed tools, permissions, default-off config). Plain-text checks, no YAML dependency.
use waddle_bundle_ai_teammates::ai::CONSUMED;

fn read(name: &str) -> String {
    std::fs::read_to_string(format!("{}/{name}", env!("CARGO_MANIFEST_DIR")))
        .unwrap_or_else(|e| panic!("{name}: {e}"))
}

fn ids(doc: &str) -> Vec<String> {
    doc.lines()
        .filter_map(|l| l.trim().strip_prefix("- id: "))
        .map(|s| s.trim().to_string())
        .collect()
}

#[test]
fn both_manifests_declare_identical_permissions_with_justifications() {
    let (hub, bun) = (read("hub-manifest.yaml"), read("bundle.yaml"));
    let (a, b) = (ids(&hub), ids(&bun));
    assert!(a.len() >= 12, "denominator: {} permission ids", a.len());
    assert_eq!(a, b);
    assert_eq!(
        hub.matches("justification:").count(),
        a.len(),
        "every permission needs a justification"
    );
    assert!(!hub.contains("permissions: []"));
}

#[test]
fn every_consumed_tool_is_permissioned_and_requested_read_only_and_nothing_else() {
    let (hub, bun) = (read("hub-manifest.yaml"), read("bundle.yaml"));
    for doc in [&hub, &bun] {
        for (target, tool) in CONSUMED {
            assert!(
                doc.contains(&format!("id: tool.call:{target}:{tool}")),
                "missing permission for {tool}"
            );
            assert!(
                doc.contains(&format!("tool: {tool}\n")),
                "missing requested_tools entry for {tool}"
            );
        }
        let granted = ids(doc)
            .iter()
            .filter(|i| i.starts_with("tool.call:"))
            .count();
        assert_eq!(
            granted,
            CONSUMED.len(),
            "no tool permission beyond what the code consumes"
        );
        assert!(
            !doc.contains("write: true") && !doc.contains("access: write\n    target_app_id"),
            "consumed tools are read-only"
        );
    }
}

#[test]
fn stages_world_and_commands_match_the_design() {
    let (hub, bun) = (read("hub-manifest.yaml"), read("bundle.yaml"));
    assert!(bun.contains("wit_world: stage-next-tools") && bun.contains("language: rust"));
    for doc in [&hub, &bun] {
        assert!(doc.contains("app_id: waddles.core.example.ai-teammates"));
        assert!(doc.contains("member.join") && doc.contains("chat.message"));
        assert!(
            doc.contains("waddle:bundle/process-stage#transform")
                && doc.contains("waddle:bundle/action-stage#dispatch")
        );
        assert!(
            !doc.contains("egress:\n- "),
            "no network egress; all access via tools"
        );
        for p in [
            "storage.kv",
            "identity.resolve",
            "chat.send:twitch",
            "chat.send:discord",
            "chat.send:youtube",
            "streaming.lifecycle.subscribe",
        ] {
            assert!(doc.contains(&format!("id: {p}")), "{p}");
        }
    }
}

#[test]
fn config_schema_is_closed_and_every_teammate_defaults_off() {
    for name in ["hub-manifest.yaml", "bundle.yaml"] {
        let doc = read(name);
        assert!(doc.contains("config_schema:"));
        for t in ["researcher:", "receptionist:", "producer:"] {
            assert!(doc.contains(&format!("\n    {t}\n")), "{name}: {t}");
        }
        assert_eq!(
            doc.matches("enabled:\n          type: boolean\n          default: false")
                .count(),
            3,
            "{name}: all three default OFF"
        );
        assert!(
            doc.contains("minimum: 5") && doc.contains("maximum: 6"),
            "producer hard limits in schema"
        );
        assert!(doc.contains("admin_actions:") && doc.contains("producer.pause"));
    }
}
