#!/usr/bin/env python3
"""Local application accounting. No browser, credentials, network or scheduler.

Python standard library only. See modules/02-apply/GOVERNANCE.md section 7.
All cooperating senders use reserve -> dispatch -> record. SQLite serializes
reservations; it cannot prevent a browser operation performed outside this API.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid


class Refused(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".apply-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def offset(value):
    if not isinstance(value, str) or not re.fullmatch(r"[+-]\d{2}:\d{2}", value):
        raise ValueError("timezone must be a fixed UTC offset, e.g. +08:00")
    hours, minutes = int(value[1:3]), int(value[4:6])
    if hours > 14 or minutes > 59 or (hours == 14 and minutes):
        raise ValueError("invalid UTC offset")
    return timezone(timedelta(minutes=(hours * 60 + minutes) * (-1 if value[0] == "-" else 1)))


def validate(config):
    if type(config.get("schema_version")) is not int or config["schema_version"] != 1:
        raise ValueError("unsupported pacing schema; migrate old placeholders first")
    for key in ("daily_target", "daily_cap", "round_target", "batch_size",
                "session_max_batches", "concurrency", "lease_seconds"):
        if type(config.get(key)) is not int or config[key] < 1:
            raise ValueError(key + " must be a positive integer")
    if config["daily_target"] > config["daily_cap"]:
        raise ValueError("daily_target exceeds daily_cap")
    if config["concurrency"] != 1:
        raise ValueError("this local helper supports one active run")
    offset(config.get("timezone"))
    return config


def initialize(workspace, target=None, cap=None):
    workspace = Path(workspace).resolve()
    policy = workspace / "policies/pacing.json"
    existing = policy.exists()
    if not existing and (workspace / "pacing.json").exists():
        raise ValueError("legacy pacing.json exists; migrate its actual settings to policies/pacing.json first")
    source = policy if existing else Path(__file__).resolve().parents[1] / "modules/02-apply/config/pacing.json"
    config = json.loads(source.read_text(encoding="utf-8"))
    old = dict(config)
    if target is not None:
        config["daily_target"] = target
    if cap is not None:
        config["daily_cap"] = cap
    validate(config)
    if not existing or config != old:
        atomic_text(policy, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        changes = workspace / "policies/changes.md"
        with changes.open("a", encoding="utf-8") as stream:
            stream.write("\n- pacing: " + json.dumps({"old": old if existing else None,
                         "new": config, "source": "explicit arguments" if target is not None or cap is not None
                         else "module default"}, ensure_ascii=False) + "\n")
    return Budget(workspace)


class Budget:
    def __init__(self, workspace, clock=None):
        self.workspace = Path(workspace).resolve()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._operation = ContextVar("apply_budget_operation", default=None)
        self.policy = self.workspace / "policies/pacing.json"
        self.config()
        self.db = self.workspace / "runs/apply-state.sqlite3"
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db, timeout=5) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL,
                    day TEXT NOT NULL, lease_until TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    config_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS applications (
                    key TEXT PRIMARY KEY, attempt TEXT NOT NULL, run_id TEXT,
                    status TEXT NOT NULL, day TEXT NOT NULL, sent_at TEXT,
                    evidence TEXT NOT NULL, details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL,
                    application_key TEXT, run_id TEXT, detail TEXT NOT NULL);
            """)
            con.execute("BEGIN IMMEDIATE")
            if not self._get(con, "initialized"):
                legacy = self.workspace / "apply-lock.json"
                if legacy.exists():
                    state = json.loads(legacy.read_text(encoding="utf-8"))
                    if state.get("risk_paused"):
                        self._set(con, "risk", state.get("risk_note") or "legacy risk pause")
                    if state.get("status") == "in_progress":
                        self._set(con, "legacy_busy", "unverified legacy run")
                self._set(con, "initialized", "1")

    def config(self):
        if self._operation.get():
            return self._operation.get()["config"]
        return validate(json.loads(self.policy.read_text(encoding="utf-8")))

    def config_hash(self):
        if self._operation.get():
            return self._operation.get()["hash"]
        return hashlib.sha256(self.policy.read_bytes()).hexdigest()

    def now(self):
        now = self._operation.get()["now"] if self._operation.get() else self.clock()
        if now.tzinfo is None:
            raise ValueError("clock must include timezone")
        return now.astimezone(timezone.utc)

    def day(self):
        return self.now().astimezone(offset(self.config()["timezone"])).date().isoformat()

    @contextmanager
    def transaction(self):
        con = sqlite3.connect(self.db, timeout=5, isolation_level=None)
        con.row_factory = sqlite3.Row
        token = None
        try:
            con.execute("BEGIN IMMEDIATE")
            raw = self.policy.read_bytes()
            token = self._operation.set({"config": validate(json.loads(raw)),
                                         "hash": hashlib.sha256(raw).hexdigest(), "now": self.clock()})
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            if token is not None:
                self._operation.reset(token)
            con.close()

    @staticmethod
    def _get(con, key):
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    @staticmethod
    def _set(con, key, value):
        con.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def _event(self, con, kind, key=None, run=None, **detail):
        con.execute("INSERT INTO events(at,kind,application_key,run_id,detail) VALUES (?,?,?,?,?)",
                    (self.now().isoformat(), kind, key, run, json.dumps(detail, ensure_ascii=False)))

    def _guard(self, con, run, sending=False):
        row = con.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
        if not row or row["status"] != "in_progress" or self._get(con, "current_run") != run:
            raise Refused("OWNER_FENCED")
        if datetime.fromisoformat(row["lease_until"]) <= self.now():
            raise Refused("OWNER_EXPIRED")
        if sending:
            if self._get(con, "risk"):
                raise Refused("RISK_PAUSED")
            if row["day"] != self.day():
                raise Refused("DAY_CHANGED")
            if row["config_hash"] != self.config_hash():
                raise Refused("CONFIG_CHANGED")
        con.execute("UPDATE runs SET lease_until=? WHERE id=?", ((self.now() + timedelta(seconds=self.config()["lease_seconds"])).isoformat(), run))
        return row

    def _counts(self, con, day):
        counts = dict(con.execute("SELECT status,count(*) FROM applications WHERE day=? GROUP BY status", (day,)).fetchall())
        success = counts.get("confirmed", 0)
        occupied = sum(counts.get(s, 0) for s in ("confirmed", "pending", "sending", "reserved"))
        return {"confirmed": success, "pending": counts.get("pending", 0),
                "sending": counts.get("sending", 0), "reserved": counts.get("reserved", 0), "occupied": occupied}

    def _limit(self, con, run):
        cfg, counts = self.config(), self._counts(con, self.day())
        if counts["confirmed"] >= cfg["daily_target"]:
            raise Refused("DAILY_TARGET")
        if counts["occupied"] >= cfg["daily_cap"]:
            raise Refused("DAILY_CAP")
        successes = con.execute("SELECT count(*) FROM applications WHERE run_id=? AND status='confirmed'", (run["id"],)).fetchone()[0]
        if successes >= cfg["round_target"]:
            raise Refused("ROUND_TARGET")
        reserved = con.execute("SELECT count(*) FROM applications WHERE run_id=? AND status='reserved'", (run["id"],)).fetchone()[0]
        if run["attempts"] + reserved >= cfg["batch_size"] * cfg["session_max_batches"]:
            raise Refused("SESSION_LIMIT")

    def begin(self, owner=None, recover=False):
        with self.transaction() as con:
            if self._get(con, "risk"):
                raise Refused("RISK_PAUSED")
            if self._get(con, "legacy_busy"):
                raise Refused("LEGACY_RUN_UNVERIFIED")
            current = self._get(con, "current_run")
            old = con.execute("SELECT * FROM runs WHERE id=? AND status='in_progress'", (current,)).fetchone()
            if old:
                if datetime.fromisoformat(old["lease_until"]) > self.now():
                    raise Refused("RUN_BUSY")
                if not recover:
                    raise Refused("STALE_RUN_VERIFY_EXECUTOR")
                con.execute("UPDATE applications SET status='not_sent',evidence='expired before dispatch' WHERE run_id=? AND status='reserved'", (current,))
                con.execute("UPDATE applications SET status='pending',evidence='owner expired after dispatch' WHERE run_id=? AND status='sending'", (current,))
                con.execute("UPDATE runs SET status='expired' WHERE id=?", (current,))
                self._event(con, "recover", run=current)
            run = str(uuid.uuid4())
            con.execute("INSERT INTO runs(id,owner,status,day,lease_until,config_hash) VALUES (?,?,?,?,?,?)",
                        (run, owner or str(uuid.uuid4()), "in_progress", self.day(),
                         (self.now() + timedelta(seconds=self.config()["lease_seconds"])).isoformat(), self.config_hash()))
            self._set(con, "current_run", run)
            self._event(con, "begin", run=run)
            return {"run_id": run, "day": self.day(), "config_hash": self.config_hash()}

    def heartbeat(self, run):
        with self.transaction() as con:
            self._guard(con, run)
            con.execute("UPDATE runs SET lease_until=? WHERE id=?", ((self.now() + timedelta(seconds=self.config()["lease_seconds"])).isoformat(), run))

    def reserve(self, run, key, details=None):
        if not isinstance(key, str) or not key.strip():
            raise ValueError("stable application key required")
        if details is not None and not isinstance(details, dict):
            raise ValueError("details must be a JSON object")
        with self.transaction() as con:
            row = self._guard(con, run, sending=True)
            previous = con.execute("SELECT status FROM applications WHERE key=?", (key,)).fetchone()
            if previous and previous["status"] != "not_sent":
                raise Refused("DUPLICATE_OR_UNRESOLVED")
            if con.execute("SELECT count(*) FROM events WHERE application_key=? AND kind='dispatch'", (key,)).fetchone()[0] >= 2:
                raise Refused("RETRY_LIMIT")
            self._limit(con, row)
            attempt = str(uuid.uuid4())
            con.execute("INSERT INTO applications VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET attempt=excluded.attempt,run_id=excluded.run_id,status=excluded.status,day=excluded.day,sent_at=NULL,evidence='',details=excluded.details",
                        (key, attempt, run, "reserved", self.day(), None, "", json.dumps(details or {}, ensure_ascii=False)))
            self._event(con, "reserve", key, run, attempt=attempt)
            return {"application_key": key, "attempt_id": attempt}

    def dispatch(self, run, key, attempt):
        with self.transaction() as con:
            row = self._guard(con, run, sending=True)
            application = con.execute("SELECT * FROM applications WHERE key=?", (key,)).fetchone()
            if not application or application["attempt"] != attempt or application["run_id"] != run or application["status"] != "reserved":
                raise Refused("RESERVATION_REQUIRED")
            cfg = self.config()
            counts = self._counts(con, self.day())
            if counts["confirmed"] >= cfg["daily_target"] or counts["occupied"] > cfg["daily_cap"]:
                raise Refused("DAILY_TARGET_OR_CAP")
            if row["attempts"] >= cfg["batch_size"] * cfg["session_max_batches"]:
                raise Refused("SESSION_LIMIT")
            if con.execute("SELECT count(*) FROM applications WHERE run_id=? AND status='confirmed'", (run,)).fetchone()[0] >= cfg["round_target"]:
                raise Refused("ROUND_TARGET")
            con.execute("UPDATE applications SET status='sending',sent_at=? WHERE key=?", (self.now().isoformat(), key))
            con.execute("UPDATE runs SET attempts=attempts+1 WHERE id=?", (run,))
            self._event(con, "dispatch", key, run, attempt=attempt)

    def record(self, run, key, attempt, outcome, evidence):
        if outcome not in ("confirmed", "pending", "not_sent") or not evidence:
            raise ValueError("outcome and result evidence required")
        with self.transaction() as con:
            self._guard(con, run)
            row = con.execute("SELECT * FROM applications WHERE key=?", (key,)).fetchone()
            if not row or row["attempt"] != attempt or row["run_id"] != run or row["status"] != "sending":
                raise Refused("NOT_CURRENT_DISPATCH")
            con.execute("UPDATE applications SET status=?,evidence=? WHERE key=?", (outcome, evidence, key))
            self._event(con, "result", key, run, outcome=outcome, evidence=evidence)

    def execute(self, run, key, submit, details=None):
        """Use a caller-supplied, already authorized executor. Never retry writes."""
        reservation = self.reserve(run, key, details)
        self.dispatch(run, key, reservation["attempt_id"])
        try:
            result = submit(key, reservation["attempt_id"])
            outcome, evidence = result["status"], result["evidence"]
            if outcome not in ("confirmed", "pending", "not_sent") or not evidence:
                raise ValueError("unverified executor result")
        except Exception as error:
            outcome, evidence = "pending", "executor result unknown: " + type(error).__name__
        self.record(run, key, reservation["attempt_id"], outcome, evidence)
        return {**reservation, "status": outcome, "evidence": evidence}

    def resolve(self, key, outcome, evidence):
        if outcome not in ("confirmed", "not_sent") or not evidence:
            raise ValueError("resolution requires verified result evidence")
        with self.transaction() as con:
            row = con.execute("SELECT * FROM applications WHERE key=?", (key,)).fetchone()
            if not row or row["status"] != "pending":
                raise Refused("PENDING_RESULT_REQUIRED")
            con.execute("UPDATE applications SET status=?,evidence=? WHERE key=?", (outcome, evidence, key))
            self._event(con, "resolve", key, row["run_id"], outcome=outcome, evidence=evidence)

    def import_record(self, key, outcome, occurred_at, evidence, details=None):
        if outcome not in ("confirmed", "pending") or not evidence or not key:
            raise ValueError("verified historical key, date and evidence required")
        when = datetime.fromisoformat(occurred_at)
        if when.tzinfo is None:
            raise ValueError("historical send time requires timezone")
        day = when.astimezone(offset(self.config()["timezone"])).date().isoformat()
        with self.transaction() as con:
            if con.execute("SELECT 1 FROM applications WHERE key=?", (key,)).fetchone():
                raise Refused("HISTORY_ALREADY_REGISTERED")
            con.execute("INSERT INTO applications VALUES (?,?,?,?,?,?,?,?)", (key, str(uuid.uuid4()), None,
                        outcome, day, when.isoformat(), evidence, json.dumps(details or {}, ensure_ascii=False)))
            self._event(con, "import", key, outcome=outcome, day=day, evidence=evidence)

    def follow_up(self, key, evidence):
        if not evidence:
            raise ValueError("contact evidence required")
        with self.transaction() as con:
            if not con.execute("SELECT 1 FROM applications WHERE key=?", (key,)).fetchone():
                raise Refused("APPLICATION_NOT_REGISTERED")
            self._event(con, "follow_up", key, evidence=evidence)

    def finish(self, run, reason, evidence=""):
        with self.transaction() as con:
            row = self._guard(con, run)
            cfg, counts = self.config(), self._counts(con, self.day())
            success = con.execute("SELECT count(*) FROM applications WHERE run_id=? AND status='confirmed'", (run,)).fetchone()[0]
            allowed = {"daily_target": counts["confirmed"] >= cfg["daily_target"],
                       "daily_cap": counts["occupied"] >= cfg["daily_cap"],
                       "round_target": success >= cfg["round_target"],
                       "session_limit": row["attempts"] >= cfg["batch_size"] * cfg["session_max_batches"],
                       "risk_paused": bool(self._get(con, "risk")),
                       "day_changed": row["day"] != self.day(),
                       "blocked": bool(evidence), "pool_exhausted": bool(evidence)}
            if not allowed.get(reason):
                raise Refused("STOP_CONDITION_NOT_MET")
            con.execute("UPDATE applications SET status='not_sent',evidence='released before dispatch' WHERE run_id=? AND status='reserved'", (run,))
            con.execute("UPDATE applications SET status='pending',evidence='run ended before result verification' WHERE run_id=? AND status='sending'", (run,))
            con.execute("UPDATE runs SET status=? WHERE id=?", ("pending" if reason in ("blocked", "risk_paused") else "completed", run))
            self._set(con, "current_run", "")
            self._event(con, "finish", run=run, reason=reason, evidence=evidence)

    def pause(self, note):
        if not note:
            raise ValueError("risk signal required")
        with self.transaction() as con:
            self._set(con, "risk", note)
            self._event(con, "risk_pause", note=note)

    def resume_risk(self, reason):
        if not reason:
            raise ValueError("explicit human recovery reason required")
        with self.transaction() as con:
            self._set(con, "risk", "")
            self._event(con, "risk_resume", reason=reason)

    def clear_legacy_run(self, evidence):
        if not evidence:
            raise ValueError("verify the old executor has stopped first")
        with self.transaction() as con:
            self._set(con, "legacy_busy", "")
            self._event(con, "legacy_run_verified", evidence=evidence)

    def status(self, day=None):
        with self.transaction() as con:
            day = day or self.day()
            cfg = self.config()
            counts = self._counts(con, day)
            current = self._get(con, "current_run")
            run = con.execute("SELECT * FROM runs WHERE id=?", (current,)).fetchone()
            return {"day": day, **counts, "daily_target": cfg["daily_target"], "daily_cap": cfg["daily_cap"],
                    "remaining_budget": max(0, cfg["daily_cap"] - counts["occupied"]),
                    "target_remaining": max(0, cfg["daily_target"] - counts["confirmed"]),
                    "risk_paused": bool(self._get(con, "risk")), "risk_note": self._get(con, "risk"),
                    "run_id": current, "owner": run["owner"] if run else None,
                    "lease_until": run["lease_until"] if run else None,
                    "run_attempts": run["attempts"] if run else 0, "config_hash": self.config_hash()}

    def export_ledger(self):
        """Refresh only the managed section, preserving all original contact notes."""
        start, end = "<!-- apply-budget:start -->", "<!-- apply-budget:end -->"
        def cell(value):
            return str(value).replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        with self.transaction() as con:
            rows = con.execute("SELECT * FROM applications WHERE status != 'not_sent' ORDER BY day,key").fetchall()
            lines = [start, "## 申请发送记录（自动维护）", "", "数据源：`runs/apply-state.sqlite3`。同岗位首次申请一行，跟进记在原台账；结果不明不计成功。", "",
                     "| 日期 | 公司 | 岗位 | 级别 | 渠道 | 用的哪一版 | 状态 | 下一步 | 对方回应原话 | 反向吸引 | application_key | 事件类型 |",
                     "|---|---|---|---|---|---|---|---|---|---|---|---|"]
            names = {"confirmed": "已投递", "pending": "结果待核实", "sending": "发送中", "reserved": "已占额未发送"}
            for row in rows:
                details = json.loads(row["details"])
                values = [row["day"], details.get("company", "未知"), details.get("role", row["key"]), details.get("grade", "未知"),
                          details.get("channel", "未知"), details.get("material", "未知"), names[row["status"]],
                          "等待回应" if row["status"] == "confirmed" else "只读核实结果", "", "", row["key"], "首次申请"]
                lines.append("| " + " | ".join(cell(v) for v in values) + " |")
            lines.append(end)
            ledger = self.workspace / "ledger.md"
            if ledger.exists():
                with ledger.open(encoding="utf-8", newline="") as stream:
                    original = stream.read()
            else:
                original = "# ledger.md · 台账\n"
            block = "\n".join(lines)
            if start in original or end in original:
                if original.count(start) != 1 or original.count(end) != 1 or original.index(end) < original.index(start):
                    raise ValueError("ambiguous managed ledger section; original preserved")
                original = original[:original.index(start)] + block + original[original.index(end) + len(end):]
            else:
                original += "\n\n" + block + "\n"
            atomic_text(ledger, original)
            return {"ledger": str(ledger), "applications": len(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--target", type=int)
    init.add_argument("--cap", type=int)
    begin = sub.add_parser("begin")
    begin.add_argument("--owner")
    begin.add_argument("--recover", action="store_true")
    for name in ("reserve", "dispatch", "record", "heartbeat", "finish"):
        command = sub.add_parser(name)
        command.add_argument("--run", required=True)
        if name in ("reserve", "dispatch", "record"):
            command.add_argument("--key", required=True)
        if name in ("dispatch", "record"):
            command.add_argument("--attempt", required=True)
        if name == "record":
            command.add_argument("--outcome", choices=("confirmed", "pending", "not_sent"), required=True)
            command.add_argument("--evidence", required=True)
        if name == "reserve":
            command.add_argument("--details", default="{}", help="JSON company/role/grade/channel/material")
        if name == "finish":
            command.add_argument("--reason", required=True)
            command.add_argument("--evidence", default="")
    status = sub.add_parser("status")
    status.add_argument("--day")
    sub.add_parser("export")
    for name in ("pause", "resume-risk", "clear-legacy-run"):
        sub.add_parser(name).add_argument("--evidence", required=True)
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--key", required=True)
    resolve.add_argument("--outcome", choices=("confirmed", "not_sent"), required=True)
    resolve.add_argument("--evidence", required=True)
    imported = sub.add_parser("import")
    imported.add_argument("--key", required=True)
    imported.add_argument("--outcome", choices=("confirmed", "pending"), required=True)
    imported.add_argument("--at", required=True, help="actual historical send time with UTC offset")
    imported.add_argument("--evidence", required=True)
    follow = sub.add_parser("follow-up")
    follow.add_argument("--key", required=True)
    follow.add_argument("--evidence", required=True)
    args = parser.parse_args()
    try:
        budget = initialize(args.workspace, args.target, args.cap) if args.command == "init" else Budget(args.workspace)
        cmd = args.command
        if cmd == "init": result = budget.status()
        elif cmd == "begin": result = budget.begin(args.owner, args.recover)
        elif cmd == "reserve": result = budget.reserve(args.run, args.key, json.loads(args.details))
        elif cmd == "dispatch": result = budget.dispatch(args.run, args.key, args.attempt)
        elif cmd == "record": result = budget.record(args.run, args.key, args.attempt, args.outcome, args.evidence)
        elif cmd == "heartbeat": result = budget.heartbeat(args.run)
        elif cmd == "finish": result = budget.finish(args.run, args.reason, args.evidence)
        elif cmd == "status": result = budget.status(args.day)
        elif cmd == "export": result = budget.export_ledger()
        elif cmd == "resolve": result = budget.resolve(args.key, args.outcome, args.evidence)
        elif cmd == "import": result = budget.import_record(args.key, args.outcome, args.at, args.evidence)
        elif cmd == "follow-up": result = budget.follow_up(args.key, args.evidence)
        elif cmd == "pause": result = budget.pause(args.evidence)
        elif cmd == "resume-risk": result = budget.resume_risk(args.evidence)
        else: result = budget.clear_legacy_run(args.evidence)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except Refused as error:
        print(json.dumps({"ok": False, "code": error.code}))
        return 2
    except (ValueError, OSError, sqlite3.Error) as error:
        print(json.dumps({"ok": False, "code": "INVALID_OR_UNAVAILABLE", "error": str(error)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
