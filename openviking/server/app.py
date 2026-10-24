# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""FastAPI application for OpenViking HTTP Server."""

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.exceptions import ExceptionMiddleware

from openviking.observability.http_error_context import capture_public_http_error
from openviking.server.config import (
    ServerConfig,
    load_bot_gateway_token,
    load_server_config,
    validate_server_config,
)
from openviking.server.dependencies import set_server_config, set_service
from openviking.server.error_mapping import map_exception
from openviking.server.identity import RequestContext, Role
from openviking.server.models import ERROR_CODE_TO_HTTP_STATUS, ErrorInfo, Response
from openviking.server.profile_middleware import create_profile_http_middleware
from openviking.server.request_id import REQUEST_ID_HEADER, RequestIdMiddleware
from openviking.server.routers import (
    acl_router,
    admin_router,
    wiki_router,
    namespaces_router,
    kb_jobs_router,
    write_recovery_router,
    agent_evolution_router,
    bot_router,
    compile_router,
    console_router,
    content_router,
    debug_router,
    filesystem_router,
    index_search_router,
    metrics_router,
    observer_router,
    openviking_assets_router,
    pack_router,
    privacy_configs_router,
    resources_router,
    search_router,
    sessions_router,
    skills_router,
    snapshot_router,
    stats_router,
    system_router,
    tasks_router,
    user_settings_router,
    vector_search_router,
    watches_router,
    webdav_router,
)
from openviking.service.core import OpenVikingService
from openviking.service.task_tracker import get_task_tracker
from openviking_cli.exceptions import OpenVikingError
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils import get_logger
from openviking_cli.utils.config import (
    DEFAULT_OV_CONF,
    OPENVIKING_CONFIG_ENV,
    get_openviking_config,
    resolve_config_path,
)
from openviking_cli.utils.logger import init_otel_log_handler_from_server_config

logger = get_logger(__name__)

WORKER_WITH_BOT_ENV = "OPENVIKING_WORKER_WITH_BOT"
WORKER_BOT_API_URL_ENV = "OPENVIKING_WORKER_BOT_API_URL"

# Upper bound on the directories a single startup re-fires for interrupted
# generations: the point is to resume the work, not to stampede the writer.
RESUME_INTERRUPTED_DIRECTORY_LIMIT = 20

# Retired generations examined per maintenance sweep when reclaiming orphan
# vectors. Each one costs a filtered query, so the janitor is bounded rather
# than one long pass that would hold the store. The grace period keeps a
# generation that a reader captured moments ago alive for an hour.
RETIRED_VECTOR_RECLAIM_GENERATIONS = int(
    os.environ.get("OPENVIKING_RETIRED_VECTOR_RECLAIM_GENERATIONS", "50")
)
RETIRED_VECTOR_GRACE_SECONDS = float(
    os.environ.get("OPENVIKING_RETIRED_VECTOR_GRACE_SECONDS", "3600")
)


def _configure_default_executor(config: ServerConfig) -> None:
    """Apply the configured asyncio default executor to the current worker loop.

    The event loop owns the executor after ``set_default_executor`` and shuts it
    down when the loop closes. This must run before service initialization,
    because initialization itself can submit work through ``asyncio.to_thread``.
    """
    max_workers = config.executor_threads
    if max_workers == 0:
        return

    executor = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="openviking-asyncio",
    )
    asyncio.get_running_loop().set_default_executor(executor)
    logger.info("Configured asyncio default executor: max_workers=%d", max_workers)


def create_worker_app() -> FastAPI:
    """Load file config and replay parent-process Bot CLI overrides."""
    resolved_config_path = resolve_config_path(
        None,
        OPENVIKING_CONFIG_ENV,
        DEFAULT_OV_CONF,
    )
    config_path = str(resolved_config_path) if resolved_config_path is not None else None
    config = load_server_config(config_path)
    with_bot = os.environ.get(WORKER_WITH_BOT_ENV)
    if with_bot is not None:
        config.with_bot = with_bot == "1"
    bot_api_url = os.environ.get(WORKER_BOT_API_URL_ENV)
    if bot_api_url is not None:
        config.bot_api_url = bot_api_url
    return create_app(config, config_path=config_path)


async def _initialize_auth_plugin(
    app: FastAPI,
    service: OpenVikingService,
    config: ServerConfig,
) -> None:
    """Initialize the auth plugin before the app serves authenticated requests."""
    from openviking.server.auth.registry import get_registry

    effective_auth_mode = config.get_effective_auth_mode()
    registry = get_registry()

    # Ensure built-in plugins are registered
    from openviking.server.auth.plugins import (
        ApiKeyAuthPlugin,
        DevAuthPlugin,
        TrustedAuthPlugin,
    )

    if registry.get("dev") is None:
        registry.register(DevAuthPlugin)
    if registry.get("api_key") is None:
        registry.register(ApiKeyAuthPlugin)
    if registry.get("trusted") is None:
        registry.register(TrustedAuthPlugin)

    plugin_cls = registry.get(effective_auth_mode)
    if plugin_cls is None:
        logger.error(
            "Unknown auth_mode: %r. No auth plugin registered. Registered modes: %s.",
            effective_auth_mode,
            ", ".join(registry.list_modes()),
        )
        raise RuntimeError(f"Unknown auth_mode: {effective_auth_mode}")

    plugin = plugin_cls()
    app.state.auth_plugin = plugin
    await plugin.initialize(app, service, config)
    logger.info("Auth plugin initialized: %s", effective_auth_mode)


async def _initialize_runtime_state(
    app: FastAPI,
    service: OpenVikingService,
    config: ServerConfig,
) -> None:
    """Initialize service and auth dependencies before traffic is accepted."""
    await service.initialize()
    await _initialize_auth_plugin(app, service, config)
    manager = app.state.api_key_manager
    if manager is not None:
        await service.load_acl_settings(
            [
                item["account_id"]
                for item in manager.get_accounts()
                if item["account_id"] != service.user.account_id
            ]
        )
    from openviking.service.deletion import setup_deletion

    app.state.deletion_service = await setup_deletion(
        service=service,
        manager=app.state.api_key_manager,
        oauth_store=getattr(app.state, "oauth_store", None),
        usage_audit_runtime=getattr(app.state, "usage_audit_runtime", None),
    )
    logger.info("OpenVikingService initialization complete")


def _format_error_location(loc: object) -> str:
    if not isinstance(loc, (list, tuple)):
        return "request"
    parts = [str(part) for part in loc if part is not None]
    return ".".join(parts) if parts else "request"


def _normalize_validation_error(error: object) -> dict:
    if not isinstance(error, dict):
        return {"loc": ["request"], "message": str(error), "type": "value_error"}
    loc = error.get("loc", ["request"])
    if not isinstance(loc, (list, tuple)):
        loc = [loc]
    return {
        "loc": [str(part) for part in loc],
        "message": str(error.get("msg") or "Invalid value"),
        "type": str(error.get("type") or "value_error"),
    }


def _validation_error_message(errors: list[dict]) -> str:
    if not errors:
        return "Invalid request parameters"
    first = errors[0]
    location = _format_error_location(first.get("loc"))
    message = first.get("message") or "Invalid value"
    return f"Invalid request parameters: {location}: {message}"


_FRAMEWORK_HTTP_STATUS_TO_ERROR_CODE = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "INVALID_ARGUMENT",
    429: "RESOURCE_EXHAUSTED",
    502: "UNAVAILABLE",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
}


def _error_code_from_framework_http_status(status_code: int) -> str:
    """Best-effort envelope code for framework/proxy HTTPException fallbacks.

    Business routes should raise OpenVikingError subclasses directly instead
    of relying on this status-code conversion.
    """
    if status_code in _FRAMEWORK_HTTP_STATUS_TO_ERROR_CODE:
        return _FRAMEWORK_HTTP_STATUS_TO_ERROR_CODE[status_code]
    return "INTERNAL" if status_code >= 500 else "UNKNOWN"


def _message_from_http_detail(detail: object) -> str:
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, list):
        errors = [_normalize_validation_error(item) for item in detail]
        return _validation_error_message(errors)
    if isinstance(detail, dict):
        for key in ("message", "detail", "error"):
            value = detail.get(key)
            if isinstance(value, str) and value:
                return value
    if detail:
        return str(detail)
    return "HTTP request failed"


def create_app(
    config: Optional[ServerConfig] = None,
    service: Optional[OpenVikingService] = None,
    config_path: Optional[str] = None,
) -> FastAPI:
    """Create FastAPI application.

    Args:
        config: Server configuration. If None, loads from default location.
        service: Pre-initialized OpenVikingService (optional).
        config_path: Resolved ov.conf path used for live configuration reload.

    Returns:
        FastAPI application instance
    """
    resolved_config_path = (
        resolve_config_path(config_path, OPENVIKING_CONFIG_ENV, DEFAULT_OV_CONF)
        if config_path is not None or config is None
        else None
    )
    if config is None:
        config = load_server_config(
            str(resolved_config_path) if resolved_config_path is not None else config_path
        )

    validate_server_config(config)

    usage_reporter_unset = object()
    usage_reporter = usage_reporter_unset

    def _get_usage_reporter():  # noqa: ANN202
        nonlocal usage_reporter
        if usage_reporter is usage_reporter_unset:
            from openviking.usage_reporter.config import build_usage_reporter

            usage_reporter = build_usage_reporter(config.usage_reporter)
        return usage_reporter

    def _configure_session_runtime(service_obj) -> None:  # noqa: ANN001
        sessions = getattr(service_obj, "sessions", None)
        tool_output_setter = getattr(sessions, "set_tool_output_externalization_config", None)
        if callable(tool_output_setter):
            tool_output_setter(config.tool_output_externalization)

        usage_reporter_setter = getattr(sessions, "set_usage_reporter", None)
        if callable(usage_reporter_setter):
            usage_reporter_setter(_get_usage_reporter())

        agent_evolution_setter = getattr(sessions, "set_agent_evolution_config", None)
        if callable(agent_evolution_setter):
            agent_evolution_setter(config.agent_evolution)

        user_memory_policy_setter = getattr(
            sessions,
            "set_default_user_memory_policy",
            None,
        )
        if callable(user_memory_policy_setter):
            user_memory_policy_setter(config.user_config_defaults.memory_policy)

        agent_evolution_path_setter = getattr(
            sessions,
            "set_agent_evolution_config_path",
            None,
        )
        if callable(agent_evolution_path_setter):
            agent_evolution_path_setter(
                str(resolved_config_path) if resolved_config_path is not None else None
            )

    if service is not None:
        _configure_session_runtime(service)

    bot_gateway_token = load_bot_gateway_token() if config.with_bot else ""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Application lifespan handler."""
        nonlocal service
        _configure_default_executor(config)
        owns_service = service is None
        if owns_service:
            service = OpenVikingService()

        assert service is not None
        if config.with_bot:
            service.compile.configure_local_backend(config.bot_api_url, bot_gateway_token)
        _configure_session_runtime(service)
        set_service(service)

        from openviking.metrics.global_api import (
            init_metrics_from_server_config,
        )
        from openviking.observability.usage_audit import init_usage_audit_from_server_config

        init_metrics_from_server_config(config, app=app, service=service)
        if config.observability.metrics.enabled:
            logger.info("Prometheus metrics enabled at /metrics")
        await init_usage_audit_from_server_config(config, app=app, service=service)

        # Initialize OAuth 2.1 store + provider when enabled in OpenViking config.
        # The store + provider instances were already constructed at app
        # creation time so the SDK routes could capture them; here we just
        # async-initialize the SQLite connection on the same instance.
        oauth_store = getattr(app.state, "oauth_store", None)
        oauth_gc_task: Optional[asyncio.Task] = None
        if oauth_store is not None:
            await oauth_store.initialize()

            async def _oauth_gc_loop(store) -> None:  # noqa: ANN001
                while True:
                    try:
                        await asyncio.sleep(60)
                        await store.gc_expired()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        logger.warning("OAuth GC loop error: %s", e)

            oauth_gc_task = asyncio.create_task(_oauth_gc_loop(oauth_store))
            app.state.oauth_gc_task = oauth_gc_task
            logger.info("OAuth 2.1 store initialized at %s", oauth_store._db_path)

        # Start TaskTracker cleanup loop
        task_tracker = get_task_tracker()
        task_tracker.start_cleanup_loop()

        # A producer killed mid-batch (restart, cancelled job, dead client)
        # never reaches ``seal_batch``, and nothing else re-checks its batch, so
        # every chapter it had already finished stayed invisible to retrieval.
        # Seal the ones whose receipts are complete before serving traffic.
        from openviking.indexing.publication import configured_store

        registry = configured_store()
        recover = getattr(registry, "recover_stranded_batches", None)
        prune = getattr(registry, "prune_snapshots", None)
        if recover is not None:
            try:
                sealed = await asyncio.to_thread(recover)
                if sealed:
                    logger.info(
                        "Recovered %d stranded publication batch(es) at startup", sealed
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("Stranded publication batch recovery failed: %s", e)

            async def _stranded_batch_sweep() -> None:
                # A producer can also die without a restart (cancelled job,
                # killed client).  The idle window keeps this from racing a live
                # producer that is between its last receipt and seal_batch.
                while True:
                    try:
                        # 15s, not 60s: a publish writes a full manifest snapshot
                        # and the sweep is the only thing that reclaims the one it
                        # replaced. At 60s the sweep was outrun by the publish rate
                        # and the table only grew (measured 51 publishes/min
                        # against a sweep that reclaimed at most 500 rows per
                        # minute, most of them ineligible on age).
                        await asyncio.sleep(15)
                        swept = await asyncio.to_thread(recover, 200, 60.0)
                        if swept:
                            logger.info(
                                "Sealed %d stranded publication batch(es)", swept
                            )
                        if prune is not None:
                            # Each publish writes a whole *complete* manifest
                            # snapshot (~28MB gzipped on a large account) and only
                            # the head of each account can read one, so every
                            # publish leaves a large unreachable row behind.
                            # Measured: 70 publishes/min, i.e. ~116GB/hour of
                            # manifest growth -- far past the old 64-rows-per-
                            # minute, 1-hour-old sweep, which could never catch
                            # up and let the store reach 36GB with 96% dead pages.
                            # The age floor is only a safety margin on top of the
                            # explicit lease check `prune_snapshots` already does
                            # (it skips every snapshot a live query lease, gc
                            # lease or recovery record pins). The TTL is 120s and
                            # leases renew every 40s, so 240s is two TTLs -- ample
                            # -- and a 600s floor simply parked every snapshot for
                            # ten minutes while the table kept growing.
                            pruned_total = 0
                            for _ in range(20):
                                dropped = await asyncio.to_thread(prune, 240.0, 500)
                                pruned_total += dropped or 0
                                if not dropped:
                                    break
                            if pruned_total:
                                # debug, not info: this fires on every sweep
                                # while a reindex runs (one snapshot per publish).
                                # It was logged at info and the deployment runs at
                                # WARNING, so a store that had grown to 36GB of
                                # dead manifests reclaimed nothing visibly -- the
                                # log made it look like the sweep never ran.
                                logger.debug(
                                    "Pruned %d unreachable manifest snapshot(s)",
                                    pruned_total,
                                )
                        windows = getattr(registry, "prune_generation_write_windows", None)
                        if windows is not None:
                            # One row per managed vector write and nothing ever
                            # deleted them: 1.19M rows / 545MB of a 37GB store.
                            dropped = await asyncio.to_thread(windows, 86400.0, 20000)
                            if dropped:
                                logger.info(
                                    "Pruned %d confirmed generation write window(s)", dropped
                                )
                        lexical = getattr(registry, "prune_superseded_lexical", None)
                        if lexical is not None:
                            # Immutable per generation, so every reindex wrote a
                            # fresh copy of the text: 717k rows / 9.1GB of a
                            # 13.6GB store, and the store only grew.
                            dropped = await asyncio.to_thread(lexical, 86400.0, 2000)
                            if dropped:
                                logger.info(
                                    "Pruned %d superseded lexical row(s)", dropped
                                )
                        settled = getattr(registry, "prune_settled_builds", None)
                        if settled is not None:
                            # Append-only history: 653,922 rows / 337MB, and only
                            # the newest row per URI is ever read.
                            dropped = await asyncio.to_thread(settled, 604800.0, 20000)
                            if dropped:
                                logger.info("Pruned %d settled build row(s)", dropped)
                        await _reclaim_retired_vectors()
                        checkpoint = getattr(registry, "checkpoint_wal", None)
                        if checkpoint is not None:
                            # An unchecked WAL reached 2.5GB and every write had
                            # to append to it; keep it truncated between bursts.
                            await asyncio.to_thread(checkpoint, "TRUNCATE")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Stranded publication batch sweep failed: %s", e)

            async def _reclaim_retired_vectors() -> None:
                """Delete vectors whose generation nothing can reach any more.

                Every reindex writes a *new* generation's records (the record id
                embeds the generation), so the previous copy becomes an orphan
                that no code ever removed: live, 957,757 of 1,303,573 vectors
                (73.5%) belonged to retired generations, 78 GB of store. Worse,
                retrieval never filters by generation, so those stale copies were
                still searchable and competed with the current record of the same
                URI in every result set.

                Bounded per sweep and off the request path: the janitor catches
                up over time instead of holding the store for one long pass.
                """
                store = getattr(service, "vikingdb_manager", None)
                scan = getattr(registry, "retired_generations", None)
                if store is None or scan is None:
                    return
                retired = await asyncio.to_thread(
                    scan, RETIRED_VECTOR_GRACE_SECONDS, RETIRED_VECTOR_RECLAIM_GENERATIONS
                )
                if not retired:
                    return
                reclaimed = 0
                for row in retired:
                    ctx = RequestContext(
                        user=UserIdentifier(row["account"], "system"),
                        role=Role.ROOT,
                        bypass_acl=True,
                    )
                    ids: list[str] = []
                    offset = 0
                    while True:
                        page = await store.query(
                            filter={
                                "op": "must",
                                "field": "index_generation",
                                "conds": [row["generation"]],
                            },
                            limit=1000,
                            offset=offset,
                            output_fields=["id"],
                            ctx=ctx,
                        )
                        ids.extend(str(item["id"]) for item in page if item.get("id"))
                        if len(page) < 1000:
                            break
                        offset += len(page)
                    if ids:
                        reclaimed += await store.delete(ids, ctx=ctx)
                if reclaimed:
                    logger.info(
                        "Reclaimed %d vector(s) from %d retired generation(s)",
                        reclaimed,
                        len(retired),
                    )

            app.state.stranded_batch_task = asyncio.create_task(_stranded_batch_sweep())

        # A restart orphans whatever was in flight: those generations stay
        # 'failed' with an interrupt error and nothing retries them, so a tree
        # can sit half-indexed until an operator notices (live: one interrupted
        # run left 14,688 URIs for 40 hours and needed a hand-written re-fire).
        # Re-fire the affected directories through the ordinary reindex path,
        # bounded and sequential so a big interruption cannot stampede the
        # writer. The pass runs off the request path.
        interrupted = getattr(registry, "interrupted_uris", None)
        if interrupted is not None:

            async def _resume_interrupted_generations() -> None:
                try:
                    rows = await asyncio.to_thread(interrupted, 2000)
                    if not rows:
                        return
                    targets = list(
                        dict.fromkeys(
                            (row["account"], row["uri"].rsplit("/", 1)[0]) for row in rows
                        )
                    )[:RESUME_INTERRUPTED_DIRECTORY_LIMIT]
                    logger.warning(
                        "Resuming %d interrupted generation(s): re-firing %d "
                        "director(ies), newest %s",
                        len(rows),
                        len(targets),
                        targets[0][1],
                    )
                    fired = 0
                    for account, parent in targets:
                        ctx = RequestContext(
                            user=UserIdentifier(account, "system"),
                            role=Role.ROOT,
                            bypass_acl=True,
                        )
                        try:
                            await service.reindex(
                                uri=parent,
                                mode="vectors_only",
                                recursive=True,
                                wait=False,
                                ctx=ctx,
                            )
                            fired += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Resume reindex failed for %s: %s", parent, exc)
                    logger.warning(
                        "Interrupted-generation resume fired %d/%d director(ies)",
                        fired,
                        len(targets),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.warning("Interrupted-generation resume failed: %s", e)

            app.state.resume_interrupted_task = asyncio.create_task(
                _resume_interrupted_generations()
            )

        # Lexical search ranks against a per-account FTS mirror of the published
        # lexical rows. Databases written before the mirror existed, and mirrors a
        # crash left half-built, are filled in here, off the request path, so an
        # upgrade needs no manual migration step and search stops paying for the
        # brute-force scan of the whole account scope.
        rebuild_lexical = getattr(registry, "rebuild_lexical_index", None)
        if rebuild_lexical is not None:

            async def _lexical_mirror_backfill() -> None:
                try:
                    built = await asyncio.to_thread(rebuild_lexical)
                    if built:
                        logger.info("Built %d lexical FTS mirror(s): %s", len(built), built)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.warning("Lexical FTS mirror backfill failed: %s", e)

            app.state.lexical_mirror_task = asyncio.create_task(_lexical_mirror_backfill())

        # Optional diagnostics: dump stacks when the event loop stops responding.
        from openviking.utils.loop_watchdog import start_loop_watchdog

        if start_loop_watchdog():
            logger.info("Event-loop stall watchdog enabled")

        # Initialize tracing and OTLP log export from server.observability.
        from openviking.telemetry import tracer_module

        tracer_module.init_tracer_from_server_config(config)
        init_otel_log_handler_from_server_config(config)

        # Start MCP session manager (must be active before /mcp requests)
        from openviking.server.mcp_endpoint import mcp_lifespan

        async with mcp_lifespan():
            if service is not None:
                await _initialize_runtime_state(app, service, config)
            from openviking.service.kb_job_runtime import (
                start_document_job_worker, stop_document_job_worker,
            )

            kb_job_task = start_document_job_worker(app, service, config)
            try:
                yield
            finally:
                await stop_document_job_worker(kb_job_task)

        # Cleanup
        from openviking.metrics.global_api import shutdown_metrics_async
        from openviking.observability.usage_audit import shutdown_usage_audit

        await shutdown_usage_audit(app=app)
        await shutdown_metrics_async(app=app)
        task_tracker.stop_cleanup_loop()
        auth_plugin_state = getattr(app.state, "auth_plugin", None)
        if auth_plugin_state is not None:
            try:
                await auth_plugin_state.shutdown()
            except Exception as e:  # noqa: BLE001
                logger.warning("Auth plugin shutdown failed: %s", e)
        if oauth_gc_task is not None:
            oauth_gc_task.cancel()
            try:
                await oauth_gc_task
            except (asyncio.CancelledError, Exception):
                pass
        oauth_store_state = getattr(app.state, "oauth_store", None)
        if oauth_store_state is not None:
            try:
                await oauth_store_state.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("OAuth store close failed: %s", e)
        if owns_service and service:
            try:
                await service.close()
                logger.info("OpenVikingService closed")
            except asyncio.CancelledError as e:
                logger.warning(f"OpenVikingService close cancelled during shutdown: {e}")
            except Exception as e:
                logger.warning(f"OpenVikingService close failed during shutdown: {e}")
        if usage_reporter is not usage_reporter_unset and usage_reporter is not None:
            await usage_reporter.close()

    app = FastAPI(
        title="OpenViking API",
        description="OpenViking HTTP Server - Agent-native context database",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.config = config
    app.state.api_key_manager = None
    app.state.deletion_service = None
    set_server_config(config)

    # Body dump middleware must be registered BEFORE observability so it ends up
    # nested inside the trace span (in Starlette, middleware added later wraps
    # earlier-added ones — so earlier registration = inner layer).
    if config.observability.dump_body.enabled:
        from openviking.server.body_dump_middleware import (
            create_dump_http_body_middleware,
        )

        _dump_body_fn = create_dump_http_body_middleware(
            max_bytes=config.observability.dump_body.max_bytes,
        )

        @app.middleware("http")
        async def dump_http_body(request: Request, call_next: Callable):
            return await _dump_body_fn(request, call_next)

        logger.info(
            "HTTP body dump middleware enabled (max_bytes=%d) — bodies will be "
            "attached to trace spans. Disable in production via "
            "server.observability.dump_body.enabled=false.",
            config.observability.dump_body.max_bytes,
        )

    # Add HTTP observability middleware (metrics, tracing).
    # Note: In FastAPI/Starlette, middleware added later executes first (outer layer).
    # We want timing to be the outermost layer to measure the full request duration.
    from openviking.observability.http_observability_middleware import (
        create_http_observability_middleware,
    )

    http_observability_middleware = create_http_observability_middleware()
    profile_http_middleware = create_profile_http_middleware()

    @app.middleware("http")
    async def add_http_observability(request: Request, call_next: Callable):
        return await http_observability_middleware(request, call_next)

    @app.middleware("http")
    async def add_profile_output(request: Request, call_next: Callable):
        return await profile_http_middleware(request, call_next)

    # Add request timing middleware last (so it executes first as the outermost layer)
    # This ensures X-Process-Time includes the full request duration including
    # observability middleware overhead.
    # Add request header logging middleware (for debug)
    @app.middleware("http")
    async def log_request_headers(request: Request, call_next: Callable):
        access_logger = logging.getLogger("uvicorn.access")
        if access_logger.isEnabledFor(logging.DEBUG):
            headers = dict(request.headers)
            header_names = ", ".join(sorted(headers.keys()))
            access_logger.debug(
                f"Request headers for {request.method} {request.url.path}: {header_names}"
            )
        response = await call_next(request)
        return response

    # Add request timing middleware
    @app.middleware("http")
    async def add_timing(request: Request, call_next: Callable):
        """
        Middleware to measure request processing time.

        This middleware is added last so it executes as the outermost layer,
        ensuring X-Process-Time includes the full request duration including
        all other middleware overhead.

        Args:
            request: The incoming HTTP request.
            call_next: The next middleware/handler in the chain.

        Returns:
            The response with X-Process-Time header added.
        """
        start_time = time.perf_counter()
        response = await call_next(request)
        process_time = time.perf_counter() - start_time
        response.headers["X-Process-Time"] = str(process_time)
        return response

    # Add exception handler for OpenVikingError
    @app.exception_handler(OpenVikingError)
    async def openviking_error_handler(request: Request, exc: OpenVikingError):
        http_status = ERROR_CODE_TO_HTTP_STATUS.get(exc.code, 500)
        capture_public_http_error(code=exc.code, message=exc.message, details=exc.details)
        return JSONResponse(
            status_code=http_status,
            content=Response(
                status="error",
                error=ErrorInfo(
                    code=exc.code,
                    message=exc.message,
                    details=exc.details,
                ),
            ).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(request: Request, exc: RequestValidationError):
        errors = [_normalize_validation_error(error) for error in exc.errors()]
        code = "INVALID_ARGUMENT"
        message = _validation_error_message(errors)
        details = {"validation_errors": errors}
        capture_public_http_error(
            code=code,
            message=message,
            details=details,
        )
        return JSONResponse(
            status_code=ERROR_CODE_TO_HTTP_STATUS[code],
            content=Response(
                status="error",
                error=ErrorInfo(
                    code=code,
                    message=message,
                    details=details,
                ),
            ).model_dump(exclude_none=True),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        code = _error_code_from_framework_http_status(exc.status_code)
        response_status = exc.status_code
        if code != "UNKNOWN":
            response_status = ERROR_CODE_TO_HTTP_STATUS.get(code, exc.status_code)
        details = None
        if exc.status_code != response_status:
            details = {"original_http_status_code": exc.status_code}
        message = _message_from_http_detail(exc.detail)
        capture_public_http_error(code=code, message=message, details=details)
        return JSONResponse(
            status_code=response_status,
            headers=exc.headers,
            content=Response(
                status="error",
                error=ErrorInfo(
                    code=code,
                    message=message,
                    details=details,
                ),
            ).model_dump(exclude_none=True),
        )

    # Catch-all for unhandled exceptions so clients always get JSON
    async def general_error_handler(_request: Request, exc: Exception):
        mapped = map_exception(exc)
        if mapped is not None:
            http_status = ERROR_CODE_TO_HTTP_STATUS.get(mapped.code, 500)
            logger.warning(
                "Mapped unhandled exception to structured API error",
                extra={"error_code": mapped.code, "error_message": mapped.message},
                exc_info=exc,
            )
            return JSONResponse(
                status_code=http_status,
                content=Response(
                    status="error",
                    error=ErrorInfo(
                        code=mapped.code,
                        message=mapped.message,
                        details=mapped.details,
                    ),
                ).model_dump(),
            )

        logger.exception("Unhandled exception")
        return JSONResponse(
            status_code=500,
            content=Response(
                status="error",
                error=ErrorInfo(
                    code="INTERNAL",
                    message="Internal server error",
                ),
            ).model_dump(),
        )

    # Keep exception rendering inside the request-ID and CORS layers. This lets
    # those middleware own their response headers without special error paths.
    app.add_middleware(ExceptionMiddleware, handlers={Exception: general_error_handler})
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER],
    )

    # Configure Bot API if --with-bot is enabled
    if config.with_bot:
        import openviking.server.routers.bot as bot_module

        bot_module.set_bot_api_url(config.bot_api_url)
        bot_module.set_bot_api_key(bot_gateway_token)
        logger.info(f"Bot API proxy enabled, forwarding to {config.bot_api_url}")
    else:
        logger.info("Bot API proxy disabled (use --with-bot to enable)")

    # Register routers
    app.include_router(system_router)
    app.include_router(acl_router)
    app.include_router(admin_router)
    app.include_router(wiki_router)
    app.include_router(namespaces_router)
    app.include_router(kb_jobs_router)
    app.include_router(write_recovery_router)
    app.include_router(agent_evolution_router)
    app.include_router(compile_router)
    app.include_router(resources_router)
    app.include_router(filesystem_router)
    app.include_router(content_router)
    app.include_router(console_router)
    app.include_router(search_router)
    app.include_router(index_search_router)
    app.include_router(vector_search_router)
    app.include_router(privacy_configs_router)
    app.include_router(skills_router)
    app.include_router(sessions_router)
    app.include_router(snapshot_router)
    app.include_router(stats_router)
    app.include_router(pack_router)
    app.include_router(debug_router)
    app.include_router(observer_router)
    app.include_router(openviking_assets_router)
    app.include_router(metrics_router)
    app.include_router(tasks_router)
    app.include_router(user_settings_router)
    app.include_router(watches_router)
    app.include_router(webdav_router)
    app.include_router(bot_router, prefix="/bot/v1")

    # OAuth 2.1: when enabled, mount the official MCP SDK auth routes
    # (DCR / authorize / token / metadata) plus our authorize page + consent /
    # verify endpoints. The Provider that backs the SDK routes is built
    # in the lifespan; here we only register the route handlers, since the
    # SDK routes inspect request.app.state at call time.
    try:
        ov_cfg = get_openviking_config()
        if ov_cfg.oauth.enabled:
            from mcp.server.auth.routes import create_auth_routes
            from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
            from pydantic import AnyHttpUrl

            from openviking.server.oauth.router import router as oauth_router

            # Custom routes (authorize page + consent / verify endpoints).
            app.include_router(oauth_router)

            # SDK-owned routes (DCR / authorize / token / metadata / revoke).
            # We need a live Provider here; create_auth_routes captures it by
            # reference. Re-build the same construction the lifespan does so
            # the routes work as soon as they're hit (lifespan re-binds the
            # same instance to app.state for the consent / authorize-page path).
            from pathlib import Path as _Path

            from openviking.server.oauth.provider import OpenVikingOAuthProvider
            from openviking.server.oauth.storage import OAuthStore

            _workspace = _Path(ov_cfg.storage.workspace).expanduser().resolve()
            _workspace.mkdir(parents=True, exist_ok=True)
            _route_store = OAuthStore(_workspace / ov_cfg.oauth.db_filename)
            # Resolution order for the AS issuer URL:
            #   1. OPENVIKING_PUBLIC_BASE_URL env var (deployment override)
            #   2. oauth.issuer in ov.conf (operator config)
            #   3. http://127.0.0.1:1933 (dev default; SDK accepts loopback http)
            import os as _os

            _route_issuer = (
                _os.environ.get("OPENVIKING_PUBLIC_BASE_URL", "").strip().rstrip("/")
                or ov_cfg.oauth.issuer
                or "http://127.0.0.1:1933"
            )

            # Late-binding role resolver: app.state.api_key_manager is wired
            # during lifespan, after the provider is constructed. Lambda
            # closes over `app` and looks up at call time.
            def _current_role(account_id: str, user_id: str) -> Role:
                mgr = getattr(app.state, "api_key_manager", None)
                if mgr is None or not hasattr(mgr, "get_user_role"):
                    return Role.USER
                return mgr.get_user_role(account_id, user_id)

            _route_provider = OpenVikingOAuthProvider(
                store=_route_store,
                issuer=_route_issuer,
                access_token_ttl_seconds=ov_cfg.oauth.access_token_ttl_seconds,
                refresh_token_ttl_seconds=ov_cfg.oauth.refresh_token_ttl_seconds,
                auth_code_ttl_seconds=ov_cfg.oauth.auth_code_ttl_seconds,
                role_resolver=_current_role,
            )
            # Stash the route-time instances; the lifespan replaces these with
            # initialized copies before the first request lands.
            app.state.oauth_store = _route_store
            app.state.oauth_provider = _route_provider

            from openviking.server.oauth.provider import MCP_SCOPE

            sdk_routes = create_auth_routes(
                provider=_route_provider,
                issuer_url=AnyHttpUrl(_route_issuer),
                # default_scopes covers clients whose DCR omits `scope`
                # (ChatGPT does): without it they register scope-less and then
                # fail /authorize with invalid_scope when they request the
                # "mcp" scope advertised in the PRM document. valid_scopes is
                # deliberately NOT set — the SDK would 400 any DCR that carries
                # a scope outside the list, and clients like Claude register
                # with their own scope strings.
                client_registration_options=ClientRegistrationOptions(
                    enabled=True, default_scopes=[MCP_SCOPE]
                ),
                revocation_options=RevocationOptions(enabled=True),
            )
            app.routes.extend(sdk_routes)
            app.state.oauth_config = ov_cfg.oauth
            logger.info(
                "OAuth 2.1 routes mounted (SDK + authorize-page + consent): %s",
                [r.path for r in sdk_routes],
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("Skipping OAuth router registration: %s", e)

    # Favicon routes — always registered so /favicon.* and /mcp/favicon.* never
    # 404, even when web-studio isn't bundled. Source files live in
    # openviking/server/static/ (shipped via package-data, ~30KB total) so they
    # are available in every pip-install / docker / source-tree scenario.
    _server_static_dir = Path(__file__).resolve().parent / "static"
    _favicon_headers = {"Cache-Control": "public, max-age=86400"}
    _favicon_files = {
        "/favicon.ico": ("favicon.ico", "image/x-icon"),
        "/favicon.png": ("favicon-32.png", "image/png"),
        "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
        "/mcp/favicon.ico": ("favicon.ico", "image/x-icon"),
        "/mcp/favicon.png": ("favicon-32.png", "image/png"),
        "/mcp/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    }

    def _make_favicon_handler(filename: str, media_type: str):
        path = _server_static_dir / filename

        async def _handler():
            return FileResponse(path, media_type=media_type, headers=_favicon_headers)

        return _handler

    for _route, (_fname, _mime) in _favicon_files.items():
        app.add_api_route(_route, _make_favicon_handler(_fname, _mime), include_in_schema=False)

    # Web Studio SPA: serve the static bundle when present so the same OV
    # server origin can host the new frontend at /studio. The directory is
    # populated by the docker `web-studio-builder` stage and shipped inside
    # the openviking python package (see pyproject.toml package-data). Outside
    # docker, set OPENVIKING_WEB_STUDIO_DIR to a local `web-studio/dist` to
    # enable a dev build without rebuilding the wheel.
    _studio_env = os.environ.get("OPENVIKING_WEB_STUDIO_DIR", "").strip()
    if _studio_env:
        _studio_dir = Path(_studio_env)
    else:
        _studio_dir = Path(__file__).resolve().parent.parent / "web_studio" / "dist"

    if _studio_dir.is_dir() and (_studio_dir / "index.html").is_file():
        _studio_root = _studio_dir.resolve()
        _studio_index = _studio_root / "index.html"
        _studio_no_store = {"Cache-Control": "no-store"}

        def _studio_response(path: Path, *, no_store: bool = False) -> FileResponse:
            return FileResponse(path, headers=_studio_no_store if no_store else None)

        @app.get("/", include_in_schema=False)
        async def _root_redirect_to_studio():
            # When web-studio is bundled, treat / as a convenience entry to
            # /studio/ so users hitting the bare origin land on the UI.
            return RedirectResponse(url="/studio/", status_code=302)

        @app.get("/studio", include_in_schema=False)
        async def _studio_root_handler():
            return _studio_response(_studio_index, no_store=True)

        @app.get("/studio/{path:path}", include_in_schema=False)
        async def _studio_assets(path: str):
            # SPA fallback: serve real files when present, otherwise return
            # index.html so TanStack Router can resolve the deep link.
            try:
                requested = (_studio_root / path).resolve()
            except OSError:
                return _studio_response(_studio_index, no_store=True)

            if not requested.is_relative_to(_studio_root):
                return _studio_response(_studio_index, no_store=True)

            if requested.is_file():
                return _studio_response(requested)
            return _studio_response(_studio_index, no_store=True)

        logger.info("Web Studio mounted at /studio from %s", _studio_root)
    else:
        logger.info("Web Studio bundle not found at %s; skipping /studio mount", _studio_dir)

    # MCP endpoint — serves 17 tools (find, search, read, write, edit,
    # list, tree, remember, add_resource, list_watches, cancel_watch, grep,
    # glob, index_search, vector_search, forget, health) via streamable HTTP
    # for MCP clients.
    from starlette.routing import Match, Route

    from openviking.server.mcp_endpoint import create_mcp_app

    class _ScopedRoute(Route):
        """Expose the selected route through ``scope["route"]``, matching
        ``APIRoute.matches``, so outer observability can resolve the static
        ``/mcp`` route template."""

        def matches(self, scope):
            match, child_scope = super().matches(scope)
            if match != Match.NONE:
                child_scope["route"] = self
            return match, child_scope

    app.routes.append(
        _ScopedRoute("/mcp", endpoint=create_mcp_app(), methods=["GET", "POST", "DELETE"])
    )

    return app
