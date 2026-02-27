from __future__ import annotations

import functools
from contextvars import ContextVar
from typing import Any

from celery import Celery
from celery.signals import task_failure, task_postrun, task_prerun

from wireup._decorators import inject_from_container
from wireup.ioc.container.sync_container import ScopedSyncContainer, SyncContainer

_scope_ctx: ContextVar = ContextVar("wireup_celery_scope_ctx")
_scope: ContextVar[ScopedSyncContainer] = ContextVar("wireup_celery_scope")
_scope_exc: ContextVar[BaseException | None] = ContextVar("wireup_celery_scope_exc")


def setup(container: SyncContainer, app: Celery) -> None:
    """Integrate Wireup with Celery.

    Setup performs the following:
    * Injects dependencies into Celery tasks.
    * Creates a new container scope for each task execution,
      with a scoped lifetime matching the task duration.

    Scope lifecycle is managed via Celery signals:
    * ``task_prerun``: opens a new scoped container.
    * ``task_failure``: captures the exception for proper cleanup.
    * ``task_postrun``: closes the scoped container, propagating any exception
      to generator-based scoped dependencies for cleanup.
    """

    @task_prerun.connect(weak=False)
    def _on_task_prerun(sender: Any, **kwargs: Any) -> None:
        ctx = container.enter_scope()
        scoped = ctx.__enter__()
        _scope_ctx.set(ctx)
        _scope.set(scoped)
        _scope_exc.set(None)

    @task_failure.connect(weak=False)
    def _on_task_failure(sender: Any, exception: BaseException, **kwargs: Any) -> None:
        _scope_exc.set(exception)

    @task_postrun.connect(weak=False)
    def _on_task_postrun(sender: Any, retval: Any = None, state: str | None = None, **kwargs: Any) -> None:
        ctx = _scope_ctx.get(None)
        if ctx is None:
            return

        exc = _scope_exc.get(None)

        # When task_failure did not fire (e.g. task_eager_propagates=True),
        # fall back to checking the postrun state and retval.
        if exc is None and state == "FAILURE" and isinstance(retval, BaseException):
            exc = retval

        ctx.__exit__(type(exc) if exc else None, exc, exc.__traceback__ if exc else None)

    _inject_tasks(container, app)
    app.wireup_container = container  # type: ignore[reportAttributeAccessIssue]


def _inject_tasks(container: SyncContainer, app: Celery) -> None:
    inject_scoped = inject_from_container(container, get_request_container, hide_annotated_names=True)

    for task_name in list(app.tasks):
        task = app.tasks[task_name]
        # Skip built-in celery tasks (e.g. celery.backend_cleanup)
        if task_name.startswith("celery."):
            continue
        if hasattr(task, "run"):
            wrapped = inject_scoped(task.run)
            # inject_from_container returns the original function unchanged
            # when there are no wireup-annotated parameters to inject.
            if wrapped is task.run:
                continue
            task.run = wrapped
            # Celery uses __header__ for argument validation at apply_async time
            # (before the task runs). We must update it to match the wrapped
            # signature (wireup params hidden) but without performing injection,
            # since no scoped container exists at validation time.
            task.__header__ = _make_header_stub(wrapped)


def _make_header_stub(wrapped: Any) -> Any:
    """Create a no-op function with the same signature as ``wrapped``.

    Celery calls ``task.__header__(*args, **kwargs)`` at ``apply_async`` time
    to validate that the provided arguments match the task signature.  The stub
    accepts the same (hidden) parameters as the injection wrapper but does not
    perform any injection—it only exists so Celery's type-checking passes.
    """

    @functools.wraps(wrapped)
    def _stub(*args: Any, **kwargs: Any) -> None:  # noqa: ARG001
        pass

    return _stub


def get_app_container(app: Celery) -> SyncContainer:
    """Return the container associated with the given application."""
    return app.wireup_container  # type: ignore[reportAttributeAccessIssue]


def get_request_container() -> ScopedSyncContainer:
    """Return the scoped container for the currently executing task."""
    return _scope.get()
