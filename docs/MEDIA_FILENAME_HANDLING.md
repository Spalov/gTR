# Media names in Beta 2

In 1.0.1, plan_media generated a name such as gtr-0000001.pdf. The converter wrote it into the transcript. TelegramAPI then reused it for the file upload, the History Import placeholder and DocumentAttributeFilename. This explains the requested synthetic filename on the client side of the protocol. The final client rendering of a particular historical upload requires an observation from that client.

| Role | Value in Beta 2 |
|---|---|
| Local storage | Unique package-local ordinal and safe extension under media/ |
| Export evidence | Explicit JSON file_name or a filename supplied by an HTML document title, otherwise the export path's basename |
| Requested display name | Safe Unicode basename in display_name, accompanied by filename_source |
| Transcript placeholder | The name field, unique under Unicode case folding. Collisions receive a suffix |
| UploadImportedMedia.file_name | The exact placeholder written into the transcript |
| Uploaded file and document attribute | The requested display name. Two unrelated documents can request the same name |
| Observed name | Independently read from the returned historical document when available |

The loader verifies transcript-to-media mapping, distinct storage names, file sizes and SHA-256. It rejects unsafe or inconsistent filename metadata. Sanitization removes path components, bounds Unicode names by byte length, replaces controls and Windows-forbidden characters, and protects reserved device names. A generated JSON description or multi-attachment ZIP has a Russian descriptive name and explicit generated provenance. Generated files carry no claim to an original user-supplied filename.

The wire protocol exposes both the import filename and the nested uploaded media. Telegram documents a filename attribute for documents. Native photos have no equivalent filename attribute. Individual clients can choose their own presentation and downloaded filename. For a file where the visible name matters, the owner can choose document mode during preparation. Existing 1.0.1 packages keep their original transcript marker and request the best available export basename for documents. Opening an old operation to inspect its filename does not trigger another import. [History Import](https://core.telegram.org/api/import) · [Upload an imported attachment](https://core.telegram.org/method/messages.uploadImportedMedia) · [Document filename attribute](https://core.telegram.org/constructor/documentAttributeFilename)

Local tests inspect actual Telethon request objects with a fake transport and compare independent simulated readback, including a renamed server document. The manual procedure in OWNER.md checks visible filenames on two real clients. Public diagnostics omit filenames, paths, hashes and message text. Detailed local snapshots can contain participant and attachment metadata.

The offline media engine checks file signatures, extension consistency, dimensions, bounded JPEG EXIF orientation, audio/video metadata and hashes. The existing thumbnail provider accepts an optional local gFL-compatible adapter. No gFL installation or Telegram login is needed for local inspection.
