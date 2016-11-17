# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from logging import getLogger
from os import makedirs
from os.path import isdir

log = getLogger(__name__)


def conda_bld_ensure_dir(path):
    # this can fail in parallel operation, depending on timing.  Just try to make the dir,
    #    but don't bail if fail.
    if not isdir(path):
        try:
            makedirs(path)
        except OSError:
            pass


MAX_TRIES = 7

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


def backoff_unlink(file_or_symlink_path, max_tries=MAX_TRIES):
    def _unlink(path):
        make_writable(path)
        unlink(path)

    try:
        exp_backoff_fn(lambda f: lexists(f) and _unlink(f), file_or_symlink_path,
                       max_tries=max_tries)
    except (IOError, OSError) as e:
        if e.errno not in (ENOENT,):
            # errno.ENOENT File not found error / No such file or directory
            raise


def backoff_rmdir(dirpath, max_tries=MAX_TRIES):
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
        recursive_make_writable(dirname(path), max_tries=max_tries)
        func(path)

    def _rmdir(path):
        try:
            recursive_make_writable(path)
            exp_backoff_fn(rmtree, path, onerror=retry, max_tries=max_tries)
        except (IOError, OSError) as e:
            if e.errno == ENOENT:
                log.debug("no such file or directory: %s", path)
            else:
                raise

    for root, dirs, files in walk(dirpath, topdown=False):
        for file in files:
            backoff_unlink(join(root, file), max_tries=max_tries)
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


def recursive_make_writable(path, max_tries=MAX_TRIES):
    # The need for this function was pointed out at
    #   https://github.com/conda/conda/issues/3266#issuecomment-239241915
    # Especially on windows, file removal will often fail because it is marked read-only
    if isdir(path):
        for root, dirs, files in walk(path):
            for path in chain.from_iterable((files, dirs)):
                try:
                    exp_backoff_fn(make_writable, join(root, path), max_tries=max_tries)
                except (IOError, OSError) as e:
                    if e.errno == ENOENT:
                        log.debug("no such file or directory: %s", path)
                    else:
                        raise
    else:
        exp_backoff_fn(make_writable, path, max_tries=max_tries)


def exp_backoff_fn(fn, *args, **kwargs):
    """Mostly for retrying file operations that fail on Windows due to virus scanners"""
    max_tries = kwargs.pop('max_tries', MAX_TRIES)
    if not on_win:
        return fn(*args, **kwargs)

    import random
    # with max_tries = 6, max total time ~= 3.2 sec
    # with max_tries = 7, max total time ~= 6.5 sec
    for n in range(max_tries):
        try:
            makedirs(path)
        except OSError:
            pass
