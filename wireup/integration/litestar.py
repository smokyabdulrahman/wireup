from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    Type,
    Union,
)
from wireup.ioc.container.async_container import AsyncContainer
from wireup.ioc.types import AnyCallable
from wireup import inject_from_container
from litestar import Litestar
from litestar.routes import ASGIRoute, HTTPRoute, WebSocketRoute


def _inject_litestar_route(
    *,
    container: AsyncContainer,
    target: AnyCallable,
    http_connection_param_name: str,
    remove_http_connection_from_arguments: bool,
    add_custom_middleware: bool,
) -> AnyCallable:
    return inject_from_container(
        container,
        _middleware=_create_fastapi_middleware(
            http_connection_param_name=http_connection_param_name,
            remove_http_connection_from_arguments=remove_http_connection_from_arguments,
        )
        if add_custom_middleware
        else None,
    )(target)


def _inject_routes(container: AsyncContainer, routers: List[HTTPRoute | ASGIRoute | WebSocketRoute]) -> None:
    for router in routers:
        if isinstance(router, ASGIRoute):
            # We should skip this. i think? https://docs.litestar.dev/2/usage/routing/handlers.html#limitations-of-asgi-route-handlers
            continue

        if not (
            isinstance(router, (HTTPRoute, WebSocketRoute))
            and router.dependant.call
            and get_inject_annotated_parameters(router.dependant.call)
        ):
            continue

        # When using the asgi middleware, the request context variable is set there.
        # and we can get the scoped container from the request.
        if isinstance(router, APIRoute) and is_using_asgi_middleware:
            router.dependant.call = inject_from_container(container, get_request_container)(router.dependant.call)
            continue

        # This is now either a websocket route or an APIRoute but the asgi middleware is not used.
        # In this case we need to use the custom route middleware to extract the current request/websocket.
        add_custom_middleware = isinstance(router, APIWebSocketRoute) or not is_using_asgi_middleware
        is_http_connection_in_signature = router.dependant.http_connection_param_name is not None

        # Setting http_connection_param_name forces FastAPI to pass the current HTTPConnection
        # to the route handler regardless of whether it was in the signature.
        # It is then extracted in the inject_from_container middleware to set the relevant context variable.
        if not router.dependant.http_connection_param_name:
            router.dependant.http_connection_param_name = "_fastapi_http_connection"

        router.dependant.call = _inject_litestar_route(
            container=container,
            target=router.dependant.call,
            http_connection_param_name=router.dependant.http_connection_param_name,
            # If the HTTPConnection was not in the signature, it needs to be removed from the arguments
            # when calling the route handler.
            remove_http_connection_from_arguments=not is_http_connection_in_signature,
            add_custom_middleware=add_custom_middleware,
        )


async def _on_startup(app: Litestar) -> None:
    container = get_app_container(app)
    _inject_routes(container, app.routes)


async def _on_shutdown(app: Litestar) -> None:
    container = get_app_container(app)
    await container.close()


def get_app_container(app: Litestar) -> AsyncContainer:
    """Return the container associated with the given application.

    This is the instance created via `wireup.create_async_container`.
    Use this when you need the container outside of the request context lifecycle.
    """
    return app.state.wireup_container


def setup(
    container: AsyncContainer,
    app: Litestar,
) -> None:
    app.state.wireup_container = container
    app.on_startup.insert(0, _on_startup)
    app.on_shutdown.append(_on_shutdown)
