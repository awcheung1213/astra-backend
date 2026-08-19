import logging
import uuid

from context import trace_id


class Trace:
    length = 6
    static_trace_id: str | None = None

    @staticmethod
    def start_trace(new_trace_id: str | None = None) -> None:
        new_trace_id = new_trace_id if new_trace_id else str(uuid.uuid4())[: Trace.length]
        try:
            # attempt to add the trace_id to the context
            trace_id.set(new_trace_id)
        except:  # noqa
            # store the trace_id in a static variable if the context is not available
            Trace.static_trace_id = new_trace_id

    @staticmethod
    def get_trace_id() -> str:
        try:
            if Trace.static_trace_id is not None:
                return Trace.static_trace_id
            return trace_id.get()
        except:  # noqa
            return "-" * Trace.length

    @staticmethod
    def initializing_logging(fmt: str = "%(trace_id)s:%(levelname)s:%(name)s:%(message)s") -> None:
        # create a logging filter to add trace_id to logs
        class TraceFilter(logging.Filter):
            def filter(self, record) -> bool:
                record.trace_id = Trace.get_trace_id()
                return True

        try:
            # get the existing logging handler
            logging_handler = logging.getLogger().handlers[0]
        except:  # noqa
            # create a new logging handler if one does not exist
            logging_handler = logging.StreamHandler()
            logging.getLogger().addHandler(logging_handler)

        # update the logging handler format and add the trace filter
        logging_handler.setFormatter(logging.Formatter(fmt))
        logging_handler.addFilter(TraceFilter())
