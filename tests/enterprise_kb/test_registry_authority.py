"""Real process/socket/SQLite checks for the single authority boundary."""
import json
import multiprocessing
from pathlib import Path
import socket
import threading
import tempfile

import pytest

from openviking.indexing.publication import PublicationStore, configured_store
from openviking.indexing.registry_authority import (
    AuthorityRepository, AuthorityUnavailable, RegistryAuthority,
)


def _serve(endpoint, database, token_file, ready):
    with RegistryAuthority(endpoint, database, token_file) as authority:
        ready.set()
        authority.serve_forever(poll_interval=0.02)


def _worker(endpoint, token_file, output):
    repository = AuthorityRepository(endpoint, token_file)
    token = repository.begin_write("a", "viking://resources/document")
    output.put(token)


@pytest.fixture
def setup():
    # Unix sockets have a short platform path limit; pytest's macOS root exceeds it.
    with tempfile.TemporaryDirectory(prefix="ov-reg-", dir="/tmp") as directory:
        private = Path(directory).resolve()
        private.chmod(0o700)
        token = private / "token"
        token.write_text("x" * 64)
        token.chmod(0o600)
        yield str(private / "s"), str(private / "registry.db"), str(token)


def _start(setup):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=_serve, args=(*setup, ready))
    process.start()
    assert ready.wait(15), "authority did not start"
    return process


def test_workers_share_fences_and_restart_preserves_uncertain_write(setup):
    process = _start(setup)
    repository = AuthorityRepository(setup[0], setup[2])
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    workers = [context.Process(target=_worker, args=(setup[0], setup[2], output)) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        tokens = {output.get(timeout=15) for _ in workers}
        for worker in workers:
            worker.join(15)
            assert worker.exitcode == 0
        assert len(tokens) == 2
        assert len(repository.pending_writes("a", "viking://resources/document")) == 2
        with pytest.raises(RuntimeError, match="in-flight"):
            repository.observe_draft("a", "viking://resources/document")
        first, second = tokens
        repository.finish_write(first)
        repository.uncertain_write(second)
        process.terminate()
        process.join(15)
        with pytest.raises(AuthorityUnavailable):
            repository.begin_write("a", "viking://resources/new")
        process = _start(setup)
        writes = repository.pending_writes("a", "viking://resources/document")
        assert [row["id"] for row in writes] == [second]
        assert writes[0]["state"] == "uncertain"
        with pytest.raises(RuntimeError, match="confirmed live"):
            repository.finish_write(second)
        assert repository.pending_writes("a", "viking://resources/new") == []
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(15)
        process.terminate()
        process.join(15)


def test_publication_roundtrip_and_process_exclusion(setup):
    process = _start(setup)
    repository = AuthorityRepository(setup[0], setup[2])
    try:
        with pytest.raises(RuntimeError, match="owned by an authority"):
            PublicationStore(setup[1])
        with pytest.raises(BlockingIOError):
            RegistryAuthority(setup[0] + "2", setup[1], setup[2])
        generation = repository.begin("a", "resources", "viking://resources/document", "plan", ["child"])
        repository.seal(generation)
        repository.acknowledge(generation, "child")
        assert repository.state(generation)["state"] == "published"
        lease, manifest = repository.capture("a")
        assert manifest["viking://resources/document"]["generation"] == generation
        repository.renew(lease)
        repository.release(lease)
        assert isinstance(repository.protected_content_revisions(), set)
        batch = repository.begin_batch("a", "resources")
        next_generation = repository.begin("a", "resources", "viking://resources/next", "plan", {"child"}, batch=batch)
        repository.seal(next_generation)
        repository.acknowledge(next_generation, "child")
        repository.seal_batch(batch)
        assert repository.batch_state(batch)["state"] == "published"
        with pytest.raises(RuntimeError, match="Direct SQL"):
            repository.db()
    finally:
        process.terminate()
        process.join(15)


def test_authentication_allowlist_and_private_files(setup):
    endpoint, database, token = setup
    with RegistryAuthority(*setup) as authority:
        thread = threading.Thread(target=authority.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            assert Path(endpoint).stat().st_mode & 0o777 == 0o600
            for secret, command in [("wrong", "begin_write"), ("x" * 64, "db"), ("x" * 64, "__class__")]:
                with socket.socket(socket.AF_UNIX) as channel:
                    channel.connect(endpoint)
                    channel.sendall(json.dumps({"token": secret, "command": command}).encode() + b"\n")
                    result = json.loads(channel.makefile("rb").readline())
                assert result["error"]
            assert authority.repository.pending_writes("a", "uri") == []
            Path(token).chmod(0o644)
            with pytest.raises(ValueError, match="private"):
                AuthorityRepository(endpoint, token)
        finally:
            authority.shutdown()
            thread.join(5)


def test_backup_is_consistent_and_restorable(setup, tmp_path):
    with RegistryAuthority(*setup) as authority:
        token = authority.repository.begin_write("a", "uri")
        backup = tmp_path / "backup.db"
        authority.backup(backup)
    restored = PublicationStore(str(backup))
    assert restored.pending_writes("a", "uri")[0]["id"] == token


def test_configured_store_selects_authority_without_local_fallback(setup):
    from types import SimpleNamespace
    embedding = SimpleNamespace(chunking=SimpleNamespace(
        publication_path="", publication_authority_socket=setup[0],
        publication_authority_token_file=setup[2],
    ))
    repository = configured_store(embedding)
    assert isinstance(repository, AuthorityRepository)
    with pytest.raises(AuthorityUnavailable):
        repository.begin_write("a", "uri")
    assert not Path(setup[1]).exists()


def test_authority_configuration_roundtrip_and_no_local_path(setup):
    from openviking_cli.utils.config.embedding_config import ResourceChunkingConfig
    config = ResourceChunkingConfig(
        enabled=True, tokenizer_path="tokenizer.json", tokenizer_sha256="a" * 64,
        publish_content=True, publication_authority_socket=setup[0],
        publication_authority_token_file=setup[2],
    )
    assert ResourceChunkingConfig.model_validate(config.model_dump()) == config
    with pytest.raises(ValueError, match="must not configure"):
        ResourceChunkingConfig.model_validate({**config.model_dump(), "publication_path": setup[1]})
    with pytest.raises(ValueError, match="configured together"):
        ResourceChunkingConfig.model_validate({**config.model_dump(), "publication_authority_token_file": ""})

# Reuse actual VikingFS/producer fixtures; only AGFS transport and vector backend
# are substitutes. The authority itself runs in a separate OS process.
from test_content_publication import environment as environment, stage as text_stage
from test_index_plan import embedding as embedding
from test_image_publication import image_env as image_env, stage as image_stage
from test_semantic_publication import directory as semantic_stage


async def test_real_producers_use_authority_with_stable_plan_identity(image_env, embedding, setup):
    e = image_env
    producers = [
        lambda: text_stage(e, "# Same text\n\ncontent"),
        lambda: semantic_stage(e, "same semantic"),
        lambda: image_stage(e, b"same image", "same summary"),
    ]
    local_plans = []
    for produce in producers:
        messages = await produce()
        local_plans.append([message.context_data["index_plan_id"] for message in messages])
    process = _start(setup)
    try:
        embedding.chunking.publication_path = ""
        embedding.chunking.publication_authority_socket = setup[0]
        embedding.chunking.publication_authority_token_file = setup[2]
        e.store = configured_store(embedding)
        for produce, expected in zip(producers, local_plans):
            messages = await produce()
            assert [message.context_data["index_plan_id"] for message in messages] == expected
            for message in messages:
                result = await e.handler.on_dequeue({"data": message.to_json()})
                assert result.outcome.value == "success", result.error
            assert e.store.state(messages[0].context_data["index_generation"])["state"] == "published"
        assert not e.store.pending_writes("acct", "viking://resources/manual/images/a.png")
    finally:
        process.terminate()
        process.join(15)
