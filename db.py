def add_job(chat_id, info: dict, max_conn: int):
    doc = {
        "chat_id": chat_id,
        "target": info.get("display", info["connect_ip"]),
        "host": info.get("host", info["connect_ip"]),
        "port": info["port"],
        "path": info.get("path", "/"),
        "ssl": bool(info.get("ssl")),
        "max_conn": max_conn,
        "status": "running",
        "started_at": now(),
        "last_active": now(),
    }
    jobs.update_one({"chat_id": chat_id}, {"$set": doc}, upsert=True)
    return doc
