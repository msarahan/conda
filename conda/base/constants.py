# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

import os
import sys
from logging import getLogger
from platform import machine

from enum import Enum

log = getLogger(__name__)


class Arch(Enum):
    x86 = 'x86'
    x86_64 = 'x86_64'
    armv6l = 'armv6l'
    armv7l = 'armv7l'
    ppc64le = 'ppc64le'

    @classmethod
    def from_sys(cls):
        return cls[machine()]


class Platform(Enum):
    linux = 'linux'
    win = 'win32'
    openbsd = 'openbsd5'
    osx = 'darwin'

    @classmethod
    def from_sys(cls):
        return cls(sys.platform)

machine_bits = 8 * tuple.__itemsize__

UID = os.getuid()
PWD = os.getcwd()
CONDA = 'CONDA'
CONDA_ = 'CONDA_'
conda = 'conda'

SEARCH_PATH = (
    '/etc/conda/condarc',
    '/etc/conda/condarc.d/',
    '/var/lib/conda/condarc',
    '/var/lib/conda/condarc.d/',
    '$CONDA_ROOT/.condarc',
    '$CONDA_ROOT/condarc.d/',
    '$HOME/.conda/condarc',
    '$HOME/.conda/condarc.d/',
    '$HOME/.condarc',
    '$ENV/.condarc',
    '$ENV/.condaenv.yml',
)

DEFAULT_CHANNEL_ALIAS = 'https://conda.anaconda.org/'

PLATFORM_DIRECTORIES = ("linux-64",  "linux-32",
                        "win-64",  "win-32",
                        "osx-64", "noarch")

RECOGNIZED_URL_SCHEMES = ('http', 'https', 'ftp', 's3', 'file')


if Platform.from_sys() is Platform.win:
    DEFAULT_CHANNELS = ('https://repo.continuum.io/pkgs/free',
                        'https://repo.continuum.io/pkgs/pro',
                        'https://repo.continuum.io/pkgs/msys2',
                        )
else:
    DEFAULT_CHANNELS = ('https://repo.continuum.io/pkgs/free',
                        'https://repo.continuum.io/pkgs/pro',
                        )

ROOT_ENV_NAME = 'root'
