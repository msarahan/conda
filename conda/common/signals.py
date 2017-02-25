# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from contextlib import contextmanager
import signal

from .compat import iteritems

INTERRUPT_SIGNALS = (
    'SIGABRT',
    'SIGINT',
    'SIGTERM',
    'SIGQUIT',
    'SIGBREAK',
)


def get_signal_name(signum):
    """
    Examples:
        >>> from signal import SIGINT
        >>> get_signal_name(SIGINT)
        'SIGINT'

    """
    return next((k for k, v in iteritems(signal.__dict__)
                 if v == signum and k.startswith('SIG') and not k.startswith('SIG_')),
                None)


@contextmanager
def signal_handler(handler):
    previous_handlers = []
    for signame in INTERRUPT_SIGNALS:
        sig = getattr(signal, signame, None)
        if sig:
            prev_handler = signal.signal(sig, handler)
            previous_handlers.append((sig, prev_handler))
    try:
        yield
    finally:
        for sig, previous_handler in previous_handlers:
            signal.signal(sig, previous_handler)
