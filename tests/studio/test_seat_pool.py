from concurrent.futures import ThreadPoolExecutor
from time import monotonic

import pytest

from omnigent.stores.studio_tasks import StudioTaskStore
from omnigent.studio import rpc
from omnigent.studio.seat_pool import PoolExhausted, SeatPool, SeatQuota


def test_pool_balances_concurrent_reservations_and_retries_only_quota(db_uri):
    tasks = StudioTaskStore(db_uri)
    for slot, weight in [("one", 1), ("two", 2)]:
        tasks.admin_seat(
            "admin",
            rpc.Seat(
                slot_id=slot,
                harness="codex-native",
                encrypted_profile_ref=slot,
                enabled=True,
                weight=weight,
            ),
        )
    pool = SeatPool(db_uri)
    with ThreadPoolExecutor(max_workers=3) as workers:
        slots = list(workers.map(lambda _: pool.reserve("codex-native").slot_id, range(3)))
    assert sorted(slots) == ["one", "two", "two"]
    attempts = []

    def admit(seat):
        attempts.append(seat.slot_id)
        if len(attempts) == 1:
            raise SeatQuota(retry_after=60)
        return seat.slot_id

    start = monotonic()
    assert pool.dispatch("codex-native", admit) == attempts[-1]
    assert len(set(attempts)) == 2 and monotonic() - start < 10
    assert pool.reserve("codex-native").slot_id == attempts[-1]
    with pytest.raises(PoolExhausted):
        pool.reserve("claude-native")
