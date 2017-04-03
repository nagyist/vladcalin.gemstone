import logging
import os
import functools
import random
import argparse
import threading
import sys
from abc import ABC
from concurrent.futures import ThreadPoolExecutor

from tornado.web import StaticFileHandler
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.web import Application
from tornado.log import enable_pretty_logging

from gemstone.config.configurable import Configurable
from gemstone.config.configurator import CommandLineConfigurator
from gemstone.core.stats import DefaultStatsContainer, DummyStatsContainer
from gemstone.discovery.cache import ServiceDiscoveryCache
from gemstone.errors import ServiceConfigurationError
from gemstone.core.handlers import TornadoJsonRpcHandler
from gemstone.core.decorators import exposed_method
from gemstone.client.remote_service import RemoteService
from gemstone.core.container import Container

__all__ = [
    'MicroService'
]

IS_WINDOWS = sys.platform.startswith("win32")


class MicroService(Container):
    #: The name of the service. Is required.
    name = None

    #: The host where the service will listen
    host = "127.0.0.1"

    #: The port where the service will bind
    port = 8000

    #: The url where the service can be accessed by other microservices.
    #: Useful when using a service registry.
    accessible_at = None

    #: The path in the URL where the microservice JSON RPC endpoint will be accessible.
    endpoint = "/api"

    #: Template directory used by the created Tornado Application.
    #: Useful when you plan to add web application functionality
    #: to the microservice.
    template_dir = "."

    #: A list of directories where the static files will looked for.
    static_dirs = []

    #: A list of extra Tornado handlers that will be included in the
    #: created Tornado application.
    extra_handlers = []

    #: A list of service registry complete URL which will enable service auto-discovery.
    service_registry_urls = []
    #: Interval (in seconds) when the microservice will ping all the service registries.
    service_registry_ping_interval = 30
    discovery_strategies = [

    ]
    _remote_service_cache = ServiceDiscoveryCache(3600)

    #: Specifies if should record statistics
    use_statistics = False

    #: A list of (callable, time_in_seconds) that will enable periodic task execution.
    periodic_tasks = []

    #: A list of Event transports that will enable the Event dispatching feature.
    event_transports = []

    #: A list of configurable objects that allows the service's running parameters to
    #: be changed dynamically without changing its code.
    configurables = [
        Configurable("port",
                     template=lambda x: random.randint(8000, 65000) if "random" else int(x)),
        Configurable("host"),
        Configurable("accessible_at"),
        Configurable("endpoint")
    ]
    #: A list of configurator objects that will extract in order values for
    #: the defined configurators
    configurators = [
        CommandLineConfigurator()
    ]

    #: a list of ``gemstone.core.modules.Module`` instances
    modules = [

    ]

    # in some situations, on Windows the event loop may hang
    # http://stackoverflow.com/questions/33634956/why-would-a-timeout-avoid-a-tornado-hang/33643631#33643631
    default_periodic_tasks = [(lambda: None, 0.5)] if IS_WINDOWS else []

    #: How many methods can be executed in parallel at the same time. Note that every blocking
    #: method is executed in a ``concurrent.features.ThreadPoolExecutor``
    max_parallel_blocking_tasks = os.cpu_count()
    _executor = None

    def __init__(self, io_loop=None):
        """

        The base class for implementing microservices.

        :param io_loop: A :py:class:`tornado.ioloop.IOLoop` instance -
                        can be used to share the same io loop between
                        multiple microservices running from the same process.
        """
        self.app = None
        self._periodic_tasks_objs = []
        self.logger = self.get_logger()
        self.registries = []

        # name
        if self.name is None:
            raise ServiceConfigurationError("No name defined for the microservice")
        self.logger.debug("Service name: {}".format(self.name))

        # endpoint
        if self.accessible_at is None:
            self.accessible_at = "http://{host}:{port}{endpoint}".format(
                host=self.host, port=self.port, endpoint=self.endpoint
            )

        # methods
        self.methods = {}

        # event handlers
        self.event_handlers = {}

        # executor
        if self.max_parallel_blocking_tasks <= 0:
            raise ServiceConfigurationError("Invalid max_parallel_blocking_tasks value")

        self._executor = ThreadPoolExecutor(self.max_parallel_blocking_tasks)

        # ioloop
        self.io_loop = io_loop or IOLoop.current()

    @exposed_method()
    def get_service_specs(self):
        """
        A default exposed method that returns the current microservice specifications. The returned information is
        in the format:

        ::

            {
                "host": "127.0.0.1",
                "port": 9000,
                "name": "service.example",
                "max_parallel_blocking_tasks": 8,
                "methods": {
                    "get_service_specs": "...",
                    "method1": "method1's docstring",
                    ...
                }
            }

        :return:
        """
        return {
            "host": self.host,
            "port": self.port,
            "accessible_at": self.accessible_at,
            "name": self.name,
            "max_parallel_blocking_tasks": self.max_parallel_blocking_tasks,
            "methods": {m: self.methods[m].__doc__ for m in self.methods},
            "event_transports": [str(t) for t in self.event_transports],
            "events_handled": {ev_name: ev_handle.__doc__ for ev_name, ev_handle in
                               self.event_handlers.items()}
        }

    # region Can be overridden by user

    def on_service_start(self):
        """
        Override this method to do a set of actions when the service starts

        :return: ``None``
        """
        pass

    def before_method_call(self, request_object):
        """
        Called before every RPC method call

        :param request_object: a :py:class:`gemstone.core.structs.JsonRpcRequest` instance.
        """
        pass

    def after_method_call(self, request_object, response_object):
        """
        Called after every RPC **successful** method call. If ``response_object`` instance is
        modified the response of the actual call is modified

        :param request_object: a :py:class:`gemstone.core.structs.JsonRpcRequest` instance.
        :param response_object: a :py:class:`gemstone.core.structs.JsonRpcResponse` instance.
        :return:
        """
        pass

    def on_failed_method_call(self, request_object, response_object):
        # TODO: make the json rpc handler use this
        pass

    def authenticate_request(self, handler):
        """
        Based on the current request handler, checks if the request if valid.

        :param handler: a JsonRpcRequestHandler instance for the current request
        :return: False or None if the method call should be denied, or something whose
                 boolean value is True otherwise.

        .. versionadded:: 0.10

        """
        return True

    def get_logger(self):
        """
        Override this method to designate the logger for the application

        :return: a :py:class:`logging.Logger` instance
        """
        enable_pretty_logging()
        return logging.getLogger("tornado.application")

    # endregion

    # region Can be called by user

    def get_service(self, name):
        """
        Locates a remote service by name. The name can be a glob-like pattern
        (``"project.worker.*"``). If multiple services match the given name, a
        random instance will be chosen. There might be multiple services that
        match a given name if there are multiple services with the same name
        running, or when the pattern matches multiple different services.

        .. todo::

            Make this use self.io_loop to resolve the request. The current
            implementation is blocking and slow

        :param name: a pattern for the searched service.
        :return: a :py:class:`gemstone.RemoteService` instance
        :raises ValueError: when the service can not be located
        :raises ServiceConfigurationError: when there is no configured discovery strategy
        """
        if not self.discovery_strategies:
            raise ServiceConfigurationError("No service registry available")

        cached = self._remote_service_cache.get_entry(name)
        if cached:
            return cached.remote_service

        for strategy in self.discovery_strategies:
            endpoints = strategy.locate(name)
            if not endpoints:
                continue
            random.shuffle(endpoints)
            for url in endpoints:
                try:
                    service = RemoteService(url)
                    self._remote_service_cache.add_entry(name, service)
                    return service
                except ConnectionError:
                    continue  # could not establish connection, try next

        raise ValueError("Service could not be located")

    def get_io_loop(self):
        return self.io_loop or IOLoop.current()

    def get_executor(self):
        return self._executor

    def start_thread(self, target, args, kwargs):
        """
        Shortcut method for starting a thread.

        :param target: The function to be executed.
        :param args: A tuple or list representing the positional arguments for the thread.
        :param kwargs: A dictionary representing the keyword arguments.

        .. versionadded:: 0.5.0
        """
        thread_obj = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
        thread_obj.start()

    def emit_event(self, event_name, event_body, *, broadcast=True):
        """
        Publishes an event of type ``event_name`` to all subscribers, having the body
        ``event_body``. The event is pushed through all available event transports.

        The event body must be a Python object that can be represented as a JSON.

        :param event_name: a ``str`` representing the event type
        :param event_body: a Python object that can be represented as JSON.
        :param broadcast: flag that specifies if the event should be received by
                          all subscribers or only by one

        .. versionadded:: 0.5.0

        .. versionchanged:: 0.10.0
            Added parameter broadcast
        """

        for transport in self.event_transports:
            transport.emit_event(event_name, event_body, broadcast=broadcast)

    def start(self):
        """
        The main method that starts the service. This is blocking.

        """
        self._initial_setup()
        self.on_service_start()

        self.app = self.make_tornado_app()
        enable_pretty_logging()
        self.app.listen(self.port, address=self.host)

        for k, v in self.get_current_configuration().items():
            self.logger.debug("{}={}".format(k, v))

        self._start_periodic_tasks()
        # starts the event handlers
        self._initialize_event_handlers()
        self._start_event_handlers()

        try:
            self.io_loop.start()
        except RuntimeError:
            # TODO : find a way to check if the io_loop is running before trying to start it
            # this method to check if the loop is running is ugly
            pass

    def configure(self):
        """
        Called to explicitly use the configurators.

        Example usage ::

            class MyMicroservice(MicroService):
                name = "test"
                host = "127.0.0.1"
                port = 8000
                configurables = [
                    Configurable("host"), Configurable("port", template=lambda x: int(x))
                ]
                configurators = [CommandLineConfigurator()]

            service = MyMicroservice()
            print(service.host, service.port)
            # 127.0.0.1 8000
            sys.argv[1:] = ["--port", "80", "--host", "0.0.0.0"]
            service.configure() # we use the defined CommandLineConfigurator()
            print(service.host, service.port)
            # 0.0.0.0 80

        """
        self._prepare_configurators()
        self._activate_configurators()

    def _start_periodic_tasks(self):
        for periodic_task in self._periodic_task_iter():
            self.logger.debug("Starting periodic task {}".format(periodic_task))
            periodic_task.start()

    def get_current_configuration(self):
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "endpoint": self.endpoint,
            "accessible_at": self.accessible_at,
            "autodiscovery": {
                "service_registry_urls": self.service_registry_urls,
                "service_registry_ping_interval": self.service_registry_ping_interval,
            },
            "max_parallel_blocking_tasks": self.max_parallel_blocking_tasks,
            "webapp": {
                "template_dir": self.template_dir,
                "static_dirs": self.static_dirs,
                "extra_handlers": [str(h) for h in self.extra_handlers]
            },
            "event": {
                "event_transports": [str(t) for t in self.event_transports]
            },
            "configuration": {
                "configurables": [str(c) for c in self.configurables],
                "configurators": [str(c) for c in self.configurators]
            }

        }

    # endregion

    def _initial_setup(self):
        # prepare modules
        for module in self.modules:
            module.set_microservice(self)

        self._gather_exposed_methods()
        self._gather_event_handlers()

        # initializing statistics handler
        if self.use_statistics:
            self.stats = DefaultStatsContainer()
            self.methods["statistics"] = lambda: self.stats.as_json()
        else:
            self.stats = DummyStatsContainer()

    def _initialize_event_handlers(self):
        for event_transport in self.event_transports:
            self.logger.debug("Initializing transport {}".format(event_transport))
            for event_name, event_handler in self.event_handlers.items():
                self.logger.debug("Setting handler for {}".format(event_name))
                event_transport.register_event_handler(event_handler, event_name)

    def _start_event_handlers(self):
        for event_transport in self.event_transports:
            self.start_thread(
                target=event_transport.start_accepting_events, args=(), kwargs={}
            )

    def make_tornado_app(self):
        """
        Creates a :py:class`tornado.web.Application` instance that respect the
        JSON RPC 2.0 specs and exposes the designated methods. Can be used
        in tests to obtain the Tornado application.

        :return: a :py:class:`tornado.web.Application` instance
        """

        handlers = [
            (self.endpoint, TornadoJsonRpcHandler, {"microservice": self})
        ]

        self._add_extra_handlers(handlers)
        self._add_static_handlers(handlers)

        return Application(handlers, template_path=self.template_dir)

    def _add_extra_handlers(self, handlers):
        """
        Adds the extra handler (defined by the user)

        :param handlers: a list of :py:class:`tornado.web.RequestHandler` instances.
        :return:
        """
        extra_handlers = [(h[0], h[1], {"microservice": self}) for h in self.extra_handlers]
        handlers.extend(extra_handlers)

    def _add_static_handlers(self, handlers):
        """
        Creates and adds the handles needed for serving static files.

        :param handlers:
        """
        for url, path in self.static_dirs:
            handlers.append((url.rstrip("/") + "/(.*)", StaticFileHandler, {"path": path}))

    def _gather_exposed_methods(self):
        """
        Searches for the exposed methods in the current microservice class. A method is considered
        exposed if it is decorated with the :py:func:`gemstone.public_method` or
        :py:func:`gemstone.private_api_method`.
        """

        self._extract_methods_from_container(self)

        for module in self.modules:
            self._extract_methods_from_container(module)

    def _extract_methods_from_container(self, container):
        for item in container.get_exposed_methods():
            exposed_name = getattr(item, '_exposed_name', item.__name__)
            if exposed_name in self.methods:
                raise ValueError(
                    "Cannot expose two methods under the same name: '{}'".format(exposed_name))
            self.methods[exposed_name] = item

    def _gather_event_handlers(self):
        """
        Searches for the event handlers in the current microservice class.

        :return:
        """
        self._extract_event_handlers_from_container(self)
        for module in self.modules:
            self._extract_event_handlers_from_container(module)

    def _extract_event_handlers_from_container(self, container):
        for item in container.get_event_handlers():
            self.event_handlers.setdefault(getattr(item, "_handled_event"), item)

    def _periodic_task_iter(self):
        """
        Iterates through all the periodic tasks:

        - the service registry pinging
        - default dummy task if on Windows
        - user defined periodic tasks

        :return:
        """
        for strategy in self.discovery_strategies:
            self.default_periodic_tasks.append(
                (functools.partial(strategy.ping, self.name, self.accessible_at),
                 self.service_registry_ping_interval)
            )
            self.default_periodic_tasks[-1][0]()

        all_periodic_tasks = self.default_periodic_tasks + self.periodic_tasks
        for func, timer_in_seconds in all_periodic_tasks:
            timer_milisec = timer_in_seconds * 1000
            yield PeriodicCallback(func, timer_milisec, io_loop=self.io_loop)

    @classmethod
    def _set_option_if_available(cls, args, name):
        if hasattr(args, name) and getattr(args, name) is not None:
            setattr(cls, name, getattr(args, name))

    def _prepare_configurators(self):
        for configurator in self.configurators:
            for configurable in self.configurables:
                configurator.register_configurable(configurable)

    def _activate_configurators(self):
        for configurator in self.configurators:
            configurator.load()

        for configurator in self.configurators:
            for configurable in self.configurables:
                name = configurable.name
                value = configurator.get(name)
                if not value:
                    continue

                setattr(self, name, value)
