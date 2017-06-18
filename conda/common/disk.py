# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from contextlib import contextmanager
from logging import getLogger
from os import W_OK, access, chmod, getpid, listdir, lstat, makedirs, rename, unlink, walk
from os.path import abspath, basename, dirname, isdir, join, lexists
from shutil import rmtree
from stat import S_IEXEC, S_IMODE, S_ISDIR, S_ISLNK, S_ISREG, S_IWRITE
from time import sleep
from uuid import uuid4

from ..compat import lchmod, text_type
from ..utils import on_win

__all__ = ["rm_rf", "exp_backoff_fn", "try_write"]

log = getLogger(__name__)


def conda_bld_ensure_dir(path):
    # this can fail in parallel operation, depending on timing.  Just try to make the dir,
    #    but don't bail if fail.
    if not isdir(path):
        try:
            makedirs(path)
        except OSError:  # pragma: no cover
            pass


@contextmanager
def temporary_content_in_file(content, suffix=""):
    # content returns temporary file path with contents
    fh = None
    path = None
    try:
        path = abspath(path)
        log.debug("rm_rf %s", path)
        if isdir(path):
            try:
                # On Windows, always move to trash first.
                if trash and on_win:
                    move_result = move_path_to_trash(path, preclean=False)
                    if move_result:
                        return True
                backoff_rmdir(path)
            finally:
                from conda.install import delete_linked_data_any
                delete_linked_data_any(path)
        if lexists(path):
            try:
                backoff_unlink(path)
                return True
            except (OSError, IOError) as e:
                log.debug("%r errno %d\nCannot unlink %s.", e, e.errno, path)
                if trash:
                    move_result = move_path_to_trash(path)
                    if move_result:
                        return True
                log.info("Failed to remove %s.", path)

        else:
            log.debug("rm_rf failed. Not a link, file, or directory: %s", path)
        return True
    finally:
        if fh is not None:
            fh.close()
        if path is not None:
            unlink(path)
