//! Unit tests for `command.rs` (Rust-only helpers and edge paths; cross-language
//! parity lives in `tests/command_vectors.rs`).

use super::*;

fn secs(s: &str) -> Result<u64, CommandError> {
    parse_duration(s).map(|d| d.as_secs())
}

#[test]
fn durations_parse_simple_and_compound() {
    assert_eq!(secs("10m"), Ok(600));
    assert_eq!(secs("2h"), Ok(7200));
    assert_eq!(secs("45S"), Ok(45));
    assert_eq!(secs("1d"), Ok(86_400));
    assert_eq!(secs("1w"), Ok(604_800));
    assert_eq!(secs("1h30m"), Ok(5400));
    assert_eq!(secs("1w2d3h4m5s"), Ok(604_800 + 172_800 + 10_800 + 240 + 5));
}

#[test]
fn durations_reject_malformed_input() {
    for bad in [
        "", "m", "10", "0s", "10x", "1m1h", "1h1h", "-5m", "1.5h", "10 m", "1h30",
    ] {
        assert!(matches!(secs(bad), Err(CommandError::Usage(_))), "{bad:?}");
    }
    assert!(secs(&"9".repeat(65)).is_err());
    assert!(secs("99999999999999999999s").is_err());
    assert!(secs("18446744073709551615w").is_err());
}

#[test]
fn mentions_parse_ids_and_handles() {
    assert_eq!(parse_mention("<@123>"), Ok(Mention::Id("123".into())));
    assert_eq!(parse_mention("<@!456>"), Ok(Mention::Id("456".into())));
    assert_eq!(
        parse_mention("@Some_User-1.x"),
        Ok(Mention::Handle("Some_User-1.x".into()))
    );
}

#[test]
fn mentions_reject_malformed_input() {
    for bad in [
        "", "@", "user", "<@>", "<@abc>", "<@!>", "<@12 3>", "@a b", "@a/b", "<@1",
    ] {
        assert!(parse_mention(bad).is_err(), "{bad:?}");
    }
    assert!(parse_mention(&format!("<@{}>", "1".repeat(31))).is_err());
    assert!(parse_mention(&format!("@{}", "a".repeat(65))).is_err());
    assert!(parse_mention(&format!("@{}", "a".repeat(200))).is_err());
}

#[test]
fn shlex_matches_posix_rules() {
    assert_eq!(
        shlex_split(r#"a "b c" 'd e' f\ g"#).unwrap(),
        ["a", "b c", "d e", "f g"]
    );
    assert_eq!(shlex_split(r#""a\"b\\c\d""#).unwrap(), [r#"a"b\c\d"#]);
    assert_eq!(shlex_split("''").unwrap(), [""]);
    assert!(shlex_split("").unwrap().is_empty());
    for bad in ["'x", "\"x", "x\\", "\"x\\"] {
        assert!(shlex_split(bad).is_err(), "{bad:?}");
    }
}

#[test]
fn schema_errors_are_distinct_from_usage_errors() {
    let two = |a: CliArgument, b: CliArgument| CliCommandSpec {
        name: "x".into(),
        arguments: vec![a, b],
        ..Default::default()
    };
    let sub = |n: &str| CliCommandSpec {
        name: n.into(),
        ..Default::default()
    };
    let cases = [
        two(CliArgument::positional("a"), CliArgument::positional("a")),
        two(CliArgument::positional("A"), CliArgument::positional("b")),
        two(
            CliArgument::flag("a", &["bad"]),
            CliArgument::positional("b"),
        ),
        CliCommandSpec {
            name: "x".into(),
            subcommands: vec![sub("s"), sub("s")],
            ..Default::default()
        },
        CliCommandSpec {
            name: "x".into(),
            subcommands: vec![sub("s")],
            default_subcommand: Some("nope".into()),
            ..Default::default()
        },
    ];
    for spec in cases {
        assert!(
            matches!(parse_cli_command("!x", &spec), Err(CommandError::Schema(_))),
            "{spec:?}"
        );
    }
}

#[test]
fn help_lists_arguments_and_subcommands() {
    let spec = CliCommandSpec {
        name: "clip".into(),
        description: "manage clips".into(),
        subcommands: vec![CliCommandSpec {
            name: "set".into(),
            arguments: vec![
                CliArgument::flag("limit", &["--limit"]),
                CliArgument {
                    required: false,
                    ..CliArgument::flag("mode", &["--mode"])
                },
                CliArgument {
                    required: false,
                    ..CliArgument::positional("id")
                },
                CliArgument::positional("name"),
            ],
            ..Default::default()
        }],
        ..Default::default()
    };
    let h = parse_cli_command("!clip set --help", &spec)
        .unwrap()
        .help
        .unwrap();
    for needle in [
        "usage: !clip set",
        "--limit LIMIT",
        "[--mode MODE]",
        "[id]",
        "manage clips",
    ] {
        assert!(h.contains(needle), "{needle} not in {h}");
    }
    assert!(h.contains("Subcommands: set"));
}

#[test]
fn explicit_value_on_help_flag_and_ambiguous_prefix_fail() {
    let spec = CliCommandSpec {
        name: "t".into(),
        arguments: vec![
            CliArgument::flag("alpha", &["--alpha"]),
            CliArgument::flag("alps", &["--alps"]),
        ],
        ..Default::default()
    };
    assert!(parse_cli_command("!t --al 1", &spec).is_err());
    assert!(parse_cli_command("!t --help=1 --alpha 1 --alps 2", &spec).is_err());
    assert!(parse_cli_command("!t --alpha 1 --alps 2", &spec).is_ok());
}

#[test]
fn twenty_digit_integers_do_not_overflow() {
    let spec = CliCommandSpec {
        name: "t".into(),
        arguments: vec![CliArgument {
            kind: CliKind::Integer,
            ..CliArgument::positional("n")
        }],
        ..Default::default()
    };
    let r = parse_cli_command("!t 99999999999999999999", &spec).unwrap();
    assert_eq!(r.values["n"], CliValue::Int(99_999_999_999_999_999_999));
    assert!(parse_cli_command("!t 100000000000000000000", &spec).is_err());
}

#[test]
fn length_cap_applies_to_all_entry_points() {
    let long = format!("!x {}", "a".repeat(MAX_COMMAND_CHARS));
    assert!(parse_command(&long, &CommandSpec::new("x", &[])).is_err());
    let spec = CliCommandSpec {
        name: "x".into(),
        ..Default::default()
    };
    assert!(parse_cli_command(&long, &spec).is_err());
}

#[test]
fn py_repr_matches_python_quoting() {
    assert_eq!(py_repr("a"), "'a'");
    assert_eq!(py_repr("it's"), "\"it's\"");
    assert_eq!(py_repr("it's \"q\""), "'it\\'s \"q\"'");
    assert_eq!(py_repr("a\\b\n\t\r\x01"), "'a\\\\b\\n\\t\\r\\x01'");
}
