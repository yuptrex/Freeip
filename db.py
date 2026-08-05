import os
from datetime import datetime, timezone

from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGO_DB", "slowbot")

_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
_db = _client[DB_NAME]
jobs = _db.jobs
jobs.create_index("chat_id", unique=True)


def now():
    return datetime.now(timezone.utc)


def add_job(chat_id, target, port, max_conn):
    doc = {
        "chat_id": chat_id,
        "target": target,
        "port": port,
        "max_conn": max_conn,
        "status": "running",
        "started_at": now(),
        "last_active": now(),
    }
    jobs.update_one({"chat_id": chat_id}, {"$set": doc}, upsert=True)
    return doc


def get_job(chat_id):
    return jobs.find_one({"chat_id": chat_id})


def mark_stopped(chat_id):
    jobs.update_one(
        {"chat_id": chat_id},
        {"$set": {"status": "stopped", "stopped_at": now()}},
    )


def heartbeat(chat_id, conns):
    jobs.update_one(
        {"chat_id": chat_id},
        {"$set": {"last_active": now(), "connections": conns}},
    )


def active_jobs():
    return list(jobs.find({"status": "running"}).sort("started_at", -1))


def delete_stopped():
    return jobs.delete_many({"status": "stopped"}).deleted_count


def reset_stale():
    jobs.update_many(
        {"status": "running"},
        {"$set": {"status": "stopped", "stopped_at": now()}},
    )
