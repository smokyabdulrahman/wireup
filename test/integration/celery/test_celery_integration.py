from typing import Iterator, NewType
from unittest.mock import MagicMock

import pytest
import wireup
import wireup.integration.celery
from celery import Celery, _state
from celery.signals import task_failure, task_postrun, task_prerun
from typing_extensions import Annotated
from wireup import Inject
from wireup._annotations import Injected, injectable
from wireup.integration.celery import get_app_container

from test.shared import shared_services
from test.shared.shared_services.rand import RandomService
from test.shared.shared_services.scoped import ScopedService, ScopedServiceDependency


@pytest.fixture(autouse=True)
def _clean_signals():
    """Disconnect all wireup signal handlers and reset Celery global state between tests.

    Celery signals are global, so handlers registered by ``setup()``
    persist across tests unless explicitly disconnected.  Additionally,
    task definitions leak via ``_state._on_app_finalizers``.
    """
    pre = list(task_prerun.receivers)
    post = list(task_postrun.receivers)
    fail = list(task_failure.receivers)
    finalizers = set(_state._on_app_finalizers)
    yield
    task_prerun.receivers[:] = pre
    task_postrun.receivers[:] = post
    task_failure.receivers[:] = fail
    task_prerun.sender_receivers_cache.clear()
    task_postrun.sender_receivers_cache.clear()
    task_failure.sender_receivers_cache.clear()
    _state._on_app_finalizers = finalizers


def _create_celery_app() -> Celery:
    app = Celery("wireup_test")
    app.config_from_object(
        {
            "task_always_eager": True,
            "task_eager_propagates": True,
        }
    )
    return app


def test_injects_singleton_dependency() -> None:
    app = _create_celery_app()

    @app.task
    def get_random(random: Injected[RandomService]):
        return random.get_random()

    container = wireup.create_sync_container(injectables=[shared_services])
    wireup.integration.celery.setup(container, app)

    result = get_random.delay()
    assert result.result == 4


def test_injects_config_parameter() -> None:
    app = _create_celery_app()

    @app.task
    def get_env(env: Annotated[str, Inject(config="env")]):
        return env

    container = wireup.create_sync_container(config={"env": "testing"})
    wireup.integration.celery.setup(container, app)

    result = get_env.delay()
    assert result.result == "testing"


def test_scoped_dependencies() -> None:
    app = _create_celery_app()

    @app.task
    def check_scoped(
        s1: Injected[ScopedService],
        s2: Injected[ScopedServiceDependency],
        s3: Injected[ScopedServiceDependency],
    ):
        assert s1.other is s2
        assert s3 is s2
        return True

    container = wireup.create_sync_container(injectables=[shared_services])
    wireup.integration.celery.setup(container, app)

    result = check_scoped.delay()
    assert result.result is True


def test_scoped_dependencies_are_different_across_tasks() -> None:
    app = _create_celery_app()
    ids = []

    @app.task
    def capture_scoped_id(dep: Injected[ScopedServiceDependency]):
        ids.append(id(dep))

    container = wireup.create_sync_container(injectables=[shared_services])
    wireup.integration.celery.setup(container, app)

    capture_scoped_id.delay()
    capture_scoped_id.delay()

    assert len(ids) == 2
    assert ids[0] != ids[1]


def test_task_with_regular_arguments() -> None:
    app = _create_celery_app()

    @app.task
    def add(x, y, random: Injected[RandomService]):
        return x + y + random.get_random()

    container = wireup.create_sync_container(injectables=[shared_services])
    wireup.integration.celery.setup(container, app)

    result = add.delay(10, 20)
    assert result.result == 34  # 10 + 20 + 4


def test_get_app_container() -> None:
    app = _create_celery_app()
    container = wireup.create_sync_container()
    wireup.integration.celery.setup(container, app)

    assert get_app_container(app) is container


def test_service_override() -> None:
    app = _create_celery_app()

    @app.task
    def get_random(random: Injected[RandomService]):
        return random.get_random()

    container = wireup.create_sync_container(injectables=[shared_services])
    wireup.integration.celery.setup(container, app)

    mocked = MagicMock()
    mocked.get_random.return_value = 99

    with get_app_container(app).override.injectable(RandomService, new=mocked):
        result = get_random.delay()
        assert result.result == 99


def test_scoped_cleanup_on_success() -> None:
    Something = NewType("Something", str)
    dep = {"created": False, "cleanup": False}

    @injectable(lifetime="scoped")
    def make_something() -> Iterator[Something]:
        dep["created"] = True
        try:
            yield Something("hello")
        finally:
            dep["cleanup"] = True

    app = _create_celery_app()

    @app.task
    def use_something(val: Injected[Something]):
        assert val == "hello"
        return "ok"

    container = wireup.create_sync_container(injectables=[make_something])
    wireup.integration.celery.setup(container, app)

    result = use_something.delay()
    assert result.result == "ok"
    assert dep["created"] is True
    assert dep["cleanup"] is True


def test_scoped_cleanup_on_failure() -> None:
    Something = NewType("Something", str)
    dep = {"created": False, "cleanup": False}

    @injectable(lifetime="scoped")
    def make_something() -> Iterator[Something]:
        dep["created"] = True
        try:
            yield Something("hello")
        finally:
            dep["cleanup"] = True

    app = _create_celery_app()
    app.config_from_object(
        {
            "task_always_eager": True,
            "task_eager_propagates": True,
        }
    )

    @app.task
    def fail_with_something(val: Injected[Something]):
        assert val == "hello"
        msg = "task error"
        raise ValueError(msg)

    container = wireup.create_sync_container(injectables=[make_something])
    wireup.integration.celery.setup(container, app)

    with pytest.raises(ValueError, match="task error"):
        fail_with_something.delay()

    assert dep["created"] is True
    assert dep["cleanup"] is True


def test_scoped_cleanup_on_failure_no_propagation() -> None:
    """Same as above but with task_eager_propagates=False.

    This exercises the task_failure signal path instead of the postrun fallback.
    """
    Something = NewType("Something", str)
    dep = {"created": False, "cleanup": False}

    @injectable(lifetime="scoped")
    def make_something() -> Iterator[Something]:
        dep["created"] = True
        try:
            yield Something("hello")
        finally:
            dep["cleanup"] = True

    app = _create_celery_app()
    app.config_from_object(
        {
            "task_always_eager": True,
            "task_eager_propagates": False,
        }
    )

    @app.task
    def fail_with_something(val: Injected[Something]):
        assert val == "hello"
        msg = "task error"
        raise ValueError(msg)

    container = wireup.create_sync_container(injectables=[make_something])
    wireup.integration.celery.setup(container, app)

    result = fail_with_something.delay()
    assert result.state == "FAILURE"
    assert dep["created"] is True
    assert dep["cleanup"] is True


def test_task_without_injected_params_is_not_affected() -> None:
    app = _create_celery_app()

    @app.task
    def plain_add(x, y):
        return x + y

    container = wireup.create_sync_container()
    wireup.integration.celery.setup(container, app)

    result = plain_add.delay(5, 7)
    assert result.result == 12


def test_builtin_celery_tasks_are_skipped() -> None:
    app = _create_celery_app()

    container = wireup.create_sync_container()
    wireup.integration.celery.setup(container, app)

    # Built-in tasks like celery.backend_cleanup should still work
    for name in app.tasks:
        if name.startswith("celery."):
            task = app.tasks[name]
            assert hasattr(task, "run")
