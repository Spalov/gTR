"""Resolve reply IDs in the exact source chat or explicitly identified peer."""


def resolve(db, peer_chats):
    db.execute("""UPDATE messages AS m SET target=(SELECT t.id FROM messages t
        WHERE t.chat=m.chat AND t.source_id=m.reply AND t.source_id<>'' AND t.conflict=0)
        WHERE m.reply<>'' AND m.reply_peer=''""")
    foreign = db.execute(
        "SELECT DISTINCT source,reply_peer FROM messages WHERE reply_peer<>'' AND reply<>''"
    ).fetchall()
    for source_name, peer in foreign:
        matches = peer_chats.get((source_name, peer), [])
        if len(matches) == 1:
            db.execute(
                """UPDATE messages AS m SET target=(SELECT t.id FROM messages t
                WHERE t.chat=? AND t.source_id=m.reply AND t.source_id<>'' AND t.conflict=0)
                WHERE m.source=? AND m.reply_peer=? AND m.reply<>''""",
                (matches[0], source_name, peer),
            )
    db.execute("""INSERT INTO issues SELECT chat,id,'warning','unresolved_reply' FROM messages
                  WHERE reply<>'' AND target IS NULL""")
