"""Durable, offline record decisions scoped to a Telegram conversation."""

import hashlib
import json
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path

from . import __version__
from .core import encode
from .files import local, new_directory
from .model import Cancelled, RecoveryError, quiet
from .selection import count, intersection, merge

STATUS_NAMES = {
    "NEW": "Новая запись",
    "READY_FOR_RETRY": "Можно подготовить заново",
    "FAILED_BEFORE_START": "Ошибка до запуска",
    "POSSIBLY_IMPORTED": "Запуск принят, требуется сверка",
    "CONFIRMED_IMPORTED": "Восстановление подтверждено",
    "CONFLICT": "Неоднозначное совпадение",
    "UNKNOWN_SERVER_STATE": "Состояние операции неизвестно",
}
ELIGIBLE = {"NEW", "READY_FOR_RETRY", "FAILED_BEFORE_START"}
VERIFY_PREVIOUS_OPERATIONS = "verify_previous_operations"
SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_version(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS import_packages(
 sha TEXT PRIMARY KEY, manifest TEXT NOT NULL, project TEXT NOT NULL DEFAULT '', app_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS registry_records(
 operation TEXT NOT NULL, ordinal INTEGER NOT NULL, origin TEXT NOT NULL, title TEXT NOT NULL,
 source_id TEXT NOT NULL, signature TEXT NOT NULL, author TEXT NOT NULL, author_id TEXT NOT NULL,
 moment TEXT NOT NULL, zone TEXT NOT NULL, text_digest TEXT NOT NULL, media TEXT NOT NULL,
 occurrence INTEGER NOT NULL, repetitions INTEGER NOT NULL, preceding TEXT NOT NULL, following TEXT NOT NULL,
 PRIMARY KEY(operation,ordinal));
CREATE INDEX IF NOT EXISTS registry_by_signature ON registry_records(signature);
CREATE TABLE IF NOT EXISTS source_anchors(scope TEXT NOT NULL,source_id TEXT NOT NULL,signature TEXT NOT NULL,
 title TEXT NOT NULL,PRIMARY KEY(scope,source_id));
CREATE TABLE IF NOT EXISTS alpha_links(peer TEXT NOT NULL,fingerprint TEXT NOT NULL,operation TEXT NOT NULL,
 PRIMARY KEY(peer,fingerprint));
CREATE TABLE IF NOT EXISTS registry_checks(operation TEXT PRIMARY KEY, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS record_verifications(operation TEXT NOT NULL,ordinal INTEGER NOT NULL,checked_at TEXT NOT NULL,
 PRIMARY KEY(operation,ordinal));
CREATE TABLE IF NOT EXISTS package_sources(sha TEXT PRIMARY KEY,archive TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS import_profiles(
 account INTEGER NOT NULL, peer TEXT NOT NULL, target_id INTEGER NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
 project TEXT NOT NULL, archive TEXT NOT NULL, mode TEXT NOT NULL, settings TEXT NOT NULL, updated TEXT NOT NULL,
 PRIMARY KEY(account,peer));
CREATE VIEW IF NOT EXISTS import_record_status AS SELECT r.*,v.checked_at AS verified_at,c.checked_at AS last_check,
 o.account,o.peer,o.server_id,o.start_request_sent,o.start_request_accepted,o.manifest_sha256,o.app_version,
 CASE WHEN (o.outcome='accepted' AND o.state='COMPLETED') OR v.ordinal IS NOT NULL OR
  EXISTS (SELECT 1 FROM json_each(e.confirmed) j WHERE r.ordinal BETWEEN json_extract(j.value,'$[0]')
  AND json_extract(j.value,'$[1]')) THEN 'CONFIRMED_IMPORTED'
 WHEN o.outcome='accepted' THEN 'POSSIBLY_IMPORTED' WHEN o.outcome IN ('staged','uncertain') THEN 'UNKNOWN_SERVER_STATE'
 WHEN o.outcome='staged_closed' THEN 'READY_FOR_RETRY' WHEN o.state IN ('FAILED','FAILED_BEFORE_START','START_REJECTED','UNSUPPORTED')
 THEN 'FAILED_BEFORE_START' ELSE 'NEW' END AS status
 FROM registry_records r JOIN operations o ON o.id=r.operation LEFT JOIN operation_record_evidence e ON e.operation=o.id
 LEFT JOIN registry_checks c ON c.operation=o.id LEFT JOIN record_verifications v ON v.operation=o.id AND v.ordinal=r.ordinal;
"""


def migrate(db):
    for sql in SCHEMA.split(";"):
        if sql.strip():
            db.execute(sql)
    stored = db.execute("SELECT version FROM registry_version").fetchall()
    if stored and [row[0] for row in stored] != [SCHEMA_VERSION]:
        raise RecoveryError("Версия реестра импорта новее приложения.")
    db.execute("INSERT OR IGNORE INTO registry_version VALUES (?)", (SCHEMA_VERSION,))
    # Backfill numeric profiles without inventing display names, verification or record IDs.
    for account, peer in db.execute(
        """SELECT DISTINCT account,peer FROM operations WHERE mode='owner' AND account IS NOT NULL
           EXCEPT SELECT account,peer FROM import_profiles"""
    ):
        try:
            kind, raw = peer.split(":", 1)
            if kind == "pm":
                pair = [int(x) for x in raw.split("/")]
                if len(pair) != 2 or account not in pair:
                    continue
                target = pair[1] if pair[0] == account else pair[0]
            elif kind == "group":
                target = int(raw)
            else:
                continue
        except (ValueError, TypeError):
            continue
        db.execute(
            "INSERT INTO import_profiles VALUES (?,?,?,?,?,'','','text_media','{}','')",
            (account, peer, target, kind, f"Диалог {target}"),
        )


def _hash(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def identities(data):
    """Describe each occurrence in the full source, before applying the package selection."""
    items = []
    origin = data.get("source_conversation", "")
    title = data.get("chat_title", "").strip().casefold()
    if title == "диалог" or data.get("source_title_unique") is False:
        title = ""
    for record in data.get("records", ()):
        pos = record.get("source_ordinal")
        if type(pos) is not int:
            continue
        evidence = record.get("source_identity", {})
        moment = evidence.get("original_utc") or record.get("original_date", "")
        files = record.get("original_media", [])
        media = [(f.get("kind"), f.get("sha256") or f.get("original")) for f in files]
        author, author_id = record.get("author", ""), record.get("source_author_id", "")
        digest = record.get("original_text_sha256", record.get("text_sha256", ""))
        sig = _hash([author_id or author, moment, digest, media, record.get("reply_source_id", "")])
        items.append(
            {
                "ordinal": pos,
                "origin": origin,
                "title": title,
                "source_id": record.get("source_id") or "",
                "signature": sig,
                "author": author,
                "author_id": author_id,
                "moment": moment,
                "zone": evidence.get("original_timezone", ""),
                "text_digest": digest,
                "media": encode(files),
                "included": record.get("included", False),
            }
        )
    totals = Counter(r["signature"] for r in items)
    seen = Counter()
    # Neighbouring trustworthy message IDs delimit unnumbered runs across changed export ranges.
    following = ""
    for row in reversed(items):
        row["following"] = following
        if row["source_id"]:
            following = row["source_id"]
    preceding = ""
    for row in items:
        sig = row["signature"]
        seen[sig] += 1
        row.update(occurrence=seen[sig], repetitions=totals[sig], preceding=preceding)
        if row["source_id"]:
            preceding = row["source_id"]
    return items


def register(db, job, bundle, *, ranges=None, exact=True):
    if exact:
        db.execute(
            "INSERT OR IGNORE INTO import_packages(sha,manifest,app_version) VALUES (?,?,?)",
            (bundle.manifest_hash(), encode(bundle.data), __version__),
        )
    starts = [a for a, _ in ranges or ()]

    def selected(row):
        if ranges is None:
            return row["included"]
        i = bisect_right(starts, row["ordinal"]) - 1
        return i >= 0 and row["ordinal"] <= ranges[i][1]

    rows = identities(bundle.data)
    scope = bundle.scope()
    if not db.execute("SELECT 1 FROM source_anchors WHERE scope=? LIMIT 1", (scope,)).fetchone():
        db.executemany(
            "INSERT OR IGNORE INTO source_anchors VALUES (?,?,?,?)",
            ((scope, r["source_id"], r["signature"], r["title"]) for r in rows if r["source_id"]),
        )
    db.executemany(
        """INSERT OR IGNORE INTO registry_records VALUES
        (:operation,:ordinal,:origin,:title,:source_id,:signature,:author,:author_id,:moment,:zone,
         :text_digest,:media,:occurrence,:repetitions,:preceding,:following)""",
        (dict(row, operation=job) for row in rows if selected(row)),
    )


def remember_profile(journal, account, target, project, archive, mode, settings):
    from .journal import timestamp

    with journal.connect(lock=True) as db:
        db.execute(
            """INSERT INTO import_profiles VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account,peer) DO UPDATE SET title=excluded.title,
            project=coalesce(nullif(excluded.project,''),import_profiles.project),
            archive=coalesce(nullif(excluded.archive,''),import_profiles.archive),
            mode=excluded.mode,settings=json_patch(import_profiles.settings,excluded.settings),updated=excluded.updated""",
            (
                account.id,
                journal.endpoint.key,
                target.id,
                target.kind,
                f"{account.name} ↔ {target.title}",
                str(project or ""),
                str(archive or ""),
                mode,
                encode(settings),
                timestamp(),
            ),
        )


def save_verification_preference(journal, profile, enabled):
    """Save this preference without replacing the other profile settings."""
    from .journal import timestamp

    with journal.connect(lock=True) as db:
        db.execute(
            """INSERT INTO import_profiles VALUES
               (:account,:peer,:target_id,:kind,:title,:project,:archive,:mode,:preference,:updated)
               ON CONFLICT(account,peer) DO UPDATE SET
               settings=json_patch(import_profiles.settings,excluded.settings),updated=excluded.updated""",
            {**profile, "preference": encode({VERIFY_PREVIOUS_OPERATIONS: enabled}), "updated": timestamp()},
        )


def _status(row, confirmed):
    if row["outcome"] == "accepted" and row["state"] == "COMPLETED" or confirmed:
        return "CONFIRMED_IMPORTED"
    if row["outcome"] == "accepted":
        return "POSSIBLY_IMPORTED"
    if row["outcome"] in {"uncertain", "staged"} or row["state"] == "STARTING":
        return "UNKNOWN_SERVER_STATE"
    if row["outcome"] == "staged_closed":
        return "READY_FOR_RETRY"
    if row["state"] in {"FAILED", "FAILED_BEFORE_START", "START_REJECTED", "UNSUPPORTED"}:
        return "FAILED_BEFORE_START"
    return None


def unmatched_alpha(db, endpoint):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='starts'").fetchone():
        return []
    has_plans = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plans'").fetchone()
    result = []
    for peer in {endpoint.key, *endpoint.aliases}:
        for raw in db.execute(
            """SELECT s.* FROM starts s WHERE peer=? AND NOT EXISTS
                 (SELECT 1 FROM alpha_links a WHERE a.peer=s.peer AND a.fingerprint=s.fingerprint)""",
            (peer,),
        ):
            scope = (
                db.execute(
                    "SELECT scope FROM plans WHERE peer=? AND fingerprint=?", (peer, raw["fingerprint"])
                ).fetchone()
                if has_plans
                else None
            )
            result.append({**dict(raw), "scope": scope[0] if scope else None})
    return result


def link_alpha(journal, checked, old):
    """Alpha 0.2 saved the original scope and transcript/media hashes, but no timestamps."""
    from .journal import Phase, State

    accepted = old.get("state") == "accepted"
    selection = checked.record_intervals()
    job = _hash(["alpha", old["peer"], old["fingerprint"]])
    with journal.connect(lock=True) as db:
        if db.execute(
            "SELECT 1 FROM alpha_links WHERE peer=? AND fingerprint=?", (old["peer"], old["fingerprint"])
        ).fetchone():
            return
        db.execute(
            """INSERT INTO operations(id,created,peer,mode,fingerprint,scope,manifest_sha256,state,stage,
                   outcome,messages,media,skipped,duplicates,content_fingerprint,phase,selection,start_request_sent,
                   start_request_accepted) VALUES (?,'',?,'owner',?,?,?,?,'start_import',?,?,?,?,0,?,?,?,?,?)""",
            (
                job,
                journal.endpoint.key,
                checked.fingerprint(),
                checked.scope(),
                checked.manifest_hash(),
                State.VERIFYING if accepted else State.START_UNCERTAIN,
                "accepted" if accepted else "uncertain",
                checked.data["included"],
                len(checked.data["media"]),
                checked.data["skipped"],
                checked.content_fingerprint(),
                Phase.VERIFYING if accepted else Phase.UNKNOWN_SERVER_STATE,
                encode(selection) if selection is not None else None,
                True if accepted else None,
                accepted,
            ),
        )
        register(db, job, checked)
        db.execute("INSERT INTO alpha_links VALUES (?,?,?)", (old["peer"], old["fingerprint"], job))
    retain(journal, checked)


def classify(journal, db, bundle, *, exclude=""):
    from .journal import Phase, RecoveryConflict, RecoveryReview, _record_ranges

    setting = db.execute(
        "SELECT json_type(settings, ?) FROM import_profiles WHERE account=? AND peer=?",
        ("$." + VERIFY_PREVIOUS_OPERATIONS, journal.endpoint.account_id, journal.endpoint.key),
    ).fetchone()
    verify_previous = setting is None or setting[0] != "false"
    selection = bundle.record_intervals()
    previous, locked = journal._review(
        db,
        bundle.fingerprint(),
        bundle.scope(),
        bundle.content_fingerprint(),
        selection,
        bundle.data["included"],
        indexed=True,
        verify_previous=verify_previous,
    )
    if selection is None:
        if not previous.conflicts:
            prior = db.execute(
                """SELECT o.id,o.phase FROM operations o WHERE o.peer=? AND
                   o.outcome IN ('accepted','uncertain','staged') AND NOT EXISTS (
                   SELECT 1 FROM test_reset_exclusions x WHERE x.reset_id=? AND x.operation=o.id)""",
                (journal.endpoint.key, journal.test_reset_id),
            ).fetchall()
            conflicts = [RecoveryConflict(row["id"], row["phase"], None) for row in prior]
            if not journal.test_reset_id and unmatched_alpha(db, journal.endpoint):
                conflicts.append(RecoveryConflict("", Phase.UNKNOWN_SERVER_STATE, None))
            if conflicts:
                total = bundle.data["included"]
                return RecoveryReview(tuple(conflicts), total, None, 0, 0, total, (), (), ()), locked
        return previous, locked
    entire_source = identities(bundle.data)
    anchor_keys = {
        (r["source_id"], r["signature"], r["title"]) for r in entire_source if r["source_id"] and r["title"]
    }
    shared_anchors = {}
    current = [row for row in entire_source if row["included"]]
    by_id, by_signature, missing_ids = defaultdict(list), defaultdict(list), defaultdict(list)
    by_author_time = defaultdict(list)
    missing_by_time = defaultdict(list)
    decisions = {r["ordinal"]: "NEW" for r in current}
    for row in current:
        by_id[row["source_id"]].append(row)
        by_signature[row["signature"]].append(row)
        by_author_time[row["author_id"] or row["author"], row["moment"]].append(row)
        if not row["source_id"]:
            missing_ids[row["signature"]].append(row)
            missing_by_time[row["author_id"] or row["author"], row["moment"]].append(row)

    conflicts = {c.operation_id: c for c in previous.conflicts}
    missing_coverage = previous.overlap is None
    overlaps = set()

    def mark(pos, state):
        # Positive proof is retained even when another writer has locked the destination.
        priority = (
            "NEW",
            "FAILED_BEFORE_START",
            "READY_FOR_RETRY",
            "POSSIBLY_IMPORTED",
            "CONFLICT",
            "UNKNOWN_SERVER_STATE",
            "CONFIRMED_IMPORTED",
        )
        if pos in decisions and priority.index(state) > priority.index(decisions[pos]):
            decisions[pos] = state

    for ranges, state in (
        (
            previous.unknown_records,
            "UNKNOWN_SERVER_STATE" if locked or missing_coverage else "POSSIBLY_IMPORTED",
        ),
        (previous.confirmed_records, "CONFIRMED_IMPORTED"),
    ):
        for a, b in ranges:
            for pos in range(a, b + 1):
                mark(pos, state)
    for old in db.execute(
        """SELECT o.*,e.confirmed,e.selection AS linked_selection FROM operations o LEFT JOIN operation_record_evidence e
            ON e.operation=o.id WHERE o.peer=? AND o.id<>? AND NOT EXISTS (
            SELECT 1 FROM test_reset_exclusions x WHERE x.reset_id=? AND x.operation=o.id)
            AND (o.outcome<>'' OR o.state IN
            ('FAILED','FAILED_BEFORE_START','START_REJECTED','UNSUPPORTED','COMPLETED'))""",
        (journal.endpoint.key, exclude, journal.test_reset_id),
    ):
        if old["error_code"] == "REPLAY_BLOCKED":
            continue
        unknown_dispatch = old["outcome"] == "uncertain"
        protect_ambiguous_id = unknown_dispatch and not verify_previous
        rows = db.execute("SELECT * FROM registry_records WHERE operation=?", (old["id"],)).fetchall()
        complete_index = bool(rows) and len(rows) == old["messages"]
        proven = _record_ranges(old["confirmed"]) or ()
        proven_starts = [a for a, _ in proven]
        same_scope = old["scope"] == bundle.scope()
        if same_scope and unknown_dispatch and not complete_index:
            known = _record_ranges(old["linked_selection"] or old["selection"], old["messages"])
            for first, last in intersection(selection, known or ()):
                for pos in range(first, last + 1):
                    mark(pos, "UNKNOWN_SERVER_STATE")
        if not same_scope and old["scope"] not in shared_anchors:
            anchors = db.execute(
                "SELECT source_id,signature,title FROM source_anchors WHERE scope=?", (old["scope"],)
            )
            shared_anchors[old["scope"]] = (
                len({r["signature"] for r in anchors if tuple(r) in anchor_keys}) >= 2
            )
        corroborated = shared_anchors.get(old["scope"], False)
        matches = set()
        processed_runs = set()
        for prior in rows:
            pos = prior["ordinal"]
            p = bisect_right(proven_starts, pos) - 1
            state = _status(old, p >= 0 and pos <= proven[p][1])
            if not state:
                continue
            if same_scope:
                if pos in decisions:
                    mark(pos, state)
                    matches.add(pos)
                continue
            candidates = by_id.get(prior["source_id"], ()) if prior["source_id"] else ()
            alternatives = (missing_ids if prior["source_id"] else by_signature).get(prior["signature"], ())
            # Distinct numeric IDs in one source are independent even when payloads are identical.
            if (
                not prior["source_id"]
                and len(alternatives) > 1
                and (
                    not prior["preceding"]
                    or not prior["following"]
                    or prior["repetitions"] != alternatives[0]["repetitions"]
                )
            ):
                run_key = (prior["signature"], state, prior["origin"], prior["title"])
                if run_key in processed_runs:
                    continue
                processed_runs.add(run_key)
            candidates = [*candidates, *alternatives]
            uncertain_key = (prior["origin"], prior["title"], prior["signature"])
            if state not in ELIGIBLE and uncertain_key not in processed_runs:
                processed_runs.add(uncertain_key)
                same_payload = by_signature.get(prior["signature"], ())
                if prior["source_id"] and not corroborated:
                    for item in same_payload:
                        if (
                            item["source_id"]
                            and item["source_id"] != prior["source_id"]
                            and not (item["origin"] and prior["origin"])
                        ):
                            mark(item["ordinal"], "CONFLICT")
                            matches.add(item["ordinal"])
                if not same_payload:
                    times = missing_by_time if prior["source_id"] else by_author_time
                    for item in times.get((prior["author_id"] or prior["author"], prior["moment"]), ()):
                        if (not prior["source_id"] or not item["source_id"]) and not (
                            prior["origin"] and item["origin"] and prior["origin"] != item["origin"]
                        ):
                            mark(item["ordinal"], "CONFLICT")
                            matches.add(item["ordinal"])
            for item in candidates:
                a, b = prior["origin"], item["origin"]
                if a and b and a != b:
                    continue
                same_source = bool(
                    a and a == b or prior["title"] and prior["title"] == item["title"] and corroborated
                )
                same_content = item["signature"] == prior["signature"]
                # A matching ID is still plausible after a rename or a switch of export format.
                if (
                    not same_source
                    and not same_content
                    and item["title"] != prior["title"]
                    and not protect_ambiguous_id
                ):
                    continue
                trustworthy_id = bool(prior["source_id"] and item["source_id"] == prior["source_id"])
                anchored = bool(
                    item["preceding"]
                    and item["following"]
                    and all(
                        item[k] == prior[k] for k in ("preceding", "following", "occurrence", "repetitions")
                    )
                )
                if (
                    not trustworthy_id
                    and same_source
                    and not anchored
                    and (
                        item["repetitions"] == prior["repetitions"]
                        and item["preceding"] == prior["preceding"]
                        and item["following"] == prior["following"]
                        and item["preceding"]
                        and item["following"]
                        and item["occurrence"] != prior["occurrence"]
                    )
                ):
                    continue
                if state in ELIGIBLE:
                    if same_content:
                        mark(item["ordinal"], state)
                    continue
                confident = same_source and same_content and (trustworthy_id or anchored)
                mark(item["ordinal"], state if confident else "CONFLICT")
                matches.add(item["ordinal"])
        if not same_scope and not complete_index and old["outcome"] in {"accepted", "uncertain", "staged"}:
            # Incomplete coverage in an older export cannot establish which records are new.
            for pos in decisions:
                mark(pos, "UNKNOWN_SERVER_STATE")
            conflicts.setdefault(old["id"], RecoveryConflict(old["id"], old["phase"], None, True))
            missing_coverage = True
        if matches and _status(old, False) not in ELIGIBLE:
            overlaps.update(matches)
            conflicts.setdefault(
                old["id"], RecoveryConflict(old["id"], old["phase"] or Phase.VERIFYING, len(matches))
            )
    if not journal.test_reset_id and unmatched_alpha(db, journal.endpoint):
        for pos in decisions:
            mark(pos, "UNKNOWN_SERVER_STATE")
        conflicts[""] = RecoveryConflict("", Phase.UNKNOWN_SERVER_STATE, None)
        missing_coverage = True

    def ranges(statuses):
        return merge((pos, pos) for pos, state in decisions.items() if state in statuses)

    eligible = ranges(ELIGIBLE)
    confirmed = ranges({"CONFIRMED_IMPORTED"})
    unknown = ranges(set(STATUS_NAMES) - ELIGIBLE - {"CONFIRMED_IMPORTED"})
    overlap = None if missing_coverage else max(previous.overlap or 0, len(overlaps))
    return RecoveryReview(
        tuple(conflicts.values()),
        len(current),
        overlap,
        count(confirmed),
        count(eligible),
        count(unknown),
        eligible,
        confirmed,
        unknown,
        decisions,
    ), locked


def recovery_root(journal, sha):
    import re

    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise RecoveryError("Повреждён идентификатор сохранённого пакета.")
    return local(journal.path.parent / "recovery" / sha)


def retain(journal, bundle, project=None, progress=quiet):
    from .conversion import copy_package, load_bundle

    sha = bundle.manifest_hash()
    root = recovery_root(journal, sha)
    if not root.exists():
        try:
            with new_directory(root) as work:
                copy_package(bundle, work, progress)
                if load_bundle(work, progress).manifest_hash() != sha:
                    raise RecoveryError("Не удалось сверить сохранённую копию пакета.")
                from .reports import package_html

                (work / "report.html").write_text(package_html(bundle.data), encoding="utf-8")
        except (FileExistsError, RecoveryError, OSError):
            if not root.is_dir():
                raise
    saved = load_bundle(root, progress)
    if saved.manifest_hash() != sha:
        raise RecoveryError("Сохранённая копия пакета изменилась.")
    archive = ""
    if project:
        from .core import catalogue, properties

        with catalogue(project) as source:
            archive = properties(source)["original"]
    with journal.connect(lock=True) as db:
        db.execute(
            """INSERT INTO import_packages VALUES (?,?,?,?) ON CONFLICT(sha) DO UPDATE
                   SET project=coalesce(nullif(excluded.project,''),import_packages.project)""",
            (sha, encode(saved.data), str(project or ""), __version__),
        )
        if archive:
            db.execute("INSERT OR REPLACE INTO package_sources VALUES (?,?)", (sha, archive))
    return saved


def learn_legacy(journal, bundle, directories=(), progress=quiet):
    """Only bind intact packages bearing an old operation's exact, recorded manifest digest."""
    from .conversion import Bundle, load_bundle
    from .files import read_json
    from .journal import _record_ranges

    with journal.connect() as db:
        alpha = unmatched_alpha(db, journal.endpoint)
        old = db.execute(
            """SELECT o.*,coalesce(e.selection,o.selection) AS known FROM operations o
            LEFT JOIN operation_record_evidence e ON o.id=e.operation
            LEFT JOIN import_packages p ON p.sha=o.manifest_sha256
            WHERE o.peer=? AND o.outcome IN ('accepted','uncertain','staged') AND
            (p.sha IS NULL OR NOT EXISTS(SELECT 1 FROM registry_records r WHERE r.operation=o.id))""",
            (journal.endpoint.key,),
        ).fetchall()
    if not old and not alpha:
        return
    wanted = defaultdict(list)
    for row in old:
        wanted[row["manifest_sha256"]].append(row)
        ranges = _record_ranges(row["known"], row["messages"])
        if ranges and row["scope"] == bundle.scope():
            with journal.connect(lock=True) as db:
                register(db, row["id"], bundle, ranges=ranges, exact=False)
    candidates = [bundle.root]
    for folder in dict.fromkeys(directories):
        try:
            folder = local(Path(folder))
            if folder.is_dir():
                candidates.extend(
                    p for p in folder.iterdir() if p.name.startswith(("gTR-import-", "gTR-new-", "gTR-beta-"))
                )
        except (OSError, RecoveryError):
            continue
    for path in dict.fromkeys(candidates):
        if not wanted and not alpha:
            break
        try:
            if not local(path / "manifest.json").is_file():
                continue
            data = read_json(path / "manifest.json")
            candidate = Bundle(path, data)
            sha = candidate.manifest_hash()
            eligible_alpha = [
                r
                for r in alpha
                if r["scope"] == candidate.scope() and r["fingerprint"] == candidate.content_fingerprint()
            ]
            if sha not in wanted and not eligible_alpha:
                continue
            checked = load_bundle(path, progress)
        except Cancelled:
            raise
        except (KeyError, TypeError, ValueError, OSError, RecoveryError):
            continue
        for row in eligible_alpha:
            link_alpha(journal, checked, row)
            alpha.remove(row)
        for row in wanted.pop(sha, ()):
            journal.attach_selection(row["id"], checked)


def index_old_projects(journal, roots, progress):
    import sqlite3

    from .conversion import Bundle, plan_import
    from .core import catalogue, properties, verify
    from .journal import _record_ranges

    with journal.connect() as db:
        legacy = db.execute(
            """SELECT o.id,o.scope,o.messages,coalesce(e.selection,o.selection) AS chosen
            FROM operations o LEFT JOIN operation_record_evidence e ON o.id=e.operation WHERE o.peer=?
            AND o.outcome IN ('accepted','uncertain','staged') AND NOT EXISTS
            (SELECT 1 FROM registry_records r WHERE r.operation=o.id)""",
            (journal.endpoint.key,),
        ).fetchall()
    pending = defaultdict(list)
    for row in legacy:
        ranges = _record_ranges(row["chosen"], row["messages"])
        if ranges:
            pending[row["scope"]].append((row["id"], ranges))
    for root in dict.fromkeys(roots):
        if not pending or not root.is_dir():
            continue
        for folder in root.iterdir():
            if not pending:
                break
            try:
                if not local(folder / "history.sqlite").is_file():
                    continue
                with catalogue(folder) as db:
                    sha = properties(db)["sha256"]
                    chats = [r[0] for r in db.execute("SELECT id FROM chats")]
                for chat in chats:
                    scope = hashlib.sha256(f"{sha}:{chat}".encode()).hexdigest()
                    if scope not in pending or set(verify(folder, progress)) - {"ok"}:
                        continue
                    data = plan_import(folder, chat, "source", progress, media_mode="text_only")
                    with journal.connect(lock=True) as db:
                        for job, ranges in pending.pop(scope):
                            register(db, job, Bundle(folder, data), ranges=ranges, exact=False)
            except Cancelled:
                raise
            except (KeyError, OSError, RecoveryError, sqlite3.Error):
                continue


def local_preview(journal, project, chat, mode, first=1, last=None, progress=quiet):
    from .conversion import Bundle, plan_import
    from .core import catalogue, properties, verify

    progress("archive_analysis", 0, 0)
    if set(verify(project, progress, history_only=True)) - {"ok"}:
        raise RecoveryError("Исходные страницы истории изменились или отсутствуют.")
    with catalogue(project) as db:
        archive = Path(properties(db)["original"])
    progress("duplicates", 0, 0)
    data = plan_import(
        project, chat, "source", progress, media_mode=mode, first_record=first, last_record=last
    )
    if journal.test_reset_id is not None:
        data["test_reset_id"] = journal.test_reset_id
    bundle = Bundle(project, data)
    from .owner import app_home

    if not journal.test_reset_id:
        roots = [archive.parent, project.parent, journal.path.parent / "packages"]
        if journal.path.parent == app_home():
            roots.extend((Path.home() / "Documents", Path.home() / "Downloads"))
        learn_legacy(journal, bundle, roots, progress)
        index_old_projects(journal, [project.parent, journal.path.parent / "projects"], progress)
    journal.classify_interrupted()
    return bundle, journal.review_bundle(bundle), archive


def discover_for_package(journal, bundle, progress=quiet):
    from .owner import app_home

    if not journal.test_reset_id:
        roots = [bundle.root.parent, journal.path.parent / "packages"]
        if journal.path.parent == app_home():
            roots.extend((Path.home() / "Documents", Path.home() / "Downloads"))
        learn_legacy(journal, bundle, roots, progress)
        index_old_projects(journal, [journal.path.parent / "projects"], progress)
    journal.classify_interrupted()


def history(journal):
    """Local dashboard projections. Only aggregate allowlisted data goes to public reports."""
    from .journal import _record_ranges

    with journal.connect() as db:
        profiles = [dict(r) for r in db.execute("SELECT * FROM import_profiles ORDER BY updated DESC,title")]
        rows = db.execute("""SELECT o.id,o.peer,o.account,o.created,o.mode,o.test_reset_id,o.phase,o.state,o.outcome,o.messages,o.media,
            o.media_done,o.start_request_accepted,o.server_id,o.manifest_sha256,o.scope,e.confirmed,
            p.project,s.archive,json_extract(p.manifest,'$.chat_title') AS archive_chat,c.checked_at
            FROM operations o LEFT JOIN operation_record_evidence e ON e.operation=o.id
            LEFT JOIN import_packages p ON p.sha=o.manifest_sha256 LEFT JOIN registry_checks c ON c.operation=o.id
            LEFT JOIN package_sources s ON s.sha=o.manifest_sha256
            WHERE o.mode='owner' ORDER BY o.rowid DESC""").fetchall()
    ops = []
    for raw in rows:
        row = dict(raw)
        confirmed = (
            row["messages"] if row["state"] == "COMPLETED" else count(_record_ranges(row["confirmed"]) or ())
        )
        state = _status(row, False) or "NEW"
        row.update(
            confirmed_count=confirmed,
            pending=row["messages"] - confirmed
            if state in ("UNKNOWN_SERVER_STATE", "POSSIBLY_IMPORTED")
            else 0,
            status=state,
        )
        ops.append(row)
    for profile in profiles:
        related = [row for row in ops if row["peer"] == profile["peer"]]
        own = [row for row in related if row["account"] == profile["account"]]
        successful = next((r for r in own if r["start_request_accepted"]), {})
        verified = max(
            (r for r in own if r["confirmed_count"] and r["checked_at"]),
            key=lambda r: r["checked_at"],
            default={},
        )
        profile.update(
            operations=len(own),
            confirmed=sum(r["confirmed_count"] for r in related),
            pending=sum(r["pending"] for r in related),
            last_success=successful.get("created", ""),
            last_success_id=successful.get("id"),
            last_verified=verified.get("checked_at", ""),
            last_verified_id=verified.get("id"),
        )
    return profiles, ops


def recover_package(journal, job, progress=quiet):
    """Recover exact bytes or regenerate from the verified project and original choices."""
    from .conversion import load_bundle, prepare
    from .core import verify
    from .integrity import check_local
    from .profiles import PROFILES

    with journal.connect() as db:
        row = db.execute(
            """SELECT o.manifest_sha256,p.* FROM operations o LEFT JOIN import_packages p
              ON p.sha=o.manifest_sha256 WHERE o.id=? AND o.peer=?""",
            (job, journal.endpoint.key),
        ).fetchone()
    if not row or not row["manifest"]:
        raise RecoveryError(
            "У прежней версии нет сохранённого манифеста. Для этой операции нужен исходный пакет."
        )
    sha = row["manifest_sha256"]
    root = recovery_root(journal, sha)
    if root.is_dir():
        saved = load_bundle(root, progress)
    else:
        project = local(Path(row["project"])) if row["project"] else None
        if not project or not (project / "history.sqlite").is_file():
            raise RecoveryError("Исходный проект отсутствует. Откройте его неизменённую копию.")
        if set(verify(project, progress)) - {"ok"}:
            raise RecoveryError("Исходные файлы изменились или отсутствуют.")
        data = json.loads(row["manifest"])
        selection = data.get("selection", {})
        with new_directory(root, (project,)) as work:
            built = prepare(
                project,
                data["chat_id"],
                work / "package",
                data["timezone"],
                progress,
                profile=next(k for k, v in PROFILES.items() if v == data["profile"]),
                media_mode=data.get("media_mode", "text_media"),
                date_policy=data["date_policy"],
                fixture=data.get("fixture"),
                first_record=selection.get("first_record", 1),
                last_record=selection.get("last_record"),
                sample=selection.get("sample"),
                record_ranges=selection.get("ranges"),
                test_reset_id=data.get("test_reset_id"),
                video_policy=data.get("video_policy"),
                video_decisions=data.get("video_decisions"),
                annotate=data.get("annotate", False),
                record_kind=selection.get("record_kind", "all"),
            )
            if built.manifest_hash() != sha or check_local(built, project, progress)["status"] != "verified":
                raise RecoveryError("Пересобранный пакет отличается от сохранённого манифеста.")
            for child in built.root.iterdir():
                child.rename(work / child.name)
            built.root.rmdir()
        saved = load_bundle(root, progress)
    if saved.manifest_hash() != sha:
        raise RecoveryError("Контрольная сумма сохранённой операции расходится с журналом.")
    return saved
