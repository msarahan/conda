# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

import os
import uuid
import stat
import pytest
from shutil import rmtree
from contextlib import contextmanager
from tempfile import mkdtemp, gettempdir
from os.path import join, isfile, lexists
from stat import S_IRUSR, S_IRGRP, S_IROTH

from conda.gateways.disk.update import touch
from conda.utils import on_win
from conda.common.compat import text_type
from conda.gateways.disk.permissions import make_writable, recursive_make_writable


@contextmanager
def tempdir():
    tempdirdir = gettempdir()
    dirname = str(uuid.uuid4())[:8]
    prefix = join(tempdirdir, dirname)
    try:
        os.makedirs(prefix)
        yield prefix
    finally:
        if lexists(prefix):
            rmtree(prefix)


def _make_read_only(path):
    os.chmod(path, S_IRUSR | S_IRGRP | S_IROTH)


def _can_write_file(test, content):
    with open(test, 'w+') as fh:
        fh.write(content)
        fh.close()
    if os.stat(test).st_size == 0.0:
        return False
    else:
        return True


def _try_open(path):
    try:
        f = open(path, 'a+')
    except:
        raise
    else:
        f.close()


def test_make_writable():
    with tempdir() as td:
        test_path = join(td, 'test_path')
        touch(test_path)
        assert isfile(test_path)
        _try_open(test_path)
        _make_read_only(test_path)
        pytest.raises((IOError, OSError), _try_open, test_path)
        make_writable(test_path)
        _try_open(test_path)
        assert _can_write_file(test_path, "welcome to the ministry of silly walks")
        os.remove(test_path)
        assert not isfile(test_path)


@pytest.mark.skipif(on_win, reason="Testing case for windows is different then Unix")
def test_recursive_make_writable():
    with tempdir() as td:
        test_path = join(td, 'test_path')
        touch(test_path)
        assert isfile(test_path)
        _try_open(test_path)
        _make_read_only(test_path)
        pytest.raises((IOError, OSError), _try_open, test_path)
        recursive_make_writable(test_path)
        _try_open(test_path)
        assert _can_write_file(test_path, "welcome to the ministry of silly walks")
        os.remove(test_path)
        assert not isfile(test_path)
