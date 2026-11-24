"""Source identity evidence. Display labels never become Telegram account IDs."""


def html_author(label, joined, previous):
    if label is not None:
        return label, "html_label" if label else "unknown"
    if joined and previous:
        return previous, "html_joined"
    return "", "unknown"


def json_author(row, labels):
    name = row.get("from") or row.get("actor") or ""
    source_id = str(row.get("from_id") or row.get("actor_id") or "")
    if name:
        basis = "json_id" if source_id else "json_label"
    elif source_id and len(labels.get(source_id, ())) == 1:
        name = next(iter(labels[source_id]))
        basis = "json_id_matched"
    else:
        basis = "unknown"
    return name, source_id or name, {"source_author_id": source_id, "identity_basis": basis}


def participants(db, chat_id):
    return [
        dict(row)
        for row in db.execute(
            """SELECT sender_key,sender,
        CASE WHEN substr(c.source_key,1,5)='json:' AND m.sender_key<>m.sender THEN m.sender_key ELSE '' END AS source_id,
        count(*) AS messages,
        SUM(CASE WHEN json_extract(meta,'$.identity_basis')='html_joined'
                    OR json_extract(meta,'$.joined')=1 THEN 1 ELSE 0 END) AS joined
        FROM messages m JOIN chats c ON c.id=m.chat WHERE m.chat=? AND m.service=0
        GROUP BY sender_key,sender ORDER BY messages DESC, min(m.id)""",
            (chat_id,),
        )
    ]
