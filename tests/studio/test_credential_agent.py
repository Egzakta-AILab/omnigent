import hashlib
import json
import time
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from omnigent.db.db_models import current_workspace_id
from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.stores.studio_tasks import StudioTaskStore
from omnigent.studio import rpc
from omnigent.studio.credential_agent import (
    cleanup_credentials,
    create_credential_agent,
    write_private,
)
from omnigent.studio.seat_pool import SeatPool, profile_environment


async def test_broker_retries_real_admission_before_native_launch(db_uri, tmp_path, monkeypatch):
    harness = "codex-native"
    secret = tmp_path / "quota-token"
    write_private(secret, uuid4().hex)
    slots = {}
    tasks = StudioTaskStore(db_uri)
    for name in ("a", "b"):
        slots[hashlib.sha256(name.encode()).hexdigest()] = {
            "harness": harness,
            "profile": name,
            "quota_url": "https://quota.invalid/" + name,
            "quota_token_file": str(secret),
        }
        (tmp_path / name).mkdir(mode=0o700)
        tasks.admin_seat(
            "admin",
            rpc.Seat(slot_id=name, harness=harness, encrypted_profile_ref=name, enabled=True),
        )
    manifest = tmp_path / "manifest"
    write_private(
        manifest,
        json.dumps({"workspaces": {str(current_workspace_id()): {"subscriptions": slots}}}),
    )
    seen = []

    def external(request):
        seen.append(request.url.path)
        return httpx.Response(429 if len(seen) == 1 else 200, json={"available": True})

    broker = create_credential_agent(
        manifest, tmp_path, lambda: httpx.Client(transport=httpx.MockTransport(external))
    )
    monkeypatch.setenv("OMNIGENT_STUDIO_SEAT_ROOT", str(tmp_path))
    with TestClient(broker) as client:
        start = time.monotonic()
        registry = HostRegistry()
        pool = SeatPool(db_uri)
        registry.studio_profile_for_launch = lambda _, frame: pool.launch_profile(
            frame.harness, frame.session_id, client
        )
        conn = registry.register(
            uuid4().hex,
            object(),
            HostHelloFrame(version="current", frame_protocol_version=1, name="test"),
            owner="bot",
        )
        frame = HostLaunchRunnerFrame(
            request_id=uuid4().hex,
            binding_token=uuid4().hex,
            workspace="/checkout",
            session_id=uuid4().hex,
            harness=harness,
        )
        await registry.send_launch(conn, encode_host_frame(frame))
        delivered = decode_host_frame(conn.outbound_queue.get_nowait().data)
        assert isinstance(delivered, HostLaunchRunnerFrame)
        chosen = delivered.studio_seat_profile
        assert conn.outbound_queue.empty()
    assert seen == ["/a", "/b"]
    assert time.monotonic() - start < 10
    assert profile_environment(chosen, harness) == {"CODEX_HOME": str(tmp_path / "b")}


def test_private_paid_grant_and_no_overdraft_fallback(db_uri, tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    public = tmp_path / "issuer.pem"
    write_private(public, pem)
    token_file = tmp_path / "issuer.token"
    write_private(token_file, uuid4().hex)
    origin = "https://inference.invalid"
    grant_url = "https://billing.invalid/grants"
    api = {
        "model": "priced-model",
        "origin": origin,
        "grant_url": grant_url,
        "issuer_key_file": str(token_file),
        "attestation_public_key_file": str(public),
    }
    harness = "codex-native"
    org = {
        "api": {harness: api},
        "on_prem": {harness: {"profile": "local", "provider": "sparkle"}},
    }
    manifest = tmp_path / "manifest"
    write_private(manifest, json.dumps({"workspaces": {str(current_workspace_id()): org}}))
    valid = True

    def issue(request):
        body = json.loads(request.content)
        expires = body.pop("expires_by")
        credential = uuid4().hex
        claims = {
            **body,
            "no_overdraft": valid,
            "exp": expires,
            "nbf": int(time.time()),
            "iat": int(time.time()),
            "iss": grant_url,
            "aud": "omnigent-studio-seat",
            "credential_sha256": hashlib.sha256(credential.encode()).hexdigest(),
        }
        return httpx.Response(
            200,
            json={
                "credential": credential,
                "attestation": jwt.encode(claims, key, algorithm="EdDSA"),
            },
        )

    broker = create_credential_agent(
        manifest, tmp_path, lambda: httpx.Client(transport=httpx.MockTransport(issue))
    )
    monkeypatch.setenv("OMNIGENT_STUDIO_API_FALLBACK", "1")
    monkeypatch.setenv("OMNIGENT_STUDIO_LOCAL_FALLBACK", "1")
    pool = SeatPool(db_uri)
    monkeypatch.setattr(pool, "reserve_paid", lambda _: ("acme/repo#1", 100))
    with TestClient(broker) as client:
        profile = pool.launch_profile(harness, uuid4().hex, client)
        assert profile.startswith("api-") and (tmp_path / profile / "auth.json").is_file()
        valid = False
        assert pool.launch_profile(harness, uuid4().hex, client) == "local"
    # The runner's own session state survives key expiry and secret removal.
    directory = tmp_path / profile
    (directory / "history").touch()
    marker = next(tmp_path.glob(".studio-expiry-*"))
    marker.write_text("1")
    cleanup_credentials(tmp_path)
    assert not (directory / "auth.json").exists() and (directory / "history").exists()
