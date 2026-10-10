"""The operator verifiers: `cli.verify_audit_chain` (database) and the offline export checker."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, update

from cli import verify_audit_chain as cli
from config import HubAPIConfig
from services.audit_service import AuditService
from tests.audit_support import (
    build_chain,
    make_event,
)
from tests.test_app_factory import _test_config

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_audit_export.py"


async def _seed(service: AuditService, n: int = 5, *, tenant_id: int = 1) -> None:
    for i in range(n):
        await service.record(make_event(action=f"admin.step_{i}", tenant_id=tenant_id))


class TestParsePins:
    def test_parses_repeated_pins(self) -> None:
        pins = cli.parse_pins([f"tenant:1=7:{'a' * 64}", f"platform=2:{'b' * 64}"])
        assert pins["tenant:1"] == cli.Pin(7, "a" * 64)
        assert pins["platform"].seq == 2

    @pytest.mark.parametrize(
        "bad",
        ["tenant:1", "tenant:1=7", f"tenant:1=0:{'a' * 64}", f"tenant:1=7:{'A' * 64}", "=1:x"],
    )
    def test_malformed_pins_are_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError, match="CHAIN=SEQ:HASH"):
            cli.parse_pins([bad])


class TestExitCodes:
    @pytest.mark.parametrize(
        ("summary", "allow_empty", "expected"),
        [
            (cli.RunSummary(2, 2, 0, 0, 10), False, cli.EXIT_OK),
            (cli.RunSummary(2, 1, 1, 0, 10), False, cli.EXIT_BROKEN),
            (cli.RunSummary(2, 1, 1, 0, 10), True, cli.EXIT_BROKEN),
            (cli.RunSummary(0, 0, 0, 0, 0), False, cli.EXIT_NOTHING_EXAMINED),
            (cli.RunSummary(1, 0, 0, 1, 0), False, cli.EXIT_NOTHING_EXAMINED),
            (cli.RunSummary(0, 0, 0, 0, 0), True, cli.EXIT_OK),
        ],
    )
    def test_mapping(self, summary: cli.RunSummary, allow_empty: bool, expected: int) -> None:
        assert summary.exit_code(allow_empty=allow_empty) == expected


class TestVerifyChains:
    async def test_all_intact_reports_denominators(self, audit_service: AuditService) -> None:
        await _seed(audit_service, 4, tenant_id=1)
        await _seed(audit_service, 3, tenant_id=2)
        lines: list[str] = []
        summary = await cli.verify_chains(audit_service, None, {}, lines)
        assert (summary.chains, summary.intact, summary.broken, summary.records) == (2, 2, 0, 7)
        assert len(lines) == 2 and all("status=intact" in line for line in lines)

    async def test_a_tampered_chain_is_flagged_and_others_are_still_checked(
        self,
        audit_service: AuditService,
        audit_dal: Any,
    ) -> None:
        await _seed(audit_service, 4, tenant_id=1)
        await _seed(audit_service, 3, tenant_id=2)
        table = audit_dal.metadata.tables["audit_events"]
        async with audit_dal.engine.begin() as conn:
            await conn.execute(
                update(table)
                .where(table.c.chain_id == "tenant:1", table.c.seq == 2)
                .values(outcome="failure")
            )
        lines: list[str] = []
        summary = await cli.verify_chains(audit_service, None, {}, lines)
        assert (summary.intact, summary.broken) == (1, 1)
        bad = next(line for line in lines if "status=broken" in line)
        assert "first_bad_seq=2" in bad and "reason=hash_mismatch" in bad
        assert summary.exit_code(allow_empty=False) == cli.EXIT_BROKEN

    async def test_pinned_head_catches_a_truncated_tail(
        self,
        audit_service: AuditService,
        audit_dal: Any,
    ) -> None:
        await _seed(audit_service, 5)
        head = await audit_service.head("tenant:1")
        assert head is not None
        table = audit_dal.metadata.tables["audit_events"]
        async with audit_dal.engine.begin() as conn:
            await conn.execute(delete(table).where(table.c.seq > 3))
        pins = {"tenant:1": cli.Pin(head.seq, head.record_hash)}
        lines: list[str] = []
        summary = await cli.verify_chains(audit_service, ["tenant:1"], pins, lines)
        assert summary.broken == 1 and "reason=head_mismatch" in lines[0]

    async def test_no_chains_is_a_failure_not_a_pass(self, audit_service: AuditService) -> None:
        lines: list[str] = []
        summary = await cli.verify_chains(audit_service, None, {}, lines)
        assert summary.chains == 0 and lines == []
        assert summary.exit_code(allow_empty=False) == cli.EXIT_NOTHING_EXAMINED
        assert summary.exit_code(allow_empty=True) == cli.EXIT_OK


class TestRun:
    @pytest.fixture
    def wired(
        self,
        monkeypatch: pytest.MonkeyPatch,
        audit_dal: Any,
    ) -> None:
        async def fake_build(_url: str, pool_size: int) -> Any:
            return audit_dal

        monkeypatch.setattr(cli, "build_install_dal", fake_build)
        monkeypatch.setattr(HubAPIConfig, "from_env", classmethod(lambda cls: _test_config()))

    def _args(self, **overrides: Any) -> argparse.Namespace:
        base = {"chain": [], "expect": [], "allow_empty": False}
        base.update(overrides)
        return argparse.Namespace(**base)

    async def test_clean_run_prints_totals_and_exits_zero(
        self, wired: None, audit_service: AuditService, capsys: pytest.CaptureFixture[str]
    ) -> None:
        await _seed(audit_service, 3)
        assert await cli._run(self._args()) == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "TOTAL chains=1 intact=1 broken=0 empty=0 records_examined=3" in out

    async def test_empty_database_fails_unless_allowed(
        self, wired: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert await cli._run(self._args()) == cli.EXIT_NOTHING_EXAMINED
        assert "zero-denominator" in capsys.readouterr().err

    async def test_allow_empty(self, wired: None) -> None:
        assert await cli._run(self._args(allow_empty=True)) == cli.EXIT_OK

    async def test_bad_pin_is_a_usage_error(
        self, wired: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert await cli._run(self._args(expect=["nonsense"])) == cli.EXIT_NOTHING_EXAMINED
        assert "CHAIN=SEQ:HASH" in capsys.readouterr().err

    async def test_missing_table_exits_3_loudly(
        self,
        wired: None,
        audit_dal: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        audit_dal.metadata.remove(audit_dal.metadata.tables["audit_events"])
        assert await cli._run(self._args()) == cli.EXIT_CANNOT_RUN
        assert "0048_audit_events_hash_chain" in capsys.readouterr().err

    async def test_unexpected_failure_exits_3_and_leaks_no_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        secret = "-".join(["db", "password", "value"])

        async def boom(_url: str, pool_size: int) -> Any:
            raise RuntimeError(f"cannot connect with {secret}")

        monkeypatch.setattr(cli, "build_install_dal", boom)
        monkeypatch.setattr(HubAPIConfig, "from_env", classmethod(lambda cls: _test_config()))
        assert await cli._run(self._args()) == cli.EXIT_CANNOT_RUN
        err = capsys.readouterr().err
        assert "RuntimeError" in err and secret not in err

    def test_main_parses_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[argparse.Namespace] = []

        async def fake_run(args: argparse.Namespace) -> int:
            seen.append(args)
            return 0

        monkeypatch.setattr(cli, "_run", fake_run)
        assert (
            cli.main(["--chain", "tenant:1", "--expect", f"tenant:1=1:{'a' * 64}", "--allow-empty"])
            == 0
        )
        assert seen[0].chain == ["tenant:1"] and seen[0].allow_empty is True


def _write_export(path: Path, records: list[Any], chain_id: str = "tenant:1") -> Path:
    body = {
        "success": True,
        "chain_id": chain_id,
        "hash_version": "sha256-v1",
        "records": [
            {
                "seq": r.seq,
                "event_id": r.event_id,
                "occurred_at": r.occurred_at,
                "actor_uuid": r.actor_uuid,
                "actor_kind": r.actor_kind,
                "category": r.category,
                "action": r.action,
                "outcome": r.outcome,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "details": dict(r.details),
                "prev_hash": r.prev_hash,
                "record_hash": r.record_hash,
                "hash_version": r.hash_version,
            }
            for r in records
        ],
        "manifest": {},
    }
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv built from tmp paths, no shell
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


class TestOfflineExportVerifier:
    def test_intact_export_verifies(self, tmp_path: Path) -> None:
        records = build_chain(8)
        export = _write_export(tmp_path / "e.json", records)
        result = _run_script(str(export))
        assert result.returncode == 0, result.stderr
        assert "status=intact" in result.stdout and "records_examined=8" in result.stdout

    def test_paged_export_files_verify_in_order(self, tmp_path: Path) -> None:
        records = build_chain(9)
        first = _write_export(tmp_path / "p1.json", records[:4])
        second = _write_export(tmp_path / "p2.json", records[4:])
        result = _run_script(str(first), str(second))
        assert result.returncode == 0, result.stderr
        assert "records_examined=9" in result.stdout

    def test_a_mutated_record_in_the_file_is_tamper_detected(self, tmp_path: Path) -> None:
        records = build_chain(6)
        export = _write_export(tmp_path / "e.json", records)
        body = json.loads(export.read_text())
        body["records"][3]["outcome"] = "denied"
        export.write_text(json.dumps(body))
        result = _run_script(str(export))
        assert result.returncode == 1
        assert "TAMPER DETECTED at seq 4" in result.stdout and "hash_mismatch" in result.stdout

    def test_a_removed_record_in_the_file_is_tamper_detected(self, tmp_path: Path) -> None:
        records = build_chain(6)
        del records[2]
        result = _run_script(str(_write_export(tmp_path / "e.json", records)))
        assert result.returncode == 1 and "seq_gap" in result.stdout

    def test_mid_chain_slice_needs_a_trusted_anchor(self, tmp_path: Path) -> None:
        records = build_chain(8)
        export = _write_export(tmp_path / "e.json", records[4:])
        missing = _run_script(str(export))
        assert missing.returncode == 2 and "--anchor-hash" in missing.stderr
        ok = _run_script(str(export), "--anchor-hash", records[3].record_hash)
        assert ok.returncode == 0, ok.stderr
        forged = _run_script(str(export), "--anchor-hash", "a" * 64)
        assert forged.returncode == 1

    def test_pinned_head_detects_a_truncated_tail(self, tmp_path: Path) -> None:
        records = build_chain(8)
        export = _write_export(tmp_path / "e.json", records[:5])
        assert _run_script(str(export)).returncode == 0  # honest limit of the chain alone
        pinned = _run_script(
            str(export),
            "--expect-head-seq",
            "8",
            "--expect-head-hash",
            records[-1].record_hash,
        )
        assert pinned.returncode == 1 and "head_mismatch" in pinned.stdout

    def test_empty_export_is_not_a_pass(self, tmp_path: Path) -> None:
        result = _run_script(str(_write_export(tmp_path / "e.json", [])))
        assert result.returncode == 2 and "nothing was verified" in result.stderr

    def test_garbage_and_mismatched_chains_are_usage_errors(self, tmp_path: Path) -> None:
        junk = tmp_path / "junk.json"
        junk.write_text("{not json")
        assert _run_script(str(junk)).returncode == 2
        wrong_shape = tmp_path / "shape.json"
        wrong_shape.write_text(json.dumps({"hello": "world"}))
        assert _run_script(str(wrong_shape)).returncode == 2
        a = _write_export(tmp_path / "a.json", build_chain(2, chain_id="tenant:1"))
        b = _write_export(tmp_path / "b.json", build_chain(2, chain_id="tenant:2"), "tenant:2")
        mixed = _run_script(str(a), str(b))
        assert mixed.returncode == 2 and "differs" in mixed.stderr
        assert _run_script(str(tmp_path / "missing.json")).returncode == 2

    def test_loader_reuses_the_production_hash_code(self) -> None:
        sys.path.insert(0, str(SCRIPT.parent))
        try:
            import verify_audit_export as script
        finally:
            sys.path.remove(str(SCRIPT.parent))
        chain = script.load_chain_module()
        record = build_chain(1)[0]
        rebuilt = chain.ChainRecord(**{f: getattr(record, f) for f in record.__dataclass_fields__})
        assert chain.compute_hash(rebuilt) == record.record_hash  # identical canonical form
