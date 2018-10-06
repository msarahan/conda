# -*- coding: utf-8 -*-
"""
This file should hold most string literals and magic numbers used throughout the code base.
The exception is if a literal is specifically meant to be private to and isolated within a module.
Think of this as a "more static" source of configuration information.

Another important source of "static" configuration is conda/models/enums.py.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import sys

on_win = bool(sys.platform == "win32")
PREFIX_PLACEHOLDER = '/opt/anaconda1anaconda2anaconda3'

machine_bits = 8 * tuple.__itemsize__

APP_NAME = 'conda'

SEARCH_PATH = (
    '/etc/conda/condarc',
    '/etc/conda/condarc.d/',
    '/var/lib/conda/condarc',
    '/var/lib/conda/condarc.d/',
    '$CONDA_ROOT/condarc',
    '$CONDA_ROOT/.condarc',
    '$CONDA_ROOT/condarc.d/',
    '~/.conda/condarc',
    '~/.conda/condarc.d/',
    '~/.condarc',
    '$CONDA_PREFIX/.condarc',
    '$CONDA_PREFIX/condarc.d/',
    '$CONDARC',
)

DEFAULT_CHANNEL_ALIAS = 'https://conda.anaconda.org'
CONDA_HOMEPAGE_URL = 'http://conda.pydata.org'

PLATFORM_DIRECTORIES = ("linux-64",
                        "linux-32",
                        "win-64",
                        "win-32",
                        "osx-64",
                        "linux-ppc64le",
                        "linux-armv6l",
                        "linux-armv7l",
                        "noarch",
                        )

RECOGNIZED_URL_SCHEMES = ('http', 'https', 'ftp', 's3', 'file')

DEFAULT_CHANNELS_UNIX = ('https://repo.continuum.io/pkgs/free',
                         'https://repo.continuum.io/pkgs/r',
                         'https://repo.continuum.io/pkgs/pro',
                         )

DEFAULT_CHANNELS_WIN = ('https://repo.continuum.io/pkgs/free',
                        'https://repo.continuum.io/pkgs/r',
                        'https://repo.continuum.io/pkgs/pro',
                        'https://repo.continuum.io/pkgs/msys2',
                        )

DEFAULT_CHANNELS = DEFAULT_CHANNELS_WIN if on_win else DEFAULT_CHANNELS_UNIX

ROOT_ENV_NAME = 'root'

UTF8 = 'UTF-8'
ROOT_NO_RM = (
    'python',
    'pycosat',
    'ruamel_yaml',
    'conda',
    'openssl',
    'requests',
)
