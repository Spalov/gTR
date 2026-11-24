# Incremental history import

## Why 1.0.1 asked for a previous folder

The journal recorded a package fingerprint, a hash of the entire export plus its local chat number and, in newer operations, intervals of source ordinals. Editing an export changed its scope. Old operations sometimes had only a count. The recovery dialog needed the previous manifest to tell which rows belonged to that count.

## Implementation

The offline planner reads the full canonical source sequence before applying selection, date and media policies. A numbered JSON conversation contributes its typed Telegram source peer. Each record contributes its original message ID, historical author ID or label, normalized UTC instant, original offset, text digest, reply reference, ordered media types and content hashes. A missing original contributes its reference. A path, caption, basename, file digest or timestamp on its own cannot identify a message.

Within the same verified export, source ordinals are exact. Across exports the planner compares trusted conversation and message IDs and the payload signature. Two unequal known source peers stay independent. HTML rarely identifies a source peer. Matching title and multiple corroborating distinct ID/content pairs can establish continuity. An isolated matching ID or a changed payload goes to review. For unnumbered repetitions, the planner records relative occurrence, total multiplicity and surrounding numbered messages. An insertion, missing anchor or changed multiplicity can leave several plausible matches. Each affected record then goes to review. An export which omits source identifiers can therefore yield fewer eligible rows than a numbered export.

| Record status | Effect on the next package |
|---|---|
| NEW | Include after local validation |
| FAILED_BEFORE_START | Revalidate the new attempt. A recorded server wait still applies |
| READY_FOR_RETRY | Include after a safely closed preparation and validation |
| CONFIRMED_IMPORTED | Exclude. Preserve positive verification evidence |
| POSSIBLY_IMPORTED | Exclude while independent verification is pending |
| UNKNOWN_SERVER_STATE | Exclude. The profile's verification preference determines whether disjoint records are eligible |
| CONFLICT | Exclude. Inspect source identity, content or occurrence matching |

Private dialogs use a sorted pair of numeric Telegram IDs. Supergroups use their marked numeric peer. Profiles are per authorized account and target, while the destination lock is shared across profiles/windows. A selection is checked again inside the SQLite write transaction immediately before reservation. The import controller uses the same guard as the GUI. An accepted upload increments only the attachment counter. An accepted start records dispatch; positive record evidence comes from matched observations.

The «Проверять предыдущие операции» preference defaults to enabled on new and existing profiles. Turning it off permits a new package after a terminal unknown start if the local journal establishes a disjoint selection. The planner uses exact source ordinals or a complete record index across exports. It excludes previously dispatched, confirmed and ambiguous records. An incomplete selection can still block preparation. Active local reservations, unfinished staging and server-directed waits retain their protection.

The import transaction takes over a reservation from a terminal unknown start only after rechecking the disjoint package. The old operation retains its status, evidence and recovery actions in «История импортов». A later verification releases only the reservation belonging to the verified operation.

## Verified test resets

Test mode is an explicit opt-in in the selected private-dialog profile. The reset uses both accounts' independent authorizations and resolves the reciprocal numeric peer in each session. After a dialog disappears from the list, resolution can use Telegram contacts. Each session reads the unfiltered latest history with a limit of one. A positive count, nonempty returned list, inexact or cached reply, or failed request stops the reset. [Read message history](https://core.telegram.org/method/messages.getHistory) · [Read contacts](https://core.telegram.org/method/contacts.getContacts)

One SQLite transaction rechecks the dialog reservation, latest reset and operation events before attaching historical dispatches to the new test cycle. A prepared operation that starts later still counts as a new dispatch. Test packages carry the cycle ID so their verification evidence has a distinct manifest fingerprint. The reservation checks that the cycle is current. Ordinary imports continue to apply all operation records and rate limits. Previous operation rows, reports, and retained packages remain available.

## Migration and recovery

The existing imports.sqlite schema is extended inside BEGIN IMMEDIATE. Registry schema version 1 adds profiles, manifests, individual record identities, per-operation/per-record verification times and a record-status view. Pre-existing operation IDs, timestamps, counters, events, locks and DPAPI session bytes are preserved. Unknown future registry versions abort the entire migration. Old accounts and peers populate numeric profiles. Their display names and archive preferences are populated when the owner next chooses them. The application version of an unversioned legacy operation stays unknown.

The first preview searches standard gTR package locations and directly adjacent export/project locations for an exact recorded manifest. The planner can also map a legacy selection with known ordinals using a hash-verified canonical project of exactly the same archive and chat. It does not infer selection from counts. Where the previous release saved neither a selection nor recoverable matching files, affected records remain uncertain. The advanced recovery panel can link an exact copy discovered elsewhere.

For Alpha 0.1/0.2 hash-only starts, the planner also inspects the original starts/plans tables. A matching source scope and exact transcript/media fingerprint can link an intact package. Without those facts the target remains protected. The original Alpha rows remain in place. Unknown legacy dates and account identities are not reconstructed.

Before an owner operation contacts Telegram, the application saves verified package bytes under the local application data directory in gAssets/gTR/recovery, keyed by manifest SHA-256. This includes the transcript and each attachment. The SQLite copy of the manifest records the associated project. Normal recovery uses the saved package. If the entire cached package directory has expired, the application verifies the project files, regenerates the original selection and checks the complete manifest digest before publishing. Corrupt or missing data is reported. An interrupted local preparation can be repeated from the profile. Reopening a profile or reviewing history never starts a server operation.

The original files stay read-only. Each retained selection consumes local disk space, including media. Back up the whole application data directory with the app closed to preserve the database and its recovery files together. A rollback to the previous state uses a pre-upgrade backup in a separate directory. 1.0.1 does not understand new filename manifests or the cross-export protection.

ID-window and historical date scans compare source text digests, original times, labels, import flags and available attachment metadata in the actual destination. Two independent, complete historical scans can supply positive record presence after an accepted start. Resolving an unknown final dispatch still needs strong pre-start boundary evidence. Missing observations, truncated ranges and acknowledgement of file uploads cannot release that lock. Participant display names do not establish native Telegram authorship.
