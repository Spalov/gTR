# gTR 1.0.0-beta.1 engineering plan

Baseline under review: Alpha 0.4.0 source and Windows portable distribution. The canonical archive SQLite format, the supported package readers, the parser and the Telethon adapter are retained. The offline socket guard remains in effect. Live Telegram validation is reserved for an explicitly authorized owner acceptance test.

## Findings and implementation sequence

| Area | Baseline finding | Beta implementation |
|---|---|---|
| `journal.py`, `importer.py` | The durable final boundary exists. A single source scope blocks every subset after acceptance. The operational states mix checks and server milestones. | Add an independently validated lifecycle and commit it with existing safety states and event rows. Compare exact selected ordinal ranges at reservation time. Keep destination locks and legacy proof intact. |
| `conversion.py`, `integrity.py` | Prefix sampling cannot express a second non-overlapping subset. Text-only integrity currently hashes the entire workspace. | Add an explicit canonical ordinal window to the package. Preserve the full source ledger and isolate text evidence from attachment access. |
| `media.py`, `payloads.py`, `viewer.py` | Type detection and safe byte copies exist. Metadata inspection, duplicate inventory, and cancellation during preview hashing are limited. | Add a bounded media inspector, compatibility inventory, and verified previews. Keep original bytes. Keep an optional local preview adapter contract for gFL. |
| `gui.py`, `telegram_ui.py`, `verification.py` | Existing tabs and account, transfer and evidence controls work independently. Operators must infer the correct order. | Add a guided eight-step import flow that drives the existing account and import components. Give validation, transfer and verification separate counters and persisted outcomes. |
| `packaging/` | The exact Alpha ZIP is reproducible from pinned dependencies with machine-generated timestamps. The C# launcher is unsigned. | Build from pinned inputs with normalized archive metadata. Investigate Authenticode, keep the separate launch script and record the actual Windows smoke results. |

## Data and safety decisions

- Keep the previous journal `state` as a transport/recovery detail. Beta `phase` is the durable operator lifecycle from `CREATED` through `COMPLETED`. Migrate old rows with the evidence available at that stage. A completed phase requires independent verification.
- Store the exact included source ordinals as coalesced closed intervals within an existing source/chat scope. Same-scope overlapping records remain blocked after acceptance. Disjoint Beta selections may proceed. An old operation whose selected ordinals were never recorded remains a conservative scope-wide guard. An unknown final result always holds the destination lock.
- Perform both replay checks and reservation against the selected package, account and target, with the final reservation in the transaction that authorizes the upload. No retry is issued for a mutating request. Bounded backoff is reserved for repeatable read checks and never overrides Telegram wait instructions.
- Source date, UTC offset and ordinal are retained. Beta owner imports use historical dates. The previous alternate timeline option remains usable for local planning and diagnostics.
- The owner authorizes consented preflight and the actual transfer separately. Synthetic fixtures are visibly marked and generated only when selected. A QA run opens no Telegram sessions or sockets.
- Keep full media and verification evidence on the operator's computer. Default shareable diagnostics carry counts, hashes only where explicitly selected, sanitized outcomes and generated filenames. Source paths, conversation content and credentials are excluded from public exports.

Run the baseline and focused suites, inspect native UI and archive outputs, and publish both ZIPs only after final offline checks.
