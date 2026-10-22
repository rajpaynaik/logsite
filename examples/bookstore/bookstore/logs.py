"""JSON logs, one object per line, as production writes them: with the request's trace id."""

import contextvars
import json
import logging
import time
from datetime import datetime, timezone

trace_id = contextvars.ContextVar("trace_id", default=None)


class JsonFormatter(logging.Formatter):
    def __init__(self, clock=time.time, rewrite=lambda text: text):
        super().__init__()
        self.clock = clock
        self.rewrite = rewrite  # production paths, e.g. /srv/bookstore/...

    def format(self, record):
        out = {
            "timestamp": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "pathname": self.rewrite(record.pathname),
            "lineno": record.lineno,
            "trace_id": trace_id.get(),
            "service": "bookstore",
        }
        if record.exc_info:
            out["exc_info"] = self.rewrite(self.formatException(record.exc_info))
        return json.dumps(out)
