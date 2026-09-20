"""What an attached dashboard sees: the last display records, and whoever is following them.

`publish` is called from the hook path and the judge workers, so it never blocks and never
waits for a reader: one that falls behind is dropped, and comes back with `since`.
"""

import asyncio
import secrets
from collections import deque
from dataclasses import dataclass

BACKLOG = 2000
SUBSCRIBER_QUEUE = 1000


@dataclass(eq=False)
class Subscription:
    backlog: list[dict]  # records after `since`, oldest first
    queue: asyncio.Queue[dict | None]  # live records; None ends the stream


class Feed:
    def __init__(self, backlog: int = BACKLOG, queue_size: int = SUBSCRIBER_QUEUE) -> None:
        self.boot = secrets.token_hex(8)  # a follower tells a restarted watchdog by it
        self._records: deque[dict] = deque(maxlen=backlog)
        self._queue_size = queue_size
        self._subscriptions: list[Subscription] = []
        self._seq = 0
        self._closed = False

    def publish(self, record: dict) -> dict:
        self._seq += 1
        record = record | {"seq": self._seq}
        self._records.append(record)
        for subscription in list(self._subscriptions):
            try:
                subscription.queue.put_nowait(record)
            except asyncio.QueueFull:
                self._end(subscription)
        return record

    def subscribe(self, since: int = 0) -> Subscription:
        backlog = [record for record in self._records if record["seq"] > since]
        subscription = Subscription(backlog, asyncio.Queue(self._queue_size))
        if self._closed:
            # An /events accepted while the watchdog stops: left open, it would hold the
            # server's cleanup up for aiohttp's whole shutdown timeout.
            subscription.queue.put_nowait(None)
        else:
            self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    def close(self) -> None:
        self._closed = True
        for subscription in list(self._subscriptions):
            self._end(subscription)

    def _end(self, subscription: Subscription) -> None:
        self.unsubscribe(subscription)
        while not subscription.queue.empty():
            subscription.queue.get_nowait()
        subscription.queue.put_nowait(None)
