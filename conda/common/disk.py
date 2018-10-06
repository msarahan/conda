# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

import errno
import shutil
import sys
from conda import CondaError
from errno import EACCES, EEXIST, ENOENT, EPERM
from itertools import chain
from logging import getLogger
from os import (W_OK, access, chmod, getpid, link as os_link, listdir, lstat, makedirs, readlink,
                rename, symlink, unlink, walk, stat)
from os.path import abspath, basename, dirname, isdir, isfile, islink, join, lexists
from shutil import rmtree
from stat import S_IEXEC, S_IMODE, S_ISDIR, S_ISLNK, S_ISREG, S_IWRITE
from time import sleep
from uuid import uuid4

from ..compat import lchmod, text_type
from ..exceptions import CondaOSError
from ..utils import on_win

__all__ = ["rm_rf", "exp_backoff_fn", "try_write"]

log = getLogger(__name__)


def try_write(dir_path, heavy=False):
    """Test write access to a directory.

    Args:
        dir_path (str): directory to test write access
        heavy (bool): Actually create and delete a file, or do a faster os.access test.
           https://docs.python.org/dev/library/os.html?highlight=xattr#os.access

    Returns:
        bool

    """
    if not isdir(dir_path):
        return False
    if on_win or heavy:
        # try to create a file to see if `dir_path` is writable, see #2151
        temp_filename = join(dir_path, '.conda-try-write-%d' % getpid())
        try:
            with open(temp_filename, mode='wb') as fo:
                fo.write(b'This is a test file.\n')
            backoff_unlink(temp_filename)
            return True
        except (IOError, OSError):
            return False
        finally:
            backoff_unlink(temp_filename)
    else:
        return access(dir_path, W_OK)


def conda_bld_ensure_dir(path):
    # this can fail in parallel operation, depending on timing.  Just try to make the dir,
    #    but don't bail if fail.
    if not isdir(path):
        try:
            makedirs(path)
        except OSError:
            pass


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


def make_writable(path):
    try:
        mode = lstat(path).st_mode
        if S_ISDIR(mode):
            chmod(path, S_IMODE(mode) | S_IWRITE | S_IEXEC)
        elif S_ISREG(mode):
            chmod(path, S_IMODE(mode) | S_IWRITE)
        elif S_ISLNK(mode):
            lchmod(path, S_IMODE(mode) | S_IWRITE)
        else:
            log.debug("path cannot be made writable: %s", path)
    except Exception as e:
        eno = getattr(e, 'errno', None)
        if eno in (ENOENT,):
            log.debug("tried to make writable, but didn't exist: %s", path)
            raise
        elif eno in (EACCES, EPERM):
            log.debug("tried make writable but failed: %s\n%r", path, e)
        else:
            log.warn("Error making path writable: %s\n%r", path, e)
            raise


def recursive_make_writable(path):
    # The need for this function was pointed out at
    #   https://github.com/conda/conda/issues/3266#issuecomment-239241915
    # Especially on windows, file removal will often fail because it is marked read-only
    if isdir(path):
        for root, dirs, files in walk(path):
            for path in chain.from_iterable((files, dirs)):
                try:
                    exp_backoff_fn(make_writable, join(root, path))
                except (IOError, OSError) as e:
                    if e.errno == ENOENT:
                        log.debug("no such file or directory: %s", path)
                    else:
                        raise
    else:
        exp_backoff_fn(make_writable, path)


def exp_backoff_fn(fn, *args, **kwargs):
    """Mostly for retrying file operations that fail on Windows due to virus scanners"""
    if not on_win:
        return fn(*args, **kwargs)

    import random
    # with max_tries = 6, max total time ~= 3.2 sec
    # with max_tries = 7, max total time ~= 6.5 sec
    max_tries = 7
    for n in range(max_tries):
        try:
            result = fn(*args, **kwargs)
        except (OSError, IOError) as e:
            log.debug(repr(e))
            if e.errno in (EPERM, EACCES):
                if n == max_tries-1:
                    raise
                sleep_time = ((2 ** n) + random.random()) * 0.1
                caller_frame = sys._getframe(1)
                log.debug("retrying %s/%s %s() in %g sec",
                          basename(caller_frame.f_code.co_filename),
                          caller_frame.f_lineno, fn.__name__,
                          sleep_time)
                sleep(sleep_time)
            elif e.errno in (ENOENT,):
                # errno.ENOENT File not found error / No such file or directory
                raise
            else:
                log.warn("Uncaught backoff with errno %d", e.errno)
                raise
        else:
            return result


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
        if lexists(path):
            log.info("rm_rf failed for %s", path)
            return False


def delete_trash(prefix=None):
    from ..base.context import context
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
    from ..base.context import context
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
            from ..core.linked_data import delete_prefix_from_linked_data
            delete_prefix_from_linked_data(path)
            return True

    return False


def yield_lines(path):
    """Generator function for lines in file.  Empty generator if path does not exist.

    Args:
        path (str): path to file

    Returns:
        iterator: each line in file, not starting with '#'

    """
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                yield line
    except (IOError, OSError) as e:
        if e.errno == errno.ENOENT:
            raise StopIteration
        else:
            raise










if on_win:
    import ctypes
    from ctypes import wintypes

    CreateHardLink = ctypes.windll.kernel32.CreateHardLinkW
    CreateHardLink.restype = wintypes.BOOL
    CreateHardLink.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                               wintypes.LPVOID]
    try:
        CreateSymbolicLink = ctypes.windll.kernel32.CreateSymbolicLinkW
        CreateSymbolicLink.restype = wintypes.BOOL
        CreateSymbolicLink.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                       wintypes.DWORD]
    except AttributeError:
        CreateSymbolicLink = None

    def win_hard_link(src, dst):
        "Equivalent to os.link, using the win32 CreateHardLink call."
        if not CreateHardLink(dst, src, None):
            raise CondaOSError('win32 hard link failed')

    def win_soft_link(src, dst):
        "Equivalent to os.symlink, using the win32 CreateSymbolicLink call."
        if CreateSymbolicLink is None:
            raise CondaOSError('win32 soft link not supported')
        if not CreateSymbolicLink(dst, src, isdir(src)):
            raise CondaOSError('win32 soft link failed')

    def win_conda_bat_redirect(src, dst, shell):
        """Special function for Windows XP where the `CreateSymbolicLink`
        function is not available.

        Simply creates a `.bat` file at `dst` which calls `src` together with
        all command line arguments.

        Works of course only with callable files, e.g. `.bat` or `.exe` files.
        """
        from conda.utils import shells
        try:
            makedirs(dirname(dst))
        except OSError as exc:  # Python >2.5
            if exc.errno == errno.EEXIST and isdir(dirname(dst)):
                pass
            else:
                raise

        # bat file redirect
        if not isfile(dst + '.bat'):
            with open(dst + '.bat', 'w') as f:
                f.write('@echo off\ncall "%s" %%*\n' % src)

        # TODO: probably need one here for powershell at some point

        # This one is for bash/cygwin/msys
        # set default shell to bash.exe when not provided, as that's most common
        if not shell:
            shell = "bash.exe"

        # technically these are "links" - but islink doesn't work on win
        if not isfile(dst):
            with open(dst, "w") as f:
                f.write("#!/usr/bin/env bash \n")
                if src.endswith("conda"):
                    f.write('%s "$@"' % shells[shell]['path_to'](src+".exe"))
                else:
                    f.write('source %s "$@"' % shells[shell]['path_to'](src))
            # Make the new file executable
            # http://stackoverflow.com/a/30463972/1170370
            mode = stat(dst).st_mode
            mode |= (mode & 292) >> 2    # copy R bits to X
            chmod(dst, mode)


LINK_HARD = 1
LINK_SOFT = 2
LINK_COPY = 3
link_name_map = {
    LINK_HARD: 'hard-link',
    LINK_SOFT: 'soft-link',
    LINK_COPY: 'copy',
}


def link(src, dst, linktype=LINK_HARD):
    if linktype == LINK_HARD:
        if on_win:
            win_hard_link(src, dst)
        else:
            os_link(src, dst)
    elif linktype == LINK_SOFT:
        if on_win:
            win_soft_link(src, dst)
        else:
            symlink(src, dst)
    elif linktype == LINK_COPY:
        # copy relative symlinks as symlinks
        if not on_win and islink(src) and not readlink(src).startswith('/'):
            symlink(readlink(src), dst)
        else:
            shutil.copy2(src, dst)
    else:
        raise CondaError("Did not expect linktype=%r" % linktype)
