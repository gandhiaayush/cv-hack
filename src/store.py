"""SQLite run log: every message in the negotiation is a row (dashboard + replay read this)."""
import json
import sqlite3
import time

DB = "runs/run.db"


def _conn():
    import os
    os.makedirs("runs", exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute(
        "CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, ts REAL, run TEXT, agent TEXT,"
        " parent TEXT, kind TEXT, body TEXT)"
    )
    return c


def log(run, agent, parent, kind, body):
    c = _conn()
    c.execute(
        "INSERT INTO messages(ts,run,agent,parent,kind,body) VALUES(?,?,?,?,?,?)",
        (time.time(), run, agent, parent, kind, json.dumps(body, default=str)),
    )
    c.commit()
    c.close()
