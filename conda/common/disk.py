# -*- coding: utf-8 -*-
# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import absolute_import, division, print_function, unicode_literals

from contextlib import contextmanager
from os import unlink
from tempfile import NamedTemporaryFile


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
