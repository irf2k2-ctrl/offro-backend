"""
Real-MongoDB concurrency verification for the Phase 2E/2F worker (STAGING ONLY, no WhatsApp, no Meta, no app data touched).

mongomock cannot prove cross-process atomicity, so this script exercises the SAME worker functions against a real MongoDB server
from several OS processes at once, in a throw-away database whose name starts with "wa_conc_check_". It drops only that database.

Run it from the backend folder with the Staging environment (so imports work), pointing at the STAGING MongoDB:

    export CONC_MONGO_URL='<staging mongodb connection string>'      # read from the environment, never printed
    python tools/wa_staging_concurrency_check.py --yes-this-is-staging [--processes 8] [--recipients 2000]

Exit code 0 = every check passed; a crashed or hung child process is reported as a FAILED check (no hang). Output never contains connection strings, phone numbers or tokens.
Checks: (1) recipient claims: every recipient claimed exactly once across N processes; (2) campaign lease: exactly one holder at a
time; (3) rate limiter: tokens per second never exceed the limit across processes; (4) delivery status: concurrent duplicate and
out-of-order events leave the newest status; (5) expired-lease recovery: requeued vs uncertain split is exact.
"""
import argparse
import multiprocessing as mp
import os
import sys
import time
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RATE_PER_SECOND = 5
RATE_WINDOW_SECONDS = 3        # the measured window: exactly this long, starting at the common start instant
RATE_START_DELAY = 2           # seconds from now until every process starts measuring together
RUN_TIMEOUT = 120              # a child that neither answers nor exits within this is terminated and reported


def rate_upper_bound(rate, window):
    """A window of `window` seconds can overlap window+1 one-second buckets, each allowing `rate` tokens."""
    return rate * (int(window) + 1)


def rate_lower_bound(rate, window):
    """It always contains at least window-1 COMPLETE buckets; fewer tokens than that means tokens are not flowing at all."""
    return rate * (int(window) - 1)


def _setup(db_name):
    from pymongo import MongoClient
    url = os.environ.get("CONC_MONGO_URL", "")
    if not url:
        raise SystemExit("CONC_MONGO_URL is not set")
    assert db_name.startswith("wa_conc_check_")
    db = MongoClient(url, serverSelectionTimeoutMS=8000)[db_name]
    import services.wa_campaign_manager as M
    M.db = db
    import services.wa_campaign_worker as W
    return db, M, W


def _claim_proc(db_name, cid, out_q):
    db, M, W = _setup(db_name)
    cfg, mine = W.config(), []
    while True:
        r = W.claim_recipient(cid, "p%d" % os.getpid(), cfg, datetime.utcnow())
        if not r:
            break
        mine.append(str(r["_id"]))
    out_q.put(mine)


def _lease_proc(db_name, rounds, out_q):
    db, M, W = _setup(db_name)
    cfg, wins = W.config(), 0
    for _ in range(rounds):
        if W._claim_campaign("p%d" % os.getpid(), cfg, datetime.utcnow()):
            wins += 1
    out_q.put(wins)


def _rate_proc(db_name, start_at, out_q):
    db, M, W = _setup(db_name)
    cfg = {"rate_per_second": RATE_PER_SECOND}
    while time.time() < start_at:                       # wait for the common start: tokens taken earlier are NOT counted
        time.sleep(0.001)
    t_end = start_at + RATE_WINDOW_SECONDS
    n = 0
    while time.time() < t_end:
        if W.take_rate_token(cfg):
            n += 1
    out_q.put(n)


def _status_proc(db_name, events, out_q):
    db, M, W = _setup(db_name)
    import services.wa_delivery as D
    for status, ts in events:
        D.handle_status(msg_id="wamid.CONC", recipient="919000000001", status=status, timestamp=str(ts), raw={})
    out_q.put(1)


class _Sender:
    """What a child receives instead of a Queue: `put(x)` sends exactly one result down ITS OWN one-way pipe."""
    def __init__(self, conn):
        self.conn = conn

    def put(self, value):
        self.conn.send(value)
        self.conn.close()


def _run(procs, target, args, timeout=RUN_TIMEOUT):
    """Run `procs` children, one result from each. One one-way Pipe per child ties every result to its child, so a child that exits
    WITHOUT a result is detected reliably as EOF on its own pipe (no Queue.empty(), no feeder-thread races: data sent before the child
    exits stays readable). Never hangs: any child still silent after `timeout` seconds ends the run, and every child is terminated.
    Raises RuntimeError on a crashed child, a child that exits without a result, a non-zero exit, or a timeout. -> results in child order."""
    from multiprocessing.connection import wait
    ps, pending = [], {}
    results, problem = [None] * procs, ""
    try:
        for i in range(procs):
            recv, send = mp.Pipe(duplex=False)
            p = mp.Process(target=target, args=args + (_Sender(send),))
            p.start()
            send.close()               # parent drops ITS copy of the send end (and children started later never inherit it): EOF is reliable
            ps.append(p)
            pending[recv] = i
        deadline = time.time() + timeout
        while pending and not problem:
            left = deadline - time.time()
            if left <= 0:
                problem = "timed out after %ss waiting for children" % timeout
                break
            for conn in wait(list(pending), timeout=min(0.5, left)):
                i = pending.pop(conn)
                try:
                    results[i] = conn.recv()
                except EOFError:       # the child closed its end without ever sending a result
                    ps[i].join(timeout=5)
                    problem = "child %d exited (code %s) without a result" % (i, ps[i].exitcode)
                finally:
                    conn.close()
    finally:
        for conn in pending:
            conn.close()
        for p in ps:
            p.join(timeout=0.5 if problem else 5)
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)
    if not problem:
        bad = [p.exitcode for p in ps if p.exitcode != 0]
        if bad:
            problem = "child exited with code %s" % bad[0]
    if problem:
        raise RuntimeError(problem)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes-this-is-staging", action="store_true", required=True)
    ap.add_argument("--processes", type=int, default=8)
    ap.add_argument("--recipients", type=int, default=2000)
    a = ap.parse_args()
    name = "wa_conc_check_" + uuid.uuid4().hex[:8]
    db, M, W = _setup(name)
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("%-62s %s %s" % (label, "PASS" if cond else "FAIL", detail))

    def step(label, fn):
        """A runner failure (crashed / hung child) is a FAILED check with a reason, never a hang or a traceback."""
        try:
            fn()
        except RuntimeError as e:
            check(label, False, "(%s)" % e)

    def c1():
        W.ensure_indexes()
        cid, now = "conc-campaign", datetime.utcnow()
        db[M.RECIPIENTS].insert_many([{"campaign_id": cid, "phone": "9190%07d" % i, "state": "queued", "attempts": 0,
                                       "next_attempt_at": now, "send_started": False} for i in range(a.recipients)])
        claimed = [x for part in _run(a.processes, _claim_proc, (name, cid)) for x in part]
        check("1. recipient claims: each exactly once", len(claimed) == a.recipients and len(set(claimed)) == a.recipients,
              "(%d claims / %d recipients / %d processes)" % (len(claimed), a.recipients, a.processes))

    def c2():
        db[M.CAMPAIGNS].insert_one({"status": "running", "run": {"lease_until": W.EPOCH, "backoff_until": W.EPOCH}})
        wins = _run(a.processes, _lease_proc, (name, 20))
        check("2. campaign lease: one holder while the lease is live", sum(wins) == 1, "(wins=%s)" % sum(wins))

    def c3():
        start_at = time.time() + RATE_START_DELAY
        sent = sum(_run(a.processes, _rate_proc, (name, start_at), timeout=RUN_TIMEOUT))
        lo, hi = rate_lower_bound(RATE_PER_SECOND, RATE_WINDOW_SECONDS), rate_upper_bound(RATE_PER_SECOND, RATE_WINDOW_SECONDS)
        check("3. rate limiter: <= %d/s cluster-wide (%d s window)" % (RATE_PER_SECOND, RATE_WINDOW_SECONDS), lo <= sent <= hi,
              "(sent=%d, expected %d..%d)" % (sent, lo, hi))

    def c4():
        db[M.RECIPIENTS].insert_one({"campaign_id": "d", "phone": "919000000001", "state": "sent", "message_id": "wamid.CONC", "delivery_rank": 2})
        ev = [("sent", 100), ("delivered", 200), ("read", 300), ("delivered", 200), ("sent", 100), ("read", 300)]
        _run(a.processes, _status_proc, (name, ev))
        final = db[M.RECIPIENTS].find_one({"message_id": "wamid.CONC"})
        check("4. delivery status: concurrent/out-of-order -> read", final["state"] == "read" and final["delivery_rank"] == 4)

    def c5():
        past = datetime.utcnow() - timedelta(seconds=5)
        db[M.RECIPIENTS].insert_many([{"campaign_id": "r", "state": "sending", "lease_until": past, "send_started": i % 2 == 0,
                                       "attempts": 1} for i in range(10)])
        res = W.recover_abandoned()
        check("5. recovery: send_started -> uncertain, else requeued", res == {"requeued": 5, "uncertain": 5}, str(res))

    try:
        for label, fn in (("1", c1), ("2", c2), ("3", c3), ("4", c4), ("5", c5)):
            step("check " + label, fn)
    finally:
        db.client.drop_database(name)
    print("RESULT:", "ALL PASS" if ok else "FAILURES")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
