"""Where the engine talks.

The engine only knows about ``Notifier``. The Discord implementation lives in
crypto_commands.py so nothing in this package imports discord.
"""

import time


class Notifier:
    async def notify(self, level, title, message, fields=None):
        raise NotImplementedError


class LogNotifier(Notifier):
    LEVEL_TAG = {"info": "INFO", "trade": "TRADE", "warn": "WARN", "error": "ERROR"}

    def __init__(self, prefix="[Crypto]"):
        self.prefix = prefix

    async def notify(self, level, title, message, fields=None):
        tag = self.LEVEL_TAG.get(level, level.upper())
        stamp = time.strftime("%H:%M:%S")
        line = f"{self.prefix} {stamp} {tag} {title}: {message}"
        if fields:
            line += " | " + ", ".join(f"{k}={v}" for k, v in fields.items())
        print(line, flush=True)


class MultiNotifier(Notifier):
    def __init__(self, *sinks):
        self.sinks = [s for s in sinks if s]

    async def notify(self, level, title, message, fields=None):
        for sink in self.sinks:
            try:
                await sink.notify(level, title, message, fields)
            except Exception as e:
                print(f"[Crypto] notifier {type(sink).__name__} failed: {e!r}", flush=True)
