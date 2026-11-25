"""Bounded incremental reader for a single Telegram Desktop JSON conversation."""

import json
import re

from .model import RecoveryError

CHUNK = 64 * 1024
MAX_VALUE = 16 * 1024**2
SPACE = re.compile(r"[ \t\r\n]*")


class MultipleChats(Exception):
    """The account export uses a different, nested layout."""


class Reader:
    def __init__(self, source):
        self.source = source
        self.buffer = ""
        self.offset = 0
        self.ended = False
        self.decoder = json.JSONDecoder()

    def fill(self):
        chunk = self.source.read(CHUNK)
        self.buffer = self.buffer[self.offset:] + chunk
        self.offset = 0
        self.ended = not chunk

    def token(self):
        while True:
            self.offset = SPACE.match(self.buffer, self.offset).end()
            if self.offset < len(self.buffer):
                return self.buffer[self.offset]
            if self.ended:
                return ""
            self.fill()

    def take(self, token):
        if self.token() != token:
            raise ValueError("Unexpected JSON token")
        self.offset += 1

    def value(self):
        self.token()
        while True:
            if len(self.buffer) - self.offset > MAX_VALUE:
                raise RecoveryError("Слишком большая отдельная запись JSON.")
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.offset)
            except json.JSONDecodeError:
                if self.ended:
                    raise
            else:
                if self.ended or (end < len(self.buffer) and self.buffer[end] in " ,:\t\r\n]}"):
                    self.offset = end
                    return value
            self.fill()

    def members(self, closing):
        if self.token() != closing:
            yield None
            while self.token() == ",":
                self.take(",")
                yield None
        self.take(closing)


def fields(path):
    with path.open(encoding="utf-8-sig") as source:
        reader = Reader(source)
        reader.take("{")
        for _ in reader.members("}"):
            key = reader.value()
            if not isinstance(key, str):
                raise TypeError("Expected a property name")
            reader.take(":")
            if key in {"chats", "left_chats"}:
                raise MultipleChats
            if key != "messages":
                yield key, reader.value()
                continue
            reader.take("[")
            yield "messages", None
            for _ in reader.members("]"):
                row = reader.value()
                if not isinstance(row, dict):
                    raise TypeError("Expected a message object")
                yield None, row
        if reader.token():
            raise ValueError("Unexpected trailing JSON")
