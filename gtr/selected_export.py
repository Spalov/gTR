"""Open a loose HTML or JSON file with exactly its companion pages and referenced attachments."""

from .archive import snapshot
from .files import local
from .media import reference
from .model import FileInfo, RecoveryError, Snapshot
from .parser import PAGE, parse


def selected_snapshot(source, destination, timezone_name, progress):
    kind = source.suffix.lower()[1:]
    root = source.parent
    if kind == "html":
        if PAGE.fullmatch(source.name):
            candidates = [(int(m[1] or 1), local(p)) for p in root.iterdir() if (m := PAGE.fullmatch(p.name))]
        else:
            candidates = [(1, source)]
        names = [p.name for _, p in candidates]
        sources = ({"": [(n, p.name) for n, p in candidates]}, [])
    else:
        names = [source.name]
        sources = ({}, names[:])
    inventory = {name: FileInfo((root / name).stat().st_size, "", "", False) for name in names}
    draft = Snapshot(root, inventory, "", 0, kind)
    rows = parse(draft, timezone_name, progress, export_format=kind, sources=sources)
    referenced = set(names)
    for chat in rows:
        for message in chat.messages:
            for asset in message.assets:
                if asset.metadata or not asset.original:
                    continue
                try:
                    relative = reference(asset)
                    if local(root / relative).is_file():
                        referenced.add(relative)
                except (ValueError, OSError, RecoveryError):
                    continue
    final = snapshot(root, destination, progress, selection=sorted(referenced))
    final.source_type = kind
    return final, sources
