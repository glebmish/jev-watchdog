"""What an attached dashboard reads: the last display records, numbered.

`publish` is called from the hook path and the judge workers and only appends. A dashboard
asks for what came after the last record it has; `boot` tells it a restarted watchdog, whose
numbers start over.
"""

import secrets
from collections import deque

BACKLOG = 2000


class Feed:
    def __init__(self, backlog: int = BACKLOG) -> None:
        self.boot = secrets.token_hex(8)
        self._records: deque[dict] = deque(maxlen=backlog)
        self._seq = 0

    def publish(self, record: dict) -> None:
        self._seq += 1
        self._records.append(record | {"seq": self._seq})

    def since(self, seq: int = 0) -> list[dict]:
        return [record for record in self._records if record["seq"] > seq]
