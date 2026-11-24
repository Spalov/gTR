"""A bounded local journal of product operations."""

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .files import local
from .model import RecoveryError


class Diagnostics:
    def __init__(self, directory):
        self.path = Path(directory) / "diagnostics.log"
        self.handler = None
        try:
            self.path = local(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handler = RotatingFileHandler(
                self.path, maxBytes=256 * 1024, backupCount=1, encoding="utf-8"
            )
        except (RecoveryError, OSError):
            pass

    def write(self, message):
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        line = f"{now}   {' '.join(message.splitlines())}"
        if self.handler:
            try:
                self.handler.emit(logging.LogRecord("gTR", logging.INFO, "", 0, line, (), None))
            except OSError:
                pass
        return line

    def recent(self):
        try:
            if self.handler:
                with self.path.open("r", encoding="utf-8") as source:
                    return "".join(source.readlines()[-300:]).rstrip()
        except OSError:
            pass
        return ""

    def close(self):
        if self.handler:
            self.handler.close()
