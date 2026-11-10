//! Golden-vector parity: reads the same `sdk/testdata/command-vectors.json`
//! fixture as the Python suite (`waddle-sdk/tests/test_command_vectors.py`).

use std::collections::BTreeMap;
use std::path::PathBuf;

use serde_json::Value;
use waddle_sdk::command::{
    CliArgument, CliCommandSpec, CliKind, CliValue, CommandError, CommandSpec,
    extract_placeholders, parse_cli_command, parse_command, substitute_placeholders,
};

fn vectors() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../testdata/command-vectors.json");
    let raw = std::fs::read_to_string(&path).expect("fixture readable");
    serde_json::from_str(&raw).expect("fixture is valid JSON")
}

fn opt_str(v: &Value) -> Option<String> {
    v.as_str().map(str::to_string)
}

fn cli_value(v: &Value) -> CliValue {
    match v {
        Value::Null => CliValue::Absent,
        Value::String(s) => CliValue::Str(s.clone()),
        Value::Number(n) => CliValue::Int(
            n.to_string()
                .parse::<i128>()
                .expect("integer fixture value"),
        ),
        other => panic!("unexpected fixture value {other}"),
    }
}

fn cli_spec(d: &Value) -> CliCommandSpec {
    CliCommandSpec {
        name: d["name"].as_str().unwrap().to_string(),
        arguments: d["arguments"]
            .as_array()
            .map(|a| {
                a.iter()
                    .map(|a| CliArgument {
                        name: a["name"].as_str().unwrap().to_string(),
                        flags: a["flags"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .map(|f| f.as_str().unwrap().to_string())
                            .collect(),
                        required: a["required"].as_bool().unwrap(),
                        kind: if a["kind"] == "integer" {
                            CliKind::Integer
                        } else {
                            CliKind::String
                        },
                        default: cli_value(&a["default"]),
                        help: String::new(),
                    })
                    .collect()
            })
            .unwrap_or_default(),
        subcommands: d["subcommands"]
            .as_array()
            .map(|s| s.iter().map(cli_spec).collect())
            .unwrap_or_default(),
        default_subcommand: opt_str(&d["default_subcommand"]),
        description: String::new(),
    }
}

#[test]
fn parse_command_vectors() {
    let v = vectors();
    let cases = v["parse_command"].as_array().unwrap();
    assert!(cases.len() >= 40, "denominator: {}", cases.len());
    for c in cases {
        let subs: Vec<&str> = c["spec"]["sub_modules"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s.as_str().unwrap())
            .collect();
        let spec = CommandSpec::new(c["spec"]["name"].as_str().unwrap(), &subs);
        let text = c["text"].as_str().unwrap();
        let got = parse_command(text, &spec);
        if let Some(err) = c.get("error") {
            assert_eq!(
                got,
                Err(CommandError::Usage(err.as_str().unwrap().to_string())),
                "text {text:?}"
            );
        } else {
            let r = got.unwrap_or_else(|e| panic!("text {text:?}: {e}"));
            let e = &c["expect"];
            assert_eq!(r.command, e["command"].as_str().unwrap(), "{text:?}");
            assert_eq!(r.sub_module, opt_str(&e["sub_module"]), "{text:?}");
            assert_eq!(r.option, opt_str(&e["option"]), "{text:?}");
            assert_eq!(r.args, opt_str(&e["args"]), "{text:?}");
        }
    }
}

#[test]
fn placeholder_vectors() {
    let v = vectors();
    let cases = v["placeholders"].as_array().unwrap();
    assert!(cases.len() >= 5);
    for c in cases {
        let tpl = c["template"].as_str().unwrap();
        let values: BTreeMap<String, String> = c["values"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string()))
            .collect();
        let want: Vec<String> = c["extract"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s.as_str().unwrap().to_string())
            .collect();
        assert_eq!(extract_placeholders(tpl), want, "{tpl:?}");
        let got = substitute_placeholders(tpl, &values);
        match c.get("missing") {
            Some(m) => assert_eq!(got.unwrap_err().0, m.as_str().unwrap(), "{tpl:?}"),
            None => assert_eq!(got.unwrap(), c["substitute"].as_str().unwrap(), "{tpl:?}"),
        }
    }
}

#[test]
fn cli_vectors() {
    let v = vectors();
    let cases = v["cli"].as_array().unwrap();
    assert!(cases.len() >= 40, "denominator: {}", cases.len());
    for c in cases {
        let spec = cli_spec(&c["spec"]);
        let text = c["text"].as_str().unwrap();
        let got = parse_cli_command(text, &spec);
        if c.get("error").is_some() {
            assert!(
                matches!(got, Err(CommandError::Usage(_))),
                "text {text:?}: {got:?}"
            );
            continue;
        }
        let r = got.unwrap_or_else(|e| panic!("text {text:?}: {e}"));
        let e = &c["expect"];
        assert_eq!(r.command, e["command"].as_str().unwrap(), "{text:?}");
        assert_eq!(r.subcommand, opt_str(&e["subcommand"]), "{text:?}");
        if e["help"].as_bool().unwrap() {
            assert!(r.help.is_some_and(|h| !h.is_empty()), "{text:?}");
        } else {
            assert!(r.help.is_none(), "{text:?}");
            let want: BTreeMap<String, CliValue> = e["values"]
                .as_object()
                .unwrap()
                .iter()
                .map(|(k, v)| (k.clone(), cli_value(v)))
                .collect();
            assert_eq!(r.values, want, "{text:?}");
        }
    }
}
