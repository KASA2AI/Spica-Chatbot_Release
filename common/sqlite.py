"""A transaction-scoped connection owns its handle until context exit."""
import sqlite3


class ClosingConnection(sqlite3.Connection):
    """Keep SQLite commit/rollback semantics and deterministically release I/O.

    The standard Connection context manager only ends the transaction; it
    does not close the handle. These stores open one connection per operation,
    so relying on cyclic garbage collection leaves Windows database files busy.
    Explicitly managed migration connections can still call close() themselves.
    """
    def __exit__(self, *exception):
        try:
            return super().__exit__(*exception)
        finally:
            self.close()
