"""System-boundary fault injection shared by Config Studio transaction tests."""

from __future__ import annotations

from collections.abc import Callable

import spica.config.document_transaction as transaction_module


def after_first_transaction_fsync(
    monkeypatch,
    callback: Callable[[], None],
) -> None:
    """Run ``callback`` after the transaction's first successful fsync."""

    real_fsync = transaction_module.os.fsync
    fired = False

    def fsync_then_callback(descriptor: int) -> None:
        nonlocal fired
        real_fsync(descriptor)
        if not fired:
            fired = True
            callback()

    monkeypatch.setattr(transaction_module.os, "fsync", fsync_then_callback)


__all__ = ["after_first_transaction_fsync"]
