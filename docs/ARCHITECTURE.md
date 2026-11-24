# gTR 1.0.1 architecture

## Source and package boundaries

`archive.py` and `files.py` validate local paths and snapshot ZIP, RAR, HTML, JSON and directories under file, entry and total byte limits. `parser.py`, `dates.py`, `core.py` and `replies.py` build the read-only SQLite catalogue. Source file hashes, the original author label, date evidence, UTC offset and encounter order remain in the project. Historical order uses the source ordinal as the stable identity, including equal timestamps. Ambiguous dates and unresolved references remain explicit project findings.

`conversion.py` builds the same record ledger for a plan and a package. Schema 3 carries an additive selection window and preserves the whole source ledger. Each candidate has an inclusion result, original identity and recorded losses. Package paths, payload descriptors, bytes and transcript are validated before use. The owner transport freezes the approved bytes in a private local working directory. `integrity.py` checks the manifest against the project and can select only canonical history pages when verifying a text-only package. Those operations use attachment metadata from SQLite.

## Media

`media_engine.py` inspects signatures, structured headers, sizes and SHA-256, with capped metadata probes and cached results for identical paths. It lists missing bytes, mismatches, duplicate attachments and compatibility for the selected representation. `payloads.py` retains the original byte stream and selects a Telegram constructor. Document fallback is recorded, and users can choose plain documents explicitly. `media_preview.py` works from verified local files. Its optional `VideoThumbnailProvider` can be adapted for gFL. The provided local FFmpeg implementation constrains the input demuxer, protocols, duration of execution and output size. gFL and FFmpeg are optional.

## Import lifecycle

`telegram_ui.py` and `wizard.py` share the owner session actions. The user provides consent and authorizes the preliminary checks and final transfer separately. The worker executes `importer.ImportController` through `mtproto.TelegramAPI`. The application disables blind retries for mutations. Reads use bounded transient retries when permitted. Flood waits are persisted and shown.

```text
CREATED → VALIDATED → PREFLIGHT → AUTHORIZED → INITIALIZED
                       → UPLOADING → STARTING → VERIFYING → COMPLETED
Outcomes  FAILED, CANCELLED, PARTIAL, UNKNOWN_SERVER_STATE
```

SQLite commits each journal transition with its event and counters. The detailed Alpha state remains available for finer recovery semantics. Beta reserves the destination immediately before the first mutation. A durable `STARTING` boundary covers a crash or lost reply once `startHistoryImport` could have been sent. Upload success and a positive start reply have their own counters. `COMPLETED` requires independent verification. Recovery releases a pre-start lock only with journal and process-identity proof. A potentially dispatched operation retains its lock until there is sufficient evidence to classify it.

The replay guard tracks account and destination, package and wire hashes, source/chat scope, and coalesced inclusive intervals of the exact packaged ordinals. A prior accepted operation protects the matching records in the destination. The subset review separates confirmed records, records that require verification and records eligible for a new package. A legacy row can gain exact selection evidence when the original package is checked against its journal identity. The recovery path preserves prior journal rows and uses the same reservation boundary for a rebuilt package. Verification can resume after a restart using data already on disk. [Duplicate protection and recovery](SAFE_IMPORT.md)

## Verification and privacy

`observation.py` captures bounded message windows from independently authorized accounts. `verification.py` compares source bytes, content, dates, ordering, imported metadata, attachment accessibility and bytes, server sender fields, plus separately recorded client appearance. Package or source evidence alone cannot establish native fields or client display. The result carries a four-way status and field-level evidence. The journal stores the outcome summary for each operation. `reports.py` and `journal.py` produce redacted shareable diagnostics and explicit private reports.

Telegram sessions use Windows DPAPI with the current user's credentials. API credentials and login challenges remain transient. New working copies restrict filesystem access. Ordinary logs carry app actions and sanitized codes. Real imports and captures require owner actions. Automated tests refuse outbound connections and use fake Telegram adapters.
