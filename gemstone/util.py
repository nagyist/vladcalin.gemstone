import sys
import logging
from multiprocessing.pool import ThreadPool


def init_default_logger():
    logging.basicConfig(
        level=logging.DEBUG,
    )
    return logging.getLogger()


def as_completed(*async_result_wrappers):
    """
    Yields results as they become available.

    :param async_result_wrappers: :py:class:`gemstone.client.remote_service.AsyncMethodCall` instances.
    :return: a generator that yields items as soon they results become available.
    """

    wrappers_copy = list(async_result_wrappers)

    while len(wrappers_copy):
        completed = list(filter(lambda x: x.finished(), wrappers_copy))
        if not len(completed):
            continue

        for item in completed:
            wrappers_copy.remove(item)
            yield item


def first_completed(*async_result_wrappers):
    """
    Just like :py:func:`gemstone.as_completed`, but returns only the first item and discards the
    rest

    :param async_result_wrappers:
    :return:
    """
    wrappers_copy = list(async_result_wrappers)
    while True:
        completed = list(filter(lambda x: x.finished(), wrappers_copy))
        if not len(completed):
            continue

        return completed[0]


def make_callbacks(async_result_wrappers, on_result, on_error, run_in_threads=False):
    """
    Monitors the :py:class:`gemstone.client.remote_service.AsyncMethodCall` instances from `async_result_wrappers`
    and apply callbacks depending on their outcome.

    :param async_result_wrappers: An iterable of :py:class:`gemstone.client.remote_service.AsyncMethodCall`
    :param on_result: a callable that takes a single positional argument (the result)
    :param on_error: a callabke that takes a single positional argument (the error)
    :param run_in_threads: flag tha specifies if the callbacks should be called in the current thread or in background
                           threads
    """

    if run_in_threads:
        pool = ThreadPool()

    for item in as_completed(*async_result_wrappers):
        if item.error():
            if run_in_threads:
                pool.apply_async(on_error, args=(item.error(),))
            else:
                on_error(item.error())
        else:
            if run_in_threads:
                pool.apply_async(on_result, args=(item.result(),))
            else:
                on_result(item.result())
