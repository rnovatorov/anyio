import asyncio
import concurrent.futures
import inspect
import math
import os
import socket
from functools import partial
from threading import Thread
from typing import (
    Callable, Set, Optional, Union, Tuple, cast, Coroutine, Any, Awaitable, TypeVar,
    Generator, List, Dict)
from weakref import WeakKeyDictionary

from async_generator import async_generator, yield_, asynccontextmanager, aclosing

from .._networking import BaseSocket
from .. import abc, claim_worker_thread, _local, T_Retval, TaskInfo
from ..exceptions import ExceptionGroup, CancelledError, ClosedResourceError, ResourceBusyError

try:
    from asyncio import run as native_run, create_task, get_running_loop, current_task, all_tasks
except ImportError:
    _T = TypeVar('_T')

    # Snatched from the standard library
    def native_run(main: Awaitable[_T], *, debug: bool = False) -> _T:
        """Run a coroutine.

        This function runs the passed coroutine, taking care of
        managing the asyncio event loop and finalizing asynchronous
        generators.

        This function cannot be called when another asyncio event loop is
        running in the same thread.

        If debug is True, the event loop will be run in debug mode.

        This function always creates a new event loop and closes it at the end.
        It should be used as a main entry point for asyncio programs, and should
        ideally only be called once.

        Example:

            async def main():
                await asyncio.sleep(1)
                print('hello')

            asyncio.run(main())
        """
        from asyncio import events, coroutines

        if events._get_running_loop() is not None:
            raise RuntimeError(
                "asyncio.run() cannot be called from a running event loop")

        if not coroutines.iscoroutine(main):
            raise ValueError("a coroutine was expected, got {!r}".format(main))

        loop = events.new_event_loop()
        try:
            events.set_event_loop(loop)
            loop.set_debug(debug)
            return loop.run_until_complete(main)
        finally:
            try:
                _cancel_all_tasks(loop)
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                events.set_event_loop(None)  # type: ignore
                loop.close()

    def _cancel_all_tasks(loop):
        from asyncio import gather

        to_cancel = all_tasks(loop)
        if not to_cancel:
            return

        for task in to_cancel:
            task.cancel()

        loop.run_until_complete(
            gather(*to_cancel, loop=loop, return_exceptions=True))

        for task in to_cancel:
            if task.cancelled():
                continue
            if task.exception() is not None:
                loop.call_exception_handler({
                    'message': 'unhandled exception during asyncio.run() shutdown',
                    'exception': task.exception(),
                    'task': task,
                })

    def create_task(coro: Union[Generator[Any, None, _T], Awaitable[_T]]) -> asyncio.Task:
        return get_running_loop().create_task(coro)

    def get_running_loop() -> asyncio.AbstractEventLoop:
        loop = asyncio._get_running_loop()
        if loop is not None:
            return loop
        else:
            raise RuntimeError('no running event loop')

    def all_tasks(loop: Optional[asyncio.AbstractEventLoop] = None) -> Set[asyncio.Task]:
        """Return a set of all tasks for the loop."""
        from asyncio import Task

        if loop is None:
            loop = get_running_loop()

        return {t for t in Task.all_tasks(loop) if not t.done()}

    def current_task(loop: Optional[asyncio.AbstractEventLoop] = None) -> Optional[asyncio.Task]:
        if loop is None:
            loop = get_running_loop()

        return asyncio.Task.current_task(loop)

# Check whether there is native support for task names in asyncio (3.8+)
_task_parents = WeakKeyDictionary()  # type: WeakKeyDictionary[asyncio.Task, int]
_task_names = None  # type: Optional[WeakKeyDictionary[asyncio.Task, str]]
if 'name' not in inspect.signature(create_task).parameters:
    _task_names = WeakKeyDictionary()


#
# Event loop
#

def run(func: Callable[..., T_Retval], *args, debug: bool = False,
        policy: Optional[asyncio.AbstractEventLoopPolicy] = None) -> T_Retval:
    async def wrapper():
        nonlocal exception, retval
        try:
            retval = await func(*args)
        except BaseException as exc:
            exception = exc

    if policy is not None:
        asyncio.set_event_loop_policy(policy)

    exception = retval = None
    native_run(wrapper(), debug=debug)
    if exception is not None:
        raise exception
    else:
        return cast(T_Retval, retval)


#
# Miscellaneous
#

finalize = aclosing


async def sleep(delay: float) -> None:
    check_cancelled()
    await asyncio.sleep(delay)


#
# Timeouts and cancellation
#

class CancelScope:
    __slots__ = ('_deadline', '_shield', '_parent_scope', '_cancel_called', '_active',
                 '_timeout_task', '_tasks', '_timeout_expired')

    def __init__(self, deadline: float = math.inf, shield: bool = False):
        self._deadline = deadline
        self._shield = shield
        self._parent_scope = None
        self._cancel_called = False
        self._active = False
        self._timeout_task = None
        self._tasks = set()  # type: Set[asyncio.Task]
        self._timeout_expired = False

    async def __aenter__(self):
        async def timeout():
            await asyncio.sleep(self._deadline - get_running_loop().time())
            self._timeout_expired = True
            await self.cancel()

        if self._active:
            raise RuntimeError(
                "Each CancelScope may only be used for a single 'async with' block"
            )

        host_task = current_task()
        self._parent_scope = get_cancel_scope(host_task)
        self._tasks.add(host_task)
        set_cancel_scope(host_task, self)

        if self._deadline != math.inf:
            self._timeout_task = get_running_loop().create_task(timeout())

        self._active = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._active = False
        if self._timeout_task:
            self._timeout_task.cancel()

        host_task = current_task()
        self._tasks.remove(host_task)
        set_cancel_scope(host_task, self._parent_scope)

        if isinstance(exc_val, asyncio.CancelledError):
            if self._timeout_expired:
                return True
            elif self._cancel_called:
                # This scope was directly cancelled
                return True

    async def cancel(self):
        if self._cancel_called:
            return

        self._cancel_called = True

        # Cancel any contained tasks
        for task in self._tasks:
            if task._coro.cr_await is not None and not task._coro.cr_running:
                # Cancel the task directly, but only if it's blocked and isn't within a shielded
                # scope
                scope = get_cancel_scope(task)
                if scope is self or not scope.shield:
                    task.cancel()

    @property
    def deadline(self) -> float:
        return self._deadline

    @property
    def cancel_called(self) -> bool:
        return self._cancel_called

    @property
    def shield(self) -> bool:
        return self._shield


abc.CancelScope.register(CancelScope)


def get_cancel_scope(task: asyncio.Task) -> Optional[CancelScope]:
    try:
        return _local.cancel_scopes_by_task.get(task)
    except AttributeError:
        return None


def set_cancel_scope(task: asyncio.Task, scope: Optional[CancelScope]):
    try:
        cancel_scopes = _local.cancel_scopes_by_task
    except AttributeError:
        cancel_scopes = _local.cancel_scopes_by_task = {}

    if scope is None:
        del cancel_scopes[task]
    else:
        cancel_scopes[task] = scope


def check_cancelled():
    task = current_task()
    cancel_scope = get_cancel_scope(task)
    if cancel_scope is not None and not cancel_scope._shield and cancel_scope._cancel_called:
        raise CancelledError


def open_cancel_scope(deadline: float = math.inf, shield: bool = False) -> CancelScope:
    return CancelScope(deadline, shield)


@asynccontextmanager
@async_generator
async def fail_after(delay: float, shield: bool):
    deadline = get_running_loop().time() + delay
    async with CancelScope(deadline, shield) as scope:
        await yield_(scope)

    if scope._timeout_expired:
        raise TimeoutError


@asynccontextmanager
@async_generator
async def move_on_after(delay: float, shield: bool):
    deadline = get_running_loop().time() + delay
    async with CancelScope(deadline=deadline, shield=shield) as scope:
        await yield_(scope)


async def current_effective_deadline():
    deadline = math.inf
    cancel_scope = get_cancel_scope(current_task())
    while cancel_scope:
        deadline = min(deadline, cancel_scope.deadline)
        cancel_scope = cancel_scope._parent_scope

    return deadline


#
# Task groups
#

class TaskGroup:
    __slots__ = 'cancel_scope', '_active'

    def __init__(self):
        self.cancel_scope = CancelScope()
        self._active = False

    async def __aenter__(self):
        await self.cancel_scope.__aenter__()
        self._active = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cancel_scope.__aexit__(exc_type, exc_val, exc_tb)

        exceptions = []  # type: List[BaseException]
        ignore_exception = False
        if exc_val is not None:
            await self.cancel_scope.cancel()
            if not isinstance(exc_val, (asyncio.CancelledError, CancelledError)):
                exceptions.append(exc_val)
            elif not self.cancel_scope._parent_scope:
                ignore_exception = True
            elif not self.cancel_scope._parent_scope.cancel_called:
                ignore_exception = True

        while self.cancel_scope._tasks:
            for task in self.cancel_scope._tasks.copy():
                try:
                    await task
                except BaseException as exc:
                    set_cancel_scope(task, None)
                    self.cancel_scope._tasks.remove(task)
                    if not isinstance(exc, (asyncio.CancelledError, CancelledError)):
                        exceptions.append(exc)

        self._active = False
        if len(exceptions) > 1:
            raise ExceptionGroup(exceptions)
        elif exceptions and exceptions[0] is not exc_val:
            raise exceptions[0]

        return ignore_exception

    async def _run_wrapped_task(self, func, *args):
        task = current_task()
        try:
            await func(*args)
        except BaseException:
            await self.cancel_scope.cancel()
            raise
        else:
            set_cancel_scope(task, None)
            self.cancel_scope._tasks.remove(task)

    async def spawn(self, func: Callable, *args, name=None) -> None:
        if not self._active:
            raise RuntimeError('This task group is not active; no new tasks can be spawned.')

        if _task_names is None:
            task = create_task(self._run_wrapped_task(func, *args), name=name)  # type: ignore
        else:
            task = create_task(self._run_wrapped_task(func, *args))
            _task_parents[task] = id(current_task())
            if name is not None:
                _task_names[task] = name

        # Make the spawned task inherit the task group's cancel scope
        self.cancel_scope._tasks.add(task)
        set_cancel_scope(task, self.cancel_scope)


abc.TaskGroup.register(TaskGroup)


#
# Threads
#

_Retval_Queue_Type = Tuple[Optional[T_Retval], Optional[BaseException]]


async def run_in_thread(func: Callable[..., T_Retval], *args) -> T_Retval:
    def thread_worker():
        try:
            with claim_worker_thread('asyncio'):
                _local.loop = loop
                result = func(*args)
        except BaseException as exc:
            loop.call_soon_threadsafe(queue.put_nowait, (None, exc))
        else:
            loop.call_soon_threadsafe(queue.put_nowait, (result, None))

    check_cancelled()
    loop = get_running_loop()
    queue = asyncio.Queue(1)  # type: asyncio.Queue[_Retval_Queue_Type]
    thread = Thread(target=thread_worker)
    thread.start()
    retval, exception = await queue.get()
    if exception is not None:
        raise exception
    else:
        return cast(T_Retval, retval)


def run_async_from_thread(func: Callable[..., Coroutine[Any, Any, T_Retval]], *args) -> T_Retval:
    f = asyncio.run_coroutine_threadsafe(
        func(*args), _local.loop)  # type: concurrent.futures.Future[T_Retval]
    return f.result()


#
# Async file I/O
#

class AsyncFile:
    def __init__(self, fp) -> None:
        self._fp = fp

    def __getattr__(self, name):
        return getattr(self._fp, name)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    @async_generator
    async def __aiter__(self):
        while True:
            line = await self.readline()
            if line:
                await yield_(line)
            else:
                break

    async def read(self, size: int = -1) -> Union[bytes, str]:
        return await run_in_thread(self._fp.read, size)

    async def read1(self, size: int = -1) -> Union[bytes, str]:
        return await run_in_thread(self._fp.read1, size)

    async def readline(self) -> bytes:
        return await run_in_thread(self._fp.readline)

    async def readlines(self) -> bytes:
        return await run_in_thread(self._fp.readlines)

    async def readinto(self, b: Union[bytes, memoryview]) -> bytes:
        return await run_in_thread(self._fp.readinto, b)

    async def readinto1(self, b: Union[bytes, memoryview]) -> bytes:
        return await run_in_thread(self._fp.readinto1, b)

    async def write(self, b: bytes) -> None:
        return await run_in_thread(self._fp.write, b)

    async def writelines(self, lines: bytes) -> None:
        return await run_in_thread(self._fp.writelines, lines)

    async def truncate(self, size: Optional[int] = None) -> int:
        return await run_in_thread(self._fp.truncate, size)

    async def seek(self, offset: int, whence: Optional[int] = os.SEEK_SET) -> int:
        return await run_in_thread(self._fp.seek, offset, whence)

    async def tell(self) -> int:
        return await run_in_thread(self._fp.tell)

    async def flush(self) -> None:
        return await run_in_thread(self._fp.flush)

    async def close(self) -> None:
        return await run_in_thread(self._fp.close)


async def aopen(*args, **kwargs):
    fp = await run_in_thread(partial(open, *args, **kwargs))
    return AsyncFile(fp)


#
# Sockets and networking
#

_read_events = {}  # type: Dict[socket.SocketType, asyncio.Event]
_write_events = {}  # type: Dict[socket.SocketType, asyncio.Event]


class Socket(BaseSocket):
    __slots__ = '_loop', '_read_event', '_write_event'

    def __init__(self, raw_socket: socket.SocketType) -> None:
        super().__init__(raw_socket)
        self._loop = get_running_loop()
        self._read_event = asyncio.Event(loop=self._loop)
        self._write_event = asyncio.Event(loop=self._loop)

    def _wait_readable(self):
        return wait_socket_readable(self._raw_socket)

    def _wait_writable(self):
        return wait_socket_writable(self._raw_socket)

    def _notify_close(self):
        return notify_socket_close(self._raw_socket)

    async def _check_cancelled(self) -> None:
        check_cancelled()

    def _run_in_thread(self, func: Callable, *args):
        return run_in_thread(func, *args)


async def wait_socket_readable(sock: socket.SocketType) -> None:
    check_cancelled()
    if _read_events.get(sock):
        raise ResourceBusyError('reading from') from None

    loop = get_running_loop()
    event = _read_events[sock] = asyncio.Event(loop=loop)
    loop.add_reader(sock, event.set)
    try:
        await event.wait()
    finally:
        loop.remove_reader(sock)
        del _read_events[sock]

    if sock.fileno() == -1:
        raise ClosedResourceError


async def wait_socket_writable(sock: socket.SocketType) -> None:
    check_cancelled()
    if _write_events.get(sock):
        raise ResourceBusyError('writing to') from None

    loop = get_running_loop()
    event = _write_events[sock] = asyncio.Event(loop=loop)
    loop.add_writer(sock.fileno(), event.set)
    try:
        await event.wait()
    finally:
        loop.remove_writer(sock)
        del _write_events[sock]

    if sock.fileno() == -1:
        raise ClosedResourceError


async def notify_socket_close(sock: socket.SocketType) -> None:
    for events_map in _read_events, _write_events:
        event = events_map.get(sock)
        if event is not None:
            event.set()


#
# Synchronization
#

class Lock(asyncio.Lock):
    async def __aenter__(self):
        check_cancelled()
        await self.acquire()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()


class Condition(asyncio.Condition):
    async def __aenter__(self):
        check_cancelled()
        return await super().__aenter__()

    async def notify(self, n=1):
        super().notify(n)

    async def notify_all(self):
        super().notify(len(self._waiters))

    def wait(self):
        check_cancelled()
        return super().wait()


class Event(asyncio.Event):
    async def set(self):
        super().set()

    def wait(self):
        check_cancelled()
        return super().wait()


class Semaphore(asyncio.Semaphore):
    def __aenter__(self):
        check_cancelled()
        return super().__aenter__()

    @property
    def value(self):
        return self._value


class Queue(asyncio.Queue):
    def get(self):
        check_cancelled()
        return super().get()

    def put(self, item):
        check_cancelled()
        return super().put(item)


abc.Lock.register(Lock)
abc.Condition.register(Condition)
abc.Event.register(Event)
abc.Semaphore.register(Semaphore)
abc.Queue.register(Queue)


#
# Operating system signals
#

@asynccontextmanager
@async_generator
async def receive_signals(*signals: int):
    @async_generator
    async def process_signal_queue():
        while True:
            signum = await queue.get()
            await yield_(signum)

    loop = get_running_loop()
    queue = asyncio.Queue(loop=loop)  # type: asyncio.Queue[int]
    handled_signals = set()
    agen = process_signal_queue()
    try:
        for sig in set(signals):
            loop.add_signal_handler(sig, queue.put_nowait, sig)
            handled_signals.add(sig)

        await yield_(agen)
    finally:
        await agen.aclose()
        for sig in handled_signals:
            loop.remove_signal_handler(sig)


#
# Testing and debugging
#

def _create_task_info(task: asyncio.Task) -> TaskInfo:
    coro = task._coro  # type: ignore
    if _task_names is None:
        name = task.get_name()  # type: ignore
    else:
        name = _task_names.get(task)

    return TaskInfo(id(task), _task_parents.get(task), name, coro)


async def get_current_task() -> TaskInfo:
    return _create_task_info(current_task())  # type: ignore


async def get_running_tasks() -> List[TaskInfo]:
    return [_create_task_info(task) for task in all_tasks() if not task.done()]


async def wait_all_tasks_blocked() -> None:
    this_task = current_task()
    while True:
        for task in all_tasks():
            if task._coro.cr_await is None and task is not this_task:  # type: ignore
                await sleep(0)
                break
        else:
            return
