"""Connection handling shared by the WebSocket feeds.

A feed subclass says how to subscribe and how to apply messages. This class keeps
it connected: it reconnects with backoff, saves every raw message, sends keepalive
pings, and reconnects (which brings fresh snapshots) when the feed asks for a
resync or the connection goes quiet.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import websockets

from poller.livebook import DISCONNECTED, RESYNCING, RawLog

logger = logging.getLogger(__name__)

PING_SECONDS = 5          # keepalive interval; a reply also proves the books are current
SILENCE_SECONDS = 30      # no reply or message for this long means the connection is dead
MIN_RESYNC_SECONDS = 10   # don't reconnect more often than this when a feed asks to resync
MAX_BACKOFF_SECONDS = 60


def utc_now():
    return datetime.now(timezone.utc)


class WebSocketFeed:
    exchange = None    # "kalshi" or "polymarket"
    raw_name = None    # folder under data/raw

    def __init__(self, raw_dir, url, clock=utc_now):
        self.url = url
        self.clock = clock
        self.raw = RawLog(raw_dir, self.raw_name)
        self.books = {}
        self.connected = False
        self.connected_at = None
        self.last_heard = None
        self.resync_needed = False
        self.resyncs = 0
        self._pong = None

    # ---------- for subclasses ----------

    def has_subscriptions(self):
        return bool(self.books)

    def subscribe_messages(self):
        raise NotImplementedError

    def handle(self, text, at):
        raise NotImplementedError

    def connect_kwargs(self):
        return {}

    def ready(self):
        """False if the feed can't run at all (missing credentials, say). Checked before each connect."""
        return True

    async def keepalive(self, ws):
        """Send a protocol ping. When the pong comes back, last_heard moves forward."""
        if self._pong is None:
            self._pong = await ws.ping()

    # ---------- connection ----------

    def _on_connect(self, at):
        self.connected, self.connected_at, self.last_heard = True, at, at
        self.resync_needed = False
        self._pong = None
        for book in self.books.values():
            book.mark(RESYNCING)   # until its snapshot arrives

    def _on_disconnect(self):
        self.connected = False
        self._pong = None
        for book in self.books.values():
            book.mark(DISCONNECTED)

    async def run(self, stop: asyncio.Event):
        """Stay connected until stop is set, reconnecting with backoff."""
        delay = 1
        while not stop.is_set():
            if not self.ready():
                return
            if not self.has_subscriptions():
                await _sleep(stop, 5)
                continue
            try:
                async with websockets.connect(self.url, open_timeout=15, max_size=None,
                                              ping_interval=None, **self.connect_kwargs()) as ws:
                    self._on_connect(self.clock())
                    for message in self.subscribe_messages():
                        await ws.send(json.dumps(message))
                    logger.info("%s feed connected, %d books", self.exchange, len(self.books))
                    if await self._read(ws, stop):
                        delay = 1
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
                logger.warning("%s feed connection problem: %s", self.exchange, exc)
            finally:
                self._on_disconnect()
            if not stop.is_set():
                await _sleep(stop, delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)

    async def _read(self, ws, stop):
        """Read until we need to reconnect. Returns True if any message arrived."""
        got_any = False
        loop = asyncio.get_running_loop()
        last_ping = loop.time() - PING_SECONDS
        while not stop.is_set():
            if loop.time() - last_ping >= PING_SECONDS:
                await self.keepalive(ws)
                last_ping = loop.time()
            try:
                text = await asyncio.wait_for(ws.recv(), timeout=1)
            except asyncio.TimeoutError:
                text = None
            at = self.clock()
            if self._pong is not None and self._pong.done():
                self._pong = None
                self.last_heard = at
            if text is not None:
                got_any = True
                self.raw.write(text if isinstance(text, str) else text.decode("utf-8", "replace"), at)
                try:
                    self.handle(text, at)
                except (ValueError, KeyError, TypeError) as exc:
                    logger.warning("Bad %s message, resyncing: %s", self.exchange, exc)
                    self.resync_needed = True
            if (at - self.last_heard).total_seconds() > SILENCE_SECONDS:
                logger.warning("%s feed silent for %ds; reconnecting", self.exchange, SILENCE_SECONDS)
                return got_any
            if self.resync_needed and (at - self.connected_at).total_seconds() >= MIN_RESYNC_SECONDS:
                self.resyncs += 1
                return got_any
        return got_any


async def _sleep(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
