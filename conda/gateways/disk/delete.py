# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from errno import EEXIST, ENOENT
from logging import getLogger
from os import listdir, makedirs, rename, unlink, walk
from os.path import abspath, dirname, isdir, isfile, islink, join, lexists
from shutil import rmtree
from uuid import uuid4

from . import MAX_TRIES, exp_backoff_fn
from .permissions import make_writable, recursive_make_writable
from ...base.context import context
from ...common.compat import text_type
from ...utils import on_win

log = getLogger(__name__)


def rm_rf(path, max_retries=5, trash=True):
    """
    Completely delete path
    max_retries is the number of times to retry on failure. The default is 5. This only applies
    to deleting a directory.
    If removing path fails and trash is True, files will be moved to the trash directory.
    """
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
                # If path was removed, ensure it's not in linked_data_
                if islink(path) or isfile(path):
                    from ...core.linked_data import delete_prefix_from_linked_data
                    delete_prefix_from_linked_data(path)
        elif lexists(path):
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
        if lexists(path):
            log.info("rm_rf failed for %s", path)
            return False


def delete_trash(prefix=None):
    for pkg_dir in context.pkgs_dirs:
        trash_dir = join(pkg_dir, '.trash')
        if not lexists(trash_dir):
            log.debug("Trash directory %s doesn't exist. Moving on.", trash_dir)
            continue
        log.debug("removing trash for %s", trash_dir)
        for p in listdir(trash_dir):
            path = join(trash_dir, p)
            try:
                if isdir(path):
                    backoff_rmdir(path)
                else:
                    backoff_unlink(path)
            except (IOError, OSError) as e:
                log.info("Could not delete path in trash dir %s\n%r", path, e)
        if listdir(trash_dir):
            log.info("Unable to clean trash directory %s", trash_dir)


def move_to_trash(prefix, f, tempdir=None):
    """
    Move a file or folder f from prefix to the trash

    tempdir is a deprecated parameter, and will be ignored.

    This function is deprecated in favor of `move_path_to_trash`.
    """
    return move_path_to_trash(join(prefix, f) if f else prefix)


def move_path_to_trash(path, preclean=True):
    """
    Move a path to the trash
    """
    for pkg_dir in context.pkgs_dirs:
        trash_dir = join(pkg_dir, '.trash')

        try:
            makedirs(trash_dir)
        except (IOError, OSError) as e1:
            if e1.errno != EEXIST:
                continue

        trash_file = join(trash_dir, text_type(uuid4()))

        try:
            rename(path, trash_file)
        except (IOError, OSError) as e:
            log.debug("Could not move %s to %s.\n%r", path, trash_file, e)
        else:
            log.debug("Moved to trash: %s", path)
            from ...core.linked_data import delete_prefix_from_linked_data
            delete_prefix_from_linked_data(path)
            return True

    return False


def backoff_unlink(file_or_symlink_path):
    def _unlink(path):
        make_writable(path)
        unlink(path)

    try:
        exp_backoff_fn(lambda f: lexists(f) and _unlink(f), file_or_symlink_path)
    except (IOError, OSError) as e:
        if e.errno not in (ENOENT,):
            # errno.ENOENT File not found error / No such file or directory
            raise


def backoff_rmdir(dirpath):
    if not isdir(dirpath):
        return

    # shutil.rmtree:
    #   if onerror is set, it is called to handle the error with arguments (func, path, exc_info)
    #     where func is os.listdir, os.remove, or os.rmdir;
    #     path is the argument to that function that caused it to fail; and
    #     exc_info is a tuple returned by sys.exc_info() ==> (type, value, traceback).
    def retry(func, path, exc_info):
        if getattr(exc_info[1], 'errno', None) == ENOENT:
            return
        recursive_make_writable(dirname(path))
        func(path)

    def _rmdir(path):
        try:
            recursive_make_writable(path)
            exp_backoff_fn(rmtree, path, onerror=retry)
        except (IOError, OSError) as e:
            if e.errno == ENOENT:
                log.debug("no such file or directory: %s", path)
            else:
                raise

    for root, dirs, files in walk(dirpath, topdown=False):
        for file in files:
            backoff_unlink(join(root, file))
        for dir in dirs:
            _rmdir(join(root, dir))

    _rmdir(dirpath)


def maybe_rmdir_if_empty(dirpath, max_tries=MAX_TRIES):
    if isdir(dirpath) and not listdir(dirpath):
        try:
            log.debug("Attempting to remove directory %s", dirpath)
            backoff_rmdir(dirpath, max_tries=max_tries)
        except (IOError, OSError) as e:
            log.debug("Failed to remove '%s'. %r", dirpath, e)
