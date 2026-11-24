# gTR Alpha 0.4.0 engineering report

## Audit of the 0.3.0 reference

The source workspace contains the application modules, synthetic fixtures, tests, a pinned Windows environment and release tooling. The inspected `release/html-dates/gTR-0.3.0-alpha-Windows-x64.zip` has SHA-256 `004f31db6cebae994376d6d76e229f7ecff136852e2baaacf8b7c9221700a79c`, which matches the audit. Its matching source archive is available locally. The installed Telethon 1.46.0 schema uses layer 233.

| Priority | Reference in 0.3.0 | Finding |
|---|---|---|
| P0 | `telegram_ui.py:511–535`, `importer.py:195–210` | The message counter becomes full during staging. Finalization follows every successful media upload. |
| P0 | `mtproto.py:94–141`, `journal.py:45–57` | Media rejections lose per-item context. Two acknowledgements before a failure identify the third attempted upload under sequential execution. The cause of the reported live `MEDIA_INVALID` is unknown. Candidate explanations include incompatible content, constructor, or server-side validation. |
| P0 | `conversion.py:183–235`, `payloads.py:70–153` | A zero-attachment experiment needs its own preparation mode and omission ledger. Changing the representation during dispatch would invalidate the reviewed package. |
| P0 | `journal.py:193–311`, `verification.py:174–349` | Staging, uncertain finalization and confirmed readback need separate evidence. Native sender fields and historical labels have different meanings. |
| P1 | `observation.py:90–155`, `verification.py:150–171` | An empty ID window is bounded evidence. Repeated content can leave ambiguous matches. |
| P1 | `dates.py:89–150`, `history.py:72–107`, `profiles.py:19–30` | Source timestamps retain their offset and UTC. The candidate wire date has second precision and no offset. |
| P1 | `files.py`, `conversion.py:401–412`, `owner.py:24–61` | Reparse-point checks and DPAPI protect distinct surfaces. Projects and staging bytes remain sensitive plaintext. |

The owner reported a media rejection and an empty selected window. No live account, socket to Telegram, or original server payload was accessed during implementation. Photo MIME and a local file signature establish local plausibility. They do not establish acceptance by Telegram. [History Import protocol](https://core.telegram.org/api/import) · [Media upload RPC](https://core.telegram.org/method/messages.uploadImportedMedia)

## Design and invariants

1. Keep the canonical archive database and existing package readers. Extend the package ledger with the media policy and omission counts. Historical passthrough remains the default time policy.
2. An owner confirmation binds the checked package, account and target. The owner explicitly attests consent for affected participants and the destination before the preflight can send a sample of the transcript. Operator review of Telegram's own confirmation is required before staging.
3. Keep SQLite durable as the point of coordination for each destination. Record attempt metadata before dispatch and outcomes after the response. Persist the final-start boundary before invoking Telegram. A lost response can leave that boundary uncertain. It never grants an automatic retry. Staged recovery uses explicit acknowledgement and local evidence that the final boundary was never passed.
4. Keep the independent readback and client observations separate. Historical labels and dates are checked as import metadata. Native author and direction observations can be reported but the API offers no parameter to assign those fields or the original message IDs. [Finalization RPC](https://core.telegram.org/method/messages.startHistoryImport) · [Imported message metadata](https://core.telegram.org/constructor/messageFwdHeader)
5. Default diagnostic exports whitelist fields. Original message references, checksums, and other correlators are local technical details. No stored authorization material or message bodies belong in this report.

## Media preparation matrix

| Original evidence | Representation chosen before dispatch | Local treatment |
|---|---|---|
| Photo with a recognized JPEG or PNG signature | `InputMediaUploadedPhoto` in the standard mode | Retain source bytes and validate size and hash. |
| Document or validated media with applicable audio, video, sticker, or animation attributes | `InputMediaUploadedDocument` | Include the corresponding bounded attributes when available. Preserve unsupported native shapes as documents. |
| Unknown type or explicit document mode | `InputMediaUploadedDocument` | Preserve source bytes and expose the fallback. |
| Multiple attachments | ZIP document | Preserve verified originals as distinct members. |
| Structured payload or unavailable original handled by the existing converter | JSON or ZIP document | Report the representation and any missing original. |
| Missing or invalid package, inconsistent markers, wrong checksum or unsupported input declaration | Local preflight rejects the package | The import is halted before staging. |

Combinations remain marked unverified until a server response is observed. A successful upload constructor is recorded independently from a final start acknowledgement. The named media marker and uploaded filename must agree. [InputMedia schema](https://core.telegram.org/type/InputMedia) · [Media upload RPC](https://core.telegram.org/method/messages.uploadImportedMedia)

## Evidence boundaries

Date-based observation filters the imported marker, historical UTC date and hashed text after an explicitly bounded scan of recent server history. A query for Telegram's native message date alone cannot reliably find historical dates recorded in forwarding metadata. Opt-in full media hashing uses a shared byte budget and compares the server-returned representation with the packaged bytes. A different hash does not identify the transformation or its cause.

## Release verification

Ruff passed. The packaged Python ran 116 tests on Windows 11 Pro 26200. The archive smoke also verified both ZIPs, the launcher file version, CLI version output and native-window startup via `gTR.cmd`. Application Control rejected `gTR.exe` with code 4551 on this host. A clean host with unsigned executables permitted was not available. The Windows CI workflow is included in the source ZIP and has not been run on a hosted runner for this release.

Reproduction commands are in [BUILD.md](BUILD.md). The separately published `SHA256SUMS.txt` identifies the exact packaged results.
