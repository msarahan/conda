# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from collections import defaultdict
from contextlib import contextmanager
from enum import Enum
from errno import EPIPE, ESHUTDOWN
from functools import wraps
import json
import logging
from logging import CRITICAL, Formatter, NOTSET, StreamHandler, WARN, getLogger
import os
from os import chdir, getcwd
from os.path import dirname, isdir, isfile, join
import sys
from time import time

from .compat import StringIO
from .path import expand
from .._vendor.auxlib.decorators import memoizemethod
from .._vendor.auxlib.logz import NullHandler
from .._vendor.auxlib.type_coercion import boolify

log = getLogger(__name__)

_FORMATTER = Formatter("%(levelname)s %(name)s:%(funcName)s(%(lineno)d): %(message)s")


class CaptureTarget(Enum):
    """Constants used for contextmanager captured.

    Used similarily like the constants PIPE, STDOUT for stdlib's subprocess.Popen.
    """
    STRING = -1
    STDOUT = -2


@contextmanager
def env_var(name, value, callback=None):
    # NOTE: will likely want to call reset_context() when using this function, so pass
    #       it as callback
    name, value = str(name), str(value)
    saved_env_var = os.environ.get(name)
    try:
        os.environ[name] = value
        if callback:
            callback()
        yield
    finally:
        if saved_env_var:
            os.environ[name] = saved_env_var
        else:
            del os.environ[name]
        if callback:
            callback()


@contextmanager
def env_vars(var_map, callback=None):
    # NOTE: will likely want to call reset_context() when using this function, so pass
    #       it as callback
    saved_vars = {str(name): os.environ.get(name, NULL) for name in var_map}
    try:
        for name, value in iteritems(var_map):
            os.environ[str(name)] = str(value)
        if callback:
            callback()
        yield
    finally:
        for name, value in iteritems(saved_vars):
            if value is NULL:
                del os.environ[name]
            else:
                os.environ[name] = value
        if callback:
            callback()


@contextmanager
def captured(stdout=CaptureTarget.STRING, stderr=CaptureTarget.STRING):
    """Capture outputs of sys.stdout and sys.stderr.

    If stdout is STRING, capture sys.stdout as a string,
    if stdout is None, do not capture sys.stdout, leaving it untouched,
    otherwise redirect sys.stdout to the file-like object given by stdout.

    Behave correspondingly for stderr with the exception that if stderr is STDOUT,
    redirect sys.stderr to stdout target and set stderr attribute of yielded object to None.

    Args:
        stdout: capture target for sys.stdout, one of STRING, None, or file-like object
        stderr: capture target for sys.stderr, one of STRING, STDOUT, None, or file-like object

    Yields:
        CapturedText: has attributes stdout, stderr which are either strings, None or the
            corresponding file-like function argument.
    """
    # NOTE: This function is not thread-safe.  Using within multi-threading may cause spurious
    # behavior of not returning sys.stdout and sys.stderr back to their 'proper' state
    # """
    # Context manager to capture the printed output of the code in the with block
    #
    # Bind the context manager to a variable using `as` and the result will be
    # in the stdout property.
    #
    # >>> from conda.common.io import captured
    # >>> with captured() as c:
    # ...     print('hello world!')
    # ...
    # >>> c.stdout
    # 'hello world!\n'
    # """
    class CapturedText(object):
        pass
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    if stdout == CaptureTarget.STRING:
        sys.stdout = outfile = StringIO()
    else:
        outfile = stdout
        if outfile is not None:
            sys.stdout = outfile
    if stderr == CaptureTarget.STRING:
        sys.stderr = errfile = StringIO()
    elif stderr == CaptureTarget.STDOUT:
        sys.stderr = errfile = outfile
    else:
        errfile = stderr
        if errfile is not None:
            sys.stderr = errfile
    c = CapturedText()
    log.info("overtaking stderr and stdout")
    try:
        yield c
    finally:
        if stdout == CaptureTarget.STRING:
            c.stdout = outfile.getvalue()
        else:
            c.stdout = outfile
        if stderr == CaptureTarget.STRING:
            c.stderr = errfile.getvalue()
        elif stderr == CaptureTarget.STDOUT:
            c.stderr = None
        else:
            c.stderr = errfile
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
        log.info("stderr and stdout yielding back")


@contextmanager
def argv(args_list):
    saved_args = sys.argv
    sys.argv = args_list
    try:
        yield
    finally:
        sys.argv = saved_args


@contextmanager
def _logger_lock():
    logging._acquireLock()
    try:
        yield
    finally:
        logging._releaseLock()


@contextmanager
def disable_logger(logger_name):
    logr = getLogger(logger_name)
    _lvl, _dsbld, _prpgt = logr.level, logr.disabled, logr.propagate
    null_handler = NullHandler()
    with _logger_lock():
        logr.addHandler(null_handler)
        logr.setLevel(CRITICAL + 1)
        logr.disabled, logr.propagate = True, False
    try:
        yield
    finally:
        with _logger_lock():
            logr.removeHandler(null_handler)  # restore list logr.handlers
            logr.level, logr.disabled = _lvl, _dsbld
            logr.propagate = _prpgt


@contextmanager
def stderr_log_level(level, logger_name=None):
    logr = getLogger(logger_name)
    _hndlrs, _lvl, _dsbld, _prpgt = logr.handlers, logr.level, logr.disabled, logr.propagate
    handler = StreamHandler(sys.stderr)
    handler.name = 'stderr'
    handler.setLevel(level)
    handler.setFormatter(_FORMATTER)
    with _logger_lock():
        logr.setLevel(level)
        logr.handlers, logr.disabled, logr.propagate = [], False, False
        logr.addHandler(handler)
        logr.setLevel(level)
    try:
        yield
    finally:
        with _logger_lock():
            logr.handlers, logr.level, logr.disabled = _hndlrs, _lvl, _dsbld
            logr.propagate = _prpgt


def attach_stderr_handler(level=WARN, logger_name=None, propagate=False, formatter=None):
    # get old stderr logger
    logr = getLogger(logger_name)
    old_stderr_handler = next((handler for handler in logr.handlers if handler.name == 'stderr'),
                              None)

    # create new stderr logger
    new_stderr_handler = StreamHandler(sys.stderr)
    new_stderr_handler.name = 'stderr'
    new_stderr_handler.setLevel(NOTSET)
    new_stderr_handler.setFormatter(formatter or _FORMATTER)

    # do the switch
    with _logger_lock():
        if old_stderr_handler:
            logr.removeHandler(old_stderr_handler)
        logr.addHandler(new_stderr_handler)
        logr.setLevel(level)
        logr.propagate = propagate


def timeout(timeout_secs, func, *args, **kwargs):
    """Enforce a maximum time for a callable to complete.
    Not yet implemented on Windows.
    """
    default_return = kwargs.pop('default_return', None)
    if on_win:
        # Why does Windows have to be so difficult all the time? Kind of gets old.
        # Guess we'll bypass Windows timeouts for now.
        try:
            return func(*args, **kwargs)
        except KeyboardInterrupt:  # pragma: no cover
            return default_return
    else:
        class TimeoutException(Exception):
            pass

        def interrupt(signum, frame):
            raise TimeoutException()

        signal.signal(signal.SIGALRM, interrupt)
        signal.alarm(timeout_secs)

        try:
            ret = func(*args, **kwargs)
            signal.alarm(0)
            return ret
        except (TimeoutException,  KeyboardInterrupt):  # pragma: no cover
            return default_return


class Spinner(object):
    # spinner_cycle = cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
    spinner_cycle = cycle('/-\\|')

    def __init__(self, enable_spin=True):
        self._stop_running = Event()
        self._spinner_thread = Thread(target=self._start_spinning)
        self._indicator_length = len(next(self.spinner_cycle)) + 1
        self.fh = sys.stdout
        self.show_spin = enable_spin and hasattr(self.fh, "isatty") and self.fh.isatty()

    def start(self):
        if self.show_spin:
            self._spinner_thread.start()
        else:
            self.fh.write("...working... ")
            self.fh.flush()

    def stop(self):
        if self.show_spin:
            self._stop_running.set()
            self._spinner_thread.join()

    def _start_spinning(self):
        while not self._stop_running.is_set():
            self.fh.write(next(self.spinner_cycle) + ' ')
            self.fh.flush()
            sleep(0.10)
            self.fh.write('\b' * self._indicator_length)


@contextmanager
def spinner(message=None, enabled=True, json=False):
    """
    Args:
        message (str, optional):
            An optional message to prefix the spinner with.
            If given, ': ' are automatically added.
        enabled (bool):
            If False, usage is a no-op.
        json (bool):
           If True, will not output non-json to stdout.

    """
    sp = Spinner(enabled)
    exception_raised = False
    try:
        if message:
            if json:
                pass
            else:
                sys.stdout.write("%s: " % message)
                sys.stdout.flush()
        if not json:
            sp.start()
        yield
    except:
        exception_raised = True
        raise
    finally:
        if not json:
            sp.stop()
        if message:
            if json:
                pass
            else:
                try:
                    if exception_raised:
                        sys.stdout.write("failed\n")
                    else:
                        sys.stdout.write("done\n")
                    sys.stdout.flush()
                except (IOError, OSError) as e:
                    # Ignore BrokenPipeError and errors related to stdout or stderr being
                    # closed by a downstream program.
                    if e.errno not in (EPIPE, ESHUTDOWN):
                        raise


class ProgressBar(object):

    def __init__(self, description, enabled=True, json=False):
        """
        Args:
            description (str):
                The name of the progress bar, shown on left side of output.
            enabled (bool):
                If False, usage is a no-op.
            json (bool):
                If true, outputs json progress to stdout rather than a progress bar.
                Currently, the json format assumes this is only used for "fetch", which
                maintains backward compatibility with conda 4.3 and earlier behavior.
        """
        self.description = description
        self.enabled = enabled
        self.json = json

        if json:
            pass
        elif enabled:
            bar_format = "{desc}{bar} | {percentage:3.0f}% "
            self.pbar = tqdm(desc=description, bar_format=bar_format, ascii=True, total=1)

    def update_to(self, fraction):
        if self.json:
            sys.stdout.write('{"fetch":"%s","finished":false,"maxval":1,"progress":%f}\n\0'
                             % (self.description, fraction))
        elif self.enabled:
            self.pbar.update(fraction - self.pbar.n)

    def finish(self):
        self.update_to(1)

    def close(self):
        try:
            if self.json:
                sys.stdout.write('{"fetch":"%s","finished":true,"maxval":1,"progress":1}\n\0'
                                 % self.description)
                sys.stdout.flush()
            elif self.enabled:
                self.pbar.close()
        except (IOError, OSError) as e:
            # Ignore BrokenPipeError and errors related to stdout or stderr being
            # closed by a downstream program.
            if e.errno not in (EPIPE, ESHUTDOWN):
                raise
        self.enabled = False


@contextmanager
def backdown_thread_pool(max_workers=10):
    """Tries to create an executor with max_workers, but will back down ultimately to a single
    thread of the OS decides you can't have more than one.
    """
    from concurrent.futures import _base  # These "_" imports are gross, but I don't think there's an alternative  # NOQA
    from concurrent.futures.thread import _WorkItem

    class CondaThreadPoolExecutor(ThreadPoolExecutor):

        def submit(self, fn, *args, **kwargs):
            with self._shutdown_lock:
                if self._shutdown:
                    raise RuntimeError('cannot schedule new futures after shutdown')

                f = _base.Future()
                w = _WorkItem(f, fn, args, kwargs)

                self._work_queue.put(w)
                try:
                    self._adjust_thread_count()
                except RuntimeError:
                    # RuntimeError: can't start new thread
                    # See https://github.com/conda/conda/issues/6624
                    if len(self._threads) > 0:
                        # It's ok to not be able to start new threads if we already have at least
                        # one thread alive.
                        pass
                    else:
                        raise
                return f

    try:
        yield CondaThreadPoolExecutor(max_workers)
    except RuntimeError as e:  # pragma: no cover
        # RuntimeError is thrown if number of threads are limited by OS
        log.debug(repr(e))
        try:
            yield CondaThreadPoolExecutor(floor(max_workers / 2))
        except RuntimeError as e:
            log.debug(repr(e))
            yield CondaThreadPoolExecutor(1)


class ContextDecorator(object):
    def __call__(self, f):
        @wraps(f)
        def decorated(*args, **kwds):
            with self:
                return f(*args, **kwds)
        return decorated


class time_recorder(ContextDecorator):  # pragma: no cover
    start_time = None
    record_file = expand(join('~', '.conda', 'instrumentation-record.csv'))

    def __init__(self, entry_name):
        self.entry_name = entry_name

    def __enter__(self):
        enabled = os.environ.get('CONDA_INSTRUMENTATION_ENABLED')
        if enabled and boolify(enabled):
            self.start_time = time()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.start_time:
            end_time = time()
            run_time = end_time - self.start_time
            self._ensure_dir()
            with open(self.record_file, 'a') as fh:
                fh.write("%s,%s\n" % (self.entry_name, run_time))

    @memoizemethod
    def _ensure_dir(self):
        if not isdir(dirname(self.record_file)):
            os.makedirs(dirname(self.record_file))


def print_instrumentation_data():  # pragma: no cover
    record_file = expand(join('~', '.conda', 'instrumentation-record.csv'))

    grouped_data = defaultdict(list)
    final_data = {}

    if not isfile(record_file):
        return

    with open(record_file) as fh:
        for line in fh:
            entry_name, total_time = line.strip().split(',')
            grouped_data[entry_name].append(float(total_time))

    for entry_name in sorted(grouped_data):
        all_times = grouped_data[entry_name]
        counts = len(all_times)
        total_time = sum(all_times)
        average_time = total_time / counts
        final_data[entry_name] = {
            'counts': counts,
            'total_time': total_time,
            'average_time': average_time,
        }

    print(json.dumps(final_data, sort_keys=True, indent=2, separators=(',', ': ')))


if __name__ == "__main__":
    print_instrumentation_data()
