"""`cli.suppression` -- the operator lift / purge / stats tool for the suppression list.

Real `init_database` against a sqlite file (the same URL path production takes), real key
derivation. Only `HubAPIConfig.from_env` is pointed at the throwaway database (it also demands a
JWT secret the CLI has no use for).
"""

from __future__ import annotations

import dataclasses
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydal import DAL

from cli import suppression as cli
from config import HubAPIConfig
from services import suppression_keys as keys
from services.schema import bind_suppression_tables
from tests.test_app_factory import _test_config

MASTER_HEX = "ab" * 32
PUID = "d-cli-1"


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(keys.ENV_CURRENT, MASTER_HEX)
    monkeypatch.delenv(keys.ENV_PREVIOUS, raising=False)


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A sqlite file with the table created, and `from_env` pointed at it."""
    path = tmp_path / "cli.db"
    seed = DAL(f"sqlite://{path}", pool_size=0)
    bind_suppression_tables(seed, migrate=True)
    seed(seed.suppression_records).count()
    seed.commit()
    config = dataclasses.replace(_test_config(), database_url=f"sqlite://{path}")
    monkeypatch.setattr(HubAPIConfig, "from_env", classmethod(lambda cls: config))
    return path


def _insert(
    path: Path,
    *,
    tenant_id: int = 1,
    puid: str = PUID,
    reason: str = "ban",
    expires: datetime | None = None,
    master: str = MASTER_HEX,
) -> None:
    code = keys.current_code(
        keys.PlatformKekKeySource({keys.ENV_CURRENT: master}), tenant_id, "discord", puid
    )
    seed = DAL(f"sqlite://{path}", pool_size=0)
    bind_suppression_tables(seed)
    seed.suppression_records.insert(
        tenant_id=tenant_id,
        platform="discord",
        key_ver=code.key_ver,
        hmac=code.hmac,
        reason_enum=reason,
        expires_at=expires,
        created_at=datetime.now(UTC),
    )
    seed.commit()


def _count(path: Path) -> int:
    seed = DAL(f"sqlite://{path}", pool_size=0)
    bind_suppression_tables(seed)
    n = seed(seed.suppression_records.id > 0).count()
    seed.commit()
    return int(n)


def _run(argv: list[str], stdin: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdin=io.StringIO(stdin), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class TestLift:
    def test_deletes_the_identitys_row_in_that_tenant_only(self, db_path: Path) -> None:
        _insert(db_path, tenant_id=1)
        _insert(db_path, tenant_id=2)

        code, out, err = _run(
            ["lift", "--tenant-id", "1", "--platform", "discord", "--platform-user-id-stdin"],
            stdin=f"{PUID}\n",
        )

        assert (code, err) == (cli.EXIT_OK, "")
        assert out == "lift tenant_id=1 platform=discord rows_deleted=1\n"
        assert _count(db_path) == 1  # tenant 2's row is untouched
        assert PUID not in out

    def test_nothing_matched_is_a_non_zero_exit_so_a_typo_cannot_look_like_success(
        self, db_path: Path
    ) -> None:
        _insert(db_path)

        code, out, _ = _run(
            ["lift", "--tenant-id", "1", "--platform", "discord", "--platform-user-id-stdin"],
            stdin="some-other-id\n",
        )

        assert code == cli.EXIT_NOTHING_MATCHED
        assert "rows_deleted=0" in out
        assert _count(db_path) == 1

    def test_the_platform_id_must_come_from_stdin_never_argv(self, db_path: Path) -> None:
        code, _, err = _run(["lift", "--tenant-id", "1", "--platform", "discord"])
        assert code == cli.EXIT_BAD_ARGS
        assert "--platform-user-id-stdin" in err

    @pytest.mark.parametrize(
        ("tenant", "platform", "stdin"),
        [("0", "discord", "x\n"), ("1", "", "x\n"), ("1", "discord", "\n")],
    )
    def test_invalid_identities_are_rejected_without_echo(
        self, db_path: Path, tenant: str, platform: str, stdin: str
    ) -> None:
        _insert(db_path)
        code, _, err = _run(
            ["lift", "--tenant-id", tenant, "--platform", platform, "--platform-user-id-stdin"],
            stdin=stdin,
        )
        assert code == cli.EXIT_BAD_ARGS
        assert PUID not in err
        assert _count(db_path) == 1

    def test_a_missing_key_with_rows_present_cannot_run(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _insert(db_path)
        monkeypatch.delenv(keys.ENV_CURRENT)

        code, _, err = _run(
            ["lift", "--tenant-id", "1", "--platform", "discord", "--platform-user-id-stdin"],
            stdin=f"{PUID}\n",
        )

        assert code == cli.EXIT_CANNOT_RUN
        assert "key unavailable" in err and MASTER_HEX not in err
        assert _count(db_path) == 1  # never reports success while leaving the row behind

    def test_an_unreachable_database_cannot_run_and_leaks_no_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = dataclasses.replace(
            _test_config(), database_url=f"sqlite://{tmp_path / 'missing' / 'x.db'}"
        )
        monkeypatch.setattr(HubAPIConfig, "from_env", classmethod(lambda cls: config))

        code, _, err = _run(["purge"])

        assert code == cli.EXIT_CANNOT_RUN
        assert "could not run" in err


class TestCheck:
    ARGS = ["check", "--tenant-id", "1", "--platform", "discord", "--platform-user-id-stdin"]

    def test_a_live_row_is_reported_without_the_code_or_the_id(self, db_path: Path) -> None:
        _insert(db_path, reason="auto_ban")

        code, out, err = _run(self.ARGS, stdin=f"{PUID}\n")

        assert (code, err) == (cli.EXIT_OK, "")
        assert out == (
            "check tenant_id=1 platform=discord matched=true reason=auto_ban "
            "expires_at=until_lifted\n"
        )
        assert PUID not in out

    def test_a_dated_row_reports_its_expiry(self, db_path: Path) -> None:
        soon = datetime.now(UTC) + timedelta(days=5)
        _insert(db_path, expires=soon)

        code, out, _ = _run(self.ARGS, stdin=f"{PUID}\n")

        assert code == cli.EXIT_OK
        assert "reason=ban expires_at=" in out and "until_lifted" not in out

    def test_no_row_and_an_expired_row_are_both_a_non_match(self, db_path: Path) -> None:
        _insert(db_path, expires=datetime.now(UTC) - timedelta(days=1))

        code, out, _ = _run(self.ARGS, stdin=f"{PUID}\n")

        assert code == cli.EXIT_NOTHING_MATCHED
        assert out == "check tenant_id=1 platform=discord matched=false\n"

    def test_another_tenant_never_matches(self, db_path: Path) -> None:
        _insert(db_path, tenant_id=2)

        assert _run(self.ARGS, stdin=f"{PUID}\n")[0] == cli.EXIT_NOTHING_MATCHED

    def test_it_never_deletes(self, db_path: Path) -> None:
        _insert(db_path)
        _run(self.ARGS, stdin=f"{PUID}\n")
        assert _count(db_path) == 1

    def test_the_id_must_come_from_stdin(self, db_path: Path) -> None:
        code, _, err = _run(["check", "--tenant-id", "1", "--platform", "discord"])
        assert code == cli.EXIT_BAD_ARGS and "--platform-user-id-stdin" in err

    def test_a_missing_key_with_rows_present_cannot_run(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _insert(db_path)
        monkeypatch.delenv(keys.ENV_CURRENT)

        code, _, err = _run(self.ARGS, stdin=f"{PUID}\n")

        assert code == cli.EXIT_CANNOT_RUN and "key unavailable" in err


class TestPurge:
    def test_deletes_expired_rows_and_keeps_the_rest(self, db_path: Path) -> None:
        _insert(db_path, puid="old", expires=datetime.now(UTC) - timedelta(days=1))
        _insert(db_path, puid="live", expires=datetime.now(UTC) + timedelta(days=1))
        _insert(db_path, puid="forever")

        code, out, _ = _run(["purge"])

        assert (code, out) == (cli.EXIT_OK, "purge rows_deleted=1\n")
        assert _count(db_path) == 2


class TestStats:
    def test_counts_live_and_expired_by_tenant_type_and_key_version(self, db_path: Path) -> None:
        other_key = "cd" * 32
        _insert(db_path, tenant_id=1, puid="a")
        _insert(db_path, tenant_id=1, puid="b", reason="auto_ban")
        _insert(db_path, tenant_id=1, puid="c", expires=datetime.now(UTC) - timedelta(days=1))
        _insert(db_path, tenant_id=2, puid="d", master=other_key)

        code, out, _ = _run(["stats"])

        assert code == cli.EXIT_OK
        lines = out.strip().splitlines()
        assert lines[-1] == "TOTAL rows=4 groups=4"
        current = keys.key_version(bytes.fromhex(MASTER_HEX))
        previous = keys.key_version(bytes.fromhex(other_key))
        assert f"tenant_id=1 reason=ban key_ver={current} state=expired rows=1" in lines
        assert f"tenant_id=1 reason=ban key_ver={current} state=live rows=1" in lines
        assert f"tenant_id=1 reason=auto_ban key_ver={current} state=live rows=1" in lines
        assert f"tenant_id=2 reason=ban key_ver={previous} state=live rows=1" in lines
        assert not any(
            "hmac" in line or "a" * 8 in line.replace("tenant_id", "")
            for line in lines[:-1]
            if "key_ver" not in line
        )

    def test_an_empty_list_is_a_valid_printed_answer(self, db_path: Path) -> None:
        code, out, _ = _run(["stats"])
        assert (code, out) == (cli.EXIT_OK, "TOTAL rows=0 groups=0\n")


class TestParser:
    def test_a_command_is_required(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([])

    def test_main_defaults_to_the_process_streams(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["stats"]) == cli.EXIT_OK
        assert "TOTAL rows=0" in capsys.readouterr().out
