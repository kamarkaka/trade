"""Error contracts shared by the core protocols (design §8.6).

``Broker.submit_order`` can fail in two very different ways, and conflating them is how a
retry doubles a real position:

* ``OrderNotPlacedError`` — the order DEFINITELY did not reach the book (rejected by
  validation or authorization, refused in read-only safe mode, or the request never left the
  process). It is safe to treat the order as never placed.
* Any OTHER exception — the outcome is UNKNOWN (timeout, 5xx, lost response): the order may
  have been placed and must never be blindly re-sent; only reconciliation may resolve it.
"""

from __future__ import annotations


class OrderNotPlacedError(Exception):
    """``submit_order`` failed and the order definitely was NOT placed."""


__all__ = ["OrderNotPlacedError"]
