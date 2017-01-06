# -*- coding: utf-8 -*-
# Try to keep compat small because it's imported by everything
# What is compat, and what isn't?
# If a piece of code is "general" and used in multiple modules, it goes here.
# If it's only used in one module, keep it in that module, preferably near the top.
from __future__ import absolute_import, division, print_function, unicode_literals

from itertools import chain
from operator import methodcaller
from os import chmod, lstat
from os.path import islink
import sys

on_win = bool(sys.platform == "win32")

from ..compat import *  # NOQA


def ensure_binary(value):
    return value.encode('utf-8') if hasattr(value, 'encode') else value


def ensure_text_type(value):
    return value.decode('utf-8') if hasattr(value, 'decode') else value
