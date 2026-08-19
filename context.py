from contextvars import ContextVar

# database
database_session: ContextVar = ContextVar("database_session", default=None)

# trace
trace_id: ContextVar = ContextVar("trace_id", default=None)
