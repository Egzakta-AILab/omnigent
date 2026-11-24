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


def test_seat_fallback_uses_operator_provider_and_preserves_reference(db_uri, monkeypatch):
    import json
    from uuid import uuid4

    import httpx
    from fastapi import HTTPException

    from omnigent.db.db_models import workspace_scope

    tasks = StudioTaskStore(db_uri, clock=lambda: 1000000)
    pool = SeatPool(db_uri, clock=tasks.clock)
    base = {"slot_id": "primary", "harness": "claude-native", "enabled": True}
    ref = uuid4().hex
    tasks.admin_seat("operator", rpc.Seat(**base, encrypted_profile_ref=ref, weight=4))
    switch = rpc.Seat(**base, routing_mode="fallback")
    with pytest.raises(HTTPException, match="No fallback"):
        tasks.admin_seat("operator", switch)
    monkeypatch.setenv("OMNIGENT_STUDIO_LOCAL_FALLBACK", "1")
    assert tasks.admin_seat("operator", switch)["weight"] == 4
    modes = []

    def admit(request):
        body = json.loads(request.content)
        modes.append(body["mode"])
        if body["mode"] == "on_prem":
            assert "reference" not in body
            return httpx.Response(200, json={"profile": "local", "provider": "sparkle"})
        assert body["reference"] == ref
        return httpx.Response(200, json={"profile": "work"})

    with httpx.Client(transport=httpx.MockTransport(admit)) as client:
        assert pool.launch_profile("claude-native", uuid4().hex, client) == "local"
        tasks.admin_seat("operator", rpc.Seat(**base, routing_mode="subscription"))
        assert pool.launch_profile("claude-native", uuid4().hex, client) == "work"
        tasks.admin_seat("operator", rpc.Seat(**{**base, "enabled": False}))
        assert pool.launch_profile("claude-native", uuid4().hex, client) == "local"
        assert modes == ["on_prem", "subscription", "on_prem"]
    assert tasks.inventory("operator", rpc.Page(), True)["seats"][0]["usage_7d"] == 2
    assert "usage_7d" not in tasks.inventory("operator", rpc.Page())["seats"][0]
    with workspace_scope(45):
        assert tasks.inventory("operator", rpc.Page(), True)["seats"] == []
    tasks.clock = lambda: 2000000
    assert tasks.inventory("operator", rpc.Page(), True)["seats"][0]["usage_7d"] == 0
