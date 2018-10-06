# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from contextlib import contextmanager
from enum import Enum
import logging
from logging import CRITICAL, Formatter, NOTSET, StreamHandler, WARN, getLogger
import os
import signal
import sys

from .compat import StringIO, iteritems
from .constants import NULL
from .._vendor.auxlib.logz import NullHandler

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
def replace_log_streams():
    # replace the logger stream handlers with stdout and stderr handlers
    stdout_logger, stderr_logger = getLogger('stdout'), getLogger('stderr')
    saved_stdout_strm = stdout_logger.handlers[0].stream
    saved_stderr_strm = stderr_logger.handlers[0].stream
    stdout_logger.handlers[0].stream = sys.stdout
    stderr_logger.handlers[0].stream = sys.stderr
    try:
        yield
    finally:
        # replace the original streams
        stdout_logger.handlers[0].stream = saved_stdout_strm
        stderr_logger.handlers[0].stream = saved_stderr_strm


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
    _hndlrs, _lvl, _dsbld, _prpgt = logr.handlers, logr.level, logr.disabled, logr.propagate
    with _logger_lock():
        logr.addHandler(NullHandler())
        logr.setLevel(CRITICAL + 1)
        logr.disabled, logr.propagate = True, False
    try:
        yield
    finally:
        with _logger_lock():
            logr.handlers, logr.level, logr.disabled = _hndlrs, _lvl, _dsbld
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
    default_return = kwargs.pop('default_return', None)

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
    except (TimeoutException,  KeyboardInterrupt):
        return default_return
