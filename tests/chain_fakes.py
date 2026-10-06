"""Adapters for faking on-chain market preparation in sync tests."""


def as_batch(single):
    """Turn a fake `prepare_market_on_chain(admin, question, labels)` into a
    fake `prepare_markets_on_chain(admin, items)`: one result per item, an
    exception in place of a market that raised."""

    def batch(admin, items):
        out = []
        for question, labels in items:
            try:
                out.append(single(admin, question, labels))
            except Exception as exc:  # noqa: BLE001 - mirrors the real batch
                out.append(exc)
        return out

    return batch
