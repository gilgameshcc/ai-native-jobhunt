"""Offline tests of the shipped budget API and CLI; no real applications."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import multiprocessing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/apply_budget.py"
spec = importlib.util.spec_from_file_location("apply_budget", TOOL)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Budget, Refused, initialize = module.Budget, module.Refused, module.initialize


def competing_begin(workspace, gate, results):
    gate.wait()
    try:
        results.put(("won", Budget(workspace).begin()["run_id"]))
    except Refused as error:
        results.put(("refused", error.code))


def competing_reserve(workspace, run, key, at, gate, results):
    gate.wait()
    try:
        results.put(("won", Budget(workspace, lambda: datetime.fromisoformat(at)).reserve(run, key)))
    except Refused as error:
        results.put(("refused", error.code))


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "private-work"
        initialize(self.workspace)
        self.time = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)
        self.budget = Budget(self.workspace, lambda: self.time)
        self.receipts = self.workspace / "runs/mock-receipts.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def update(self, **values):
        config = self.budget.config()
        config.update(values)
        self.budget.policy.write_text(json.dumps(config))

    def submit(self, key, attempt):
        # Independent executor side effect, persisted before readback/record.
        receipt = {"application_key": key, "attempt_id": attempt, "receipt": "mock:" + key}
        with self.receipts.open("a") as stream:
            stream.write(json.dumps(receipt) + "\n")
        return {"status": "confirmed", "evidence": receipt["receipt"]}

    def denied(self, code, callback, *args):
        with self.assertRaises(Refused) as caught:
            callback(*args)
        self.assertEqual(caught.exception.code, code)

    def drain(self, count, start=0):
        run = self.budget.begin()["run_id"]
        for number in range(start, start + count):
            try:
                self.budget.execute(run, f"fictional-employer:role-{number}", self.submit,
                                    {"company": "青栈科技（虚构）", "role": f"角色{number}", "grade": "A",
                                     "channel": "虚构渠道", "material": "fictional-resume"})
            except Refused as error:
                self.assertIn(error.code, ("ROUND_TARGET", "SESSION_LIMIT"))
                self.budget.finish(run, error.code.lower())
                run = self.budget.begin()["run_id"]
                self.budget.execute(run, f"fictional-employer:role-{number}", self.submit)
        return run

    def test_target_100_and_150_use_real_executor_receipts(self):
        for target in (100, 125, 150):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as child:
                budget = initialize(child, target=target)
                receipts = []
                run = budget.begin()["run_id"]
                def submit(key, attempt):
                    receipts.append((key, attempt))
                    return {"status": "confirmed", "evidence": "mock:" + key}
                for index in range(target):
                    try:
                        budget.execute(run, str(index), submit)
                    except Refused as error:
                        self.assertIn(error.code, ("ROUND_TARGET", "SESSION_LIMIT"))
                        budget.finish(run, error.code.lower())
                        run = budget.begin()["run_id"]
                        budget.execute(run, str(index), submit)
                self.denied("DAILY_TARGET", budget.execute, run, "extra", submit)
                self.assertEqual(len(receipts), target)
                self.assertEqual(len(set(key for key, _ in receipts)), target)
                self.assertEqual(budget.status()["confirmed"], target)

    def test_87_then_new_instance_continues_to_150(self):
        self.update(daily_target=150)
        run = self.drain(87)
        self.budget.finish(run, "blocked", "test interrupted after 87 confirmed receipts")
        self.budget = Budget(self.workspace, lambda: self.time)
        self.drain(63, 87)
        records = [json.loads(line) for line in self.receipts.read_text().splitlines()]
        self.assertEqual(len(records), 150)
        self.assertEqual(len({row["application_key"] for row in records}), 150)
        self.assertEqual(self.budget.status()["confirmed"], 150)

    def test_143_confirmed_7_pending_block_extra_until_resolved(self):
        self.update(daily_target=150)
        for index in range(150):
            self.budget.import_record(str(index), "confirmed" if index < 143 else "pending",
                                      self.time.isoformat(), "mock historical receipt")
        run = self.budget.begin()["run_id"]
        self.denied("DAILY_CAP", self.budget.execute, run, "extra", self.submit)
        self.assertEqual(self.budget.status()["occupied"], 150)
        self.budget.resolve("149", "not_sent", "mock explicit no submission")
        self.budget.execute(run, "replacement", self.submit)
        self.assertEqual(self.budget.status()["occupied"], 150)
        self.assertEqual(self.budget.status()["confirmed"], 144)

    def test_follow_up_and_cross_channel_duplicate_not_new_applications(self):
        run = self.budget.begin()["run_id"]
        self.budget.execute(run, "canonical-role", self.submit)
        self.budget.follow_up("canonical-role", "mock follow-up response")
        self.denied("DUPLICATE_OR_UNRESOLVED", self.budget.reserve, run, "canonical-role", {"channel": "other"})
        self.assertEqual(self.budget.status()["confirmed"], 1)

    def test_late_resolution_belongs_to_original_send_day(self):
        run = self.budget.begin()["run_id"]
        self.budget.execute(run, "late", lambda *_: {"status": "pending", "evidence": "mock timeout"})
        original = self.budget.day()
        self.budget.finish(run, "blocked", "test day ended")
        self.time += timedelta(days=1)
        self.budget.resolve("late", "confirmed", "mock previous-day receipt found")
        self.assertEqual(self.budget.status()["confirmed"], 0)
        self.assertEqual(self.budget.status(original)["confirmed"], 1)
        run = self.budget.begin()["run_id"]
        self.denied("DUPLICATE_OR_UNRESOLVED", self.budget.reserve, run, "late")

    def test_risk_survives_midnight_and_resume_has_record(self):
        self.budget.pause("mock risk signal")
        self.time += timedelta(days=1)
        self.denied("RISK_PAUSED", self.budget.begin)
        self.assertTrue(Budget(self.workspace, lambda: self.time).status()["risk_paused"])
        self.budget.resume_risk("mock human explicitly resumed")
        self.budget.begin()

    def test_cap_end_is_not_risk_pause_and_next_day_can_run(self):
        self.update(daily_target=1, daily_cap=1)
        run = self.budget.begin()["run_id"]
        self.budget.execute(run, "one", self.submit)
        self.budget.finish(run, "daily_target")
        self.assertFalse(self.budget.status()["risk_paused"])
        self.time += timedelta(days=1)
        self.budget.execute(self.budget.begin()["run_id"], "two", self.submit)

    def test_no_early_stop_after_one_batch(self):
        run = self.drain(10)
        self.denied("STOP_CONDITION_NOT_MET", self.budget.finish, run, "round_target")
        self.denied("STOP_CONDITION_NOT_MET", self.budget.finish, run, "session_limit")

    def test_expired_owner_is_fenced_pending_send_is_not_retried(self):
        run = self.budget.begin()["run_id"]
        sent = self.budget.reserve(run, "after-click")
        unused = self.budget.reserve(run, "before-click")
        self.budget.dispatch(run, "after-click", sent["attempt_id"])
        self.submit("after-click", sent["attempt_id"])  # crash before record
        self.time += timedelta(seconds=301)
        self.denied("STALE_RUN_VERIFY_EXECUTOR", self.budget.begin)
        recovered = self.budget.begin(recover=True)["run_id"]
        self.denied("OWNER_FENCED", self.budget.dispatch, run, "before-click", unused["attempt_id"])
        self.denied("OWNER_FENCED", self.budget.record, run, "after-click", sent["attempt_id"], "confirmed", "late old owner")
        self.denied("DUPLICATE_OR_UNRESOLVED", self.budget.reserve, recovered, "after-click")
        self.budget.execute(recovered, "before-click", self.submit)
        self.budget.resolve("after-click", "confirmed", "mock receipt read without repeat send")
        self.assertEqual(self.budget.status()["confirmed"], 2)
        self.assertEqual(len(self.receipts.read_text().splitlines()), 2)

    def test_begin_race_has_exactly_one_winner(self):
        # Separate processes, not threads or a mocked lock.
        ctx = multiprocessing.get_context("spawn")
        gate, results = ctx.Event(), ctx.Queue()
        workers = [ctx.Process(target=competing_begin, args=(str(self.workspace), gate, results)) for _ in range(4)]
        for process in workers: process.start()
        gate.set()
        outcomes = [results.get(timeout=15) for _ in workers]
        for process in workers:
            process.join(timeout=15)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(row[0] == "won" for row in outcomes), 1)
        self.assertEqual(sum(row == ("refused", "RUN_BUSY") for row in outcomes), 3)

    def test_reserve_race_cannot_spend_last_slot_twice(self):
        self.update(daily_target=150)
        for index in range(149):
            self.budget.import_record(str(index), "confirmed", self.time.isoformat(), "mock prior result")
        run = self.budget.begin()["run_id"]
        ctx = multiprocessing.get_context("spawn")
        gate, results = ctx.Event(), ctx.Queue()
        workers = [ctx.Process(target=competing_reserve,
                    args=(str(self.workspace), run, f"new-{i}", self.time.isoformat(), gate, results)) for i in range(4)]
        for process in workers: process.start()
        gate.set()
        outcomes = [results.get(timeout=15) for _ in workers]
        for process in workers:
            process.join(timeout=15)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(row[0] == "won" for row in outcomes), 1)
        self.assertEqual(sum(row == ("refused", "DAILY_CAP") for row in outcomes), 3)
        self.assertEqual(self.budget.status()["occupied"], 150)

    def test_known_not_sent_allows_one_retry_and_unknown_allows_none(self):
        run = self.budget.begin()["run_id"]
        not_sent = lambda *_: {"status": "not_sent", "evidence": "mock explicit no request sent"}
        self.budget.execute(run, "failed", not_sent)
        self.assertEqual(self.budget.status()["occupied"], 0)
        self.budget.execute(run, "failed", not_sent)
        self.denied("RETRY_LIMIT", self.budget.execute, run, "failed", self.submit)
        self.budget.execute(run, "unknown", lambda *_: {"status": "pending", "evidence": "mock ambiguous response"})
        self.denied("DUPLICATE_OR_UNRESOLVED", self.budget.execute, run, "unknown", self.submit)

    def test_midnight_cannot_dispatch_yesterdays_reservation(self):
        run = self.budget.begin()["run_id"]
        reserved = self.budget.reserve(run, "midnight")
        self.time = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)
        # Simulate an unusually long, explicitly configured valid lease.
        with sqlite3.connect(self.budget.db) as con:
            con.execute("UPDATE runs SET lease_until=? WHERE id=?", ((self.time + timedelta(seconds=30)).isoformat(), run))
        self.denied("DAY_CHANGED", self.budget.dispatch, run, "midnight", reserved["attempt_id"])
        self.budget.finish(run, "day_changed")
        self.budget.execute(self.budget.begin()["run_id"], "midnight", self.submit)
        self.assertEqual(self.budget.status()["confirmed"], 1)

    def test_config_change_fences_sending_but_allows_result_record(self):
        run = self.budget.begin()["run_id"]
        reservation = self.budget.reserve(run, "reserved")
        self.budget.dispatch(run, "reserved", reservation["attempt_id"])
        self.update(daily_target=150)
        self.denied("CONFIG_CHANGED", self.budget.reserve, run, "next")
        self.budget.record(run, "reserved", reservation["attempt_id"], "confirmed", "mock verified receipt")
        self.assertEqual(self.budget.status()["confirmed"], 1)

    def test_export_preserves_history_and_uses_unique_applications(self):
        original = "# 台账\r\n\r\n用户原话：保留 | 原始记录\r\n"
        ledger = self.workspace / "ledger.md"
        ledger.write_bytes(original.encode())
        self.drain(3)
        self.budget.export_ledger()
        first = ledger.read_text()
        self.budget.follow_up("fictional-employer:role-0", "mock follow-up")
        self.budget.export_ledger()
        self.assertEqual(ledger.read_text(), first)
        self.assertTrue(ledger.read_bytes().startswith(original.encode()))
        self.assertEqual(first.count("| 首次申请 |"), 3)

    def test_old_risk_lock_is_imported_and_cannot_be_cleared_by_editing_cache(self):
        with tempfile.TemporaryDirectory() as child:
            lock = Path(child) / "apply-lock.json"
            lock.write_text(json.dumps({"risk_paused": True, "risk_note": "old risk", "status": "in_progress"}))
            budget = initialize(child)
            lock.write_text(json.dumps({"risk_paused": False, "status": "completed"}))
            self.denied("RISK_PAUSED", Budget(child).begin)
            budget.resume_risk("mock human action")
            self.denied("LEGACY_RUN_UNVERIFIED", budget.begin)
            budget.clear_legacy_run("mock verified old executor stopped")
            budget.begin()

    def test_validation_preserves_existing_policy(self):
        self.update(daily_target=30, daily_cap=30)
        initialize(self.workspace)
        self.assertEqual(self.budget.config()["daily_cap"], 30)
        for values in ({"daily_target": 151}, {"batch_size": None}, {"daily_cap": True}, {"timezone": "+99:99"}, {"schema_version": True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                module.validate({**self.budget.config(), **values})

    def test_legacy_root_pacing_is_not_silently_replaced_with_higher_preset(self):
        with tempfile.TemporaryDirectory() as child:
            old = Path(child) / "pacing.json"
            original = json.dumps({"四层量级上限": {"单日总量上限": {"value": 30}}})
            old.write_text(original)
            with self.assertRaisesRegex(ValueError, "legacy pacing"):
                initialize(child)
            self.assertEqual(old.read_text(), original)
            self.assertFalse((Path(child) / "policies/pacing.json").exists())

    def test_cli_default_initialization_needs_no_placeholder_confirmation(self):
        with tempfile.TemporaryDirectory() as child:
            command = [sys.executable, str(TOOL), "--workspace", child]
            initialized = subprocess.run(command + ["init", "--target", "150"], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(initialized.stdout)["result"]["daily_target"], 150)
            started = subprocess.run(command + ["begin"], capture_output=True, text=True, check=True)
            self.assertTrue(json.loads(started.stdout)["result"]["run_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
