# -*- coding: utf-8 -*-
# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
""" This module contains:
  * all low-level code for extracting, linking and unlinking packages
  * a very simple CLI

These API functions have argument names referring to:

    dist:        canonical package name (e.g. 'numpy-1.6.2-py26_0')

    pkgs_dir:    the "packages directory" (e.g. '/opt/anaconda/pkgs' or
                 '/home/joe/envs/.pkgs')

    prefix:      the prefix of a particular environment, which may also
                 be the "default" environment (i.e. sys.prefix),
                 but is otherwise something like '/opt/anaconda/envs/foo',
                 or even any prefix, e.g. '/home/joe/myenv'
"""
from __future__ import absolute_import, division, print_function, unicode_literals

from collections import namedtuple
import errno
import functools
from itertools import chain
import json
import logging
import os
from os import chmod, makedirs, stat
from os.path import dirname, isdir, isfile, join, normcase, normpath
import sys

from .base.constants import PREFIX_PLACEHOLDER
from .common.compat import itervalues, on_win, open, iteritems
from .gateways.disk.delete import delete_trash, move_path_to_trash, rm_rf
from .models.dist import Dist
from .models.enums import PackageType
from .models.match_spec import MatchSpec

delete_trash, move_path_to_trash = delete_trash, move_path_to_trash
from .core.package_cache_data import rm_fetched, PackageCacheData  # NOQA
rm_fetched = rm_fetched

log = logging.getLogger(__name__)


# backwards compatibility for conda-build
prefix_placeholder = PREFIX_PLACEHOLDER


# backwards compatibility for conda-build
def package_cache():
    class package_cache(object):

        def __contains__(self, dist):
            return bool(PackageCacheData.first_writable().get(Dist(dist).to_package_ref(), None))

        def keys(self):
            return (Dist(v) for v in itervalues(PackageCacheData.first_writable()))

        def __delitem__(self, dist):
            PackageCacheData.first_writable().remove(Dist(dist).to_package_ref())

    return package_cache()


if on_win:  # pragma: no cover
    def win_conda_bat_redirect(src, dst, shell):
        """Special function for Windows XP where the `CreateSymbolicLink`
        function is not available.

        Simply creates a `.bat` file at `dst` which calls `src` together with
        all command line arguments.

        Works of course only with callable files, e.g. `.bat` or `.exe` files.
        """
        from .utils import shells
        try:
            makedirs(dirname(dst))
        except OSError as exc:  # Python >2.5
            if exc.errno == EEXIST and isdir(dirname(dst)):
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
            os.chmod(dst, mode)

log = logging.getLogger(__name__)
stdoutlog = logging.getLogger('stdoutlog')


SHEBANG_REGEX = re.compile(br'^(#!((?:\\ |[^ \n\r])+)(.*))')


class FileMode(Enum):
    text = 'text'
    binary = 'binary'

    def __str__(self):
        return "%s" % self.value


LINK_HARD = 1
LINK_SOFT = 2
LINK_COPY = 3
link_name_map = {
    LINK_HARD: 'hard-link',
    LINK_SOFT: 'soft-link',
    LINK_COPY: 'copy',
}

def _link(src, dst, linktype=LINK_HARD):
    if linktype == LINK_HARD:
        if on_win:
            win_hard_link(src, dst)
        else:
            os.link(src, dst)
    elif linktype == LINK_SOFT:
        if on_win:
            win_soft_link(src, dst)
        else:
            os.symlink(src, dst)
    elif linktype == LINK_COPY:
        # copy relative symlinks as symlinks
        if not on_win and islink(src) and not os.readlink(src).startswith('/'):
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst)
    else:
        raise CondaError("Did not expect linktype=%r" % linktype)


def _remove_readonly(func, path, excinfo):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def warn_failed_remove(function, path, exc_info):
    if exc_info[1].errno == errno.EACCES:
        log.warn("Cannot remove, permission denied: {0}".format(path))
    elif exc_info[1].errno == errno.ENOTEMPTY:
        log.warn("Cannot remove, not empty: {0}".format(path))
    else:
        log.warn("Cannot remove, unknown reason: {0}".format(path))


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


PREFIX_PLACEHOLDER = ('/opt/anaconda1anaconda2'
                      # this is intentionally split into parts,
                      # such that running this program on itself
                      # will leave it unchanged
                      'anaconda3')

# backwards compatibility for conda-build
prefix_placeholder = PREFIX_PLACEHOLDER


def read_has_prefix(path):
    """
    reads `has_prefix` file and return dict mapping filepaths to tuples(placeholder, FileMode)

    A line in `has_prefix` contains one of
      * filepath
      * placeholder mode filepath

    mode values are one of
      * text
      * binary
    """
    ParseResult = namedtuple('ParseResult', ('placeholder', 'filemode', 'filepath'))

    def parse_line(line):
        # placeholder, filemode, filepath
        parts = tuple(x.strip('"\'') for x in shlex.split(line, posix=False))
        if len(parts) == 1:
            return ParseResult(PREFIX_PLACEHOLDER, FileMode.text, parts[0])
        elif len(parts) == 3:
            return ParseResult(parts[0], FileMode(parts[1]), parts[2])
        else:
            raise RuntimeError("Invalid has_prefix file at path: %s" % path)
    parsed_lines = (parse_line(line) for line in yield_lines(path))
    return {pr.filepath: (pr.placeholder, pr.filemode) for pr in parsed_lines}


class _PaddingError(Exception):
    pass


def binary_replace(data, a, b):
    """
    Perform a binary replacement of `data`, where the placeholder `a` is
    replaced with `b` and the remaining string is padded with null characters.
    All input arguments are expected to be bytes objects.
    """
    if on_win:
        if has_pyzzer_entry_point(data):
            return replace_pyzzer_entry_point_shebang(data, a, b)
        # currently we should skip replacement on Windows for things we don't understand.
        else:
            return data

    def replace(match):
        occurances = match.group().count(a)
        padding = (len(a) - len(b))*occurances
        if padding < 0:
            raise _PaddingError
        return match.group().replace(a, b) + b'\0' * padding

    original_data_len = len(data)
    pat = re.compile(re.escape(a) + b'([^\0]*?)\0')
    data = pat.sub(replace, data)
    assert len(data) == original_data_len

    return data


def replace_long_shebang(mode, data):
    if mode == FileMode.text:
        shebang_match = SHEBANG_REGEX.match(data)
        if shebang_match:
            whole_shebang, executable, options = shebang_match.groups()
            if len(whole_shebang) > 127:
                executable_name = executable.decode(UTF8).split('/')[-1]
                new_shebang = '#!/usr/bin/env %s%s' % (executable_name, options.decode(UTF8))
                data = data.replace(whole_shebang, new_shebang.encode(UTF8))
    else:
        # TODO: binary shebangs exist; figure this out in the future if text works well
        log.debug("TODO: binary shebangs exist; figure this out in the future if text works well")
    return data


def has_pyzzer_entry_point(data):
    pos = data.rfind(b'PK\x05\x06')
    return pos >= 0


def replace_pyzzer_entry_point_shebang(all_data, placeholder, new_prefix):
    """Code adapted from pyzzer.  This is meant to deal with entry point exe's created by distlib,
    which consist of a launcher, then a shebang, then a zip archive of the entry point code to run.
    We need to change the shebang.
    https://bitbucket.org/vinay.sajip/pyzzer/src/5d5740cb04308f067d5844a56fbe91e7a27efccc/pyzzer/__init__.py?at=default&fileviewer=file-view-default#__init__.py-112  # NOQA
    """
    # Copyright (c) 2013 Vinay Sajip.
    #
    # Permission is hereby granted, free of charge, to any person obtaining a copy
    # of this software and associated documentation files (the "Software"), to deal
    # in the Software without restriction, including without limitation the rights
    # to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    # copies of the Software, and to permit persons to whom the Software is
    # furnished to do so, subject to the following conditions:
    #
    # The above copyright notice and this permission notice shall be included in
    # all copies or substantial portions of the Software.
    #
    # THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    # IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    # FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    # AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    # LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    # OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
    # THE SOFTWARE.
    launcher = shebang = None
    pos = all_data.rfind(b'PK\x05\x06')
    if pos >= 0:
        end_cdr = all_data[pos + 12:pos + 20]
        cdr_size, cdr_offset = struct.unpack('<LL', end_cdr)
        arc_pos = pos - cdr_size - cdr_offset
        data = all_data[arc_pos:]
        if arc_pos > 0:
            pos = all_data.rfind(b'#!', 0, arc_pos)
            if pos >= 0:
                shebang = all_data[pos:arc_pos]
                if pos > 0:
                    launcher = all_data[:pos]

        if data and shebang and launcher:
            if hasattr(placeholder, 'encode'):
                placeholder = placeholder.encode('utf-8')
            if hasattr(new_prefix, 'encode'):
                new_prefix = new_prefix.encode('utf-8')
            shebang = shebang.replace(placeholder, new_prefix)
            all_data = b"".join([launcher, shebang, data])
    return all_data


def replace_prefix(mode, data, placeholder, new_prefix):
    if mode == FileMode.text:
        data = data.replace(placeholder.encode(UTF8), new_prefix.encode(UTF8))
    elif mode == FileMode.binary:
        data = binary_replace(data, placeholder.encode(UTF8), new_prefix.encode(UTF8))
    else:
        raise RuntimeError("Invalid mode: %r" % mode)
    return data


def update_prefix(path, new_prefix, placeholder=PREFIX_PLACEHOLDER, mode=FileMode.text):
    if on_win and mode == FileMode.text:
        # force all prefix replacements to forward slashes to simplify need to escape backslashes
        # replace with unix-style path separators
        new_prefix = new_prefix.replace('\\', '/')

    path = os.path.realpath(path)
    with open(path, 'rb') as fi:
        original_data = data = fi.read()

    data = replace_prefix(mode, data, placeholder, new_prefix)
    if not on_win:
        data = replace_long_shebang(mode, data)

    if data == original_data:
        return
    st = os.lstat(path)
    with exp_backoff_fn(open, path, 'wb') as fo:
        fo.write(data)
    os.chmod(path, stat.S_IMODE(st.st_mode))


def dist2pair(dist):
    dist = str(dist)
    if dist.endswith(']'):
        dist = dist.split('[', 1)[0]
    if dist.endswith('.tar.bz2'):
        dist = dist[:-8]
    parts = dist.split('::', 1)
    return 'defaults' if len(parts) < 2 else parts[0], parts[-1]


def dist2quad(dist):
    channel, dist = dist2pair(dist)
    parts = dist.rsplit('-', 2) + ['', '']
    return (str(parts[0]), str(parts[1]), str(parts[2]), str(channel))


def dist2name(dist):
    return dist2quad(dist)[0]


def name_dist(dist):
    return dist2name(dist)


def dist2filename(dist, suffix='.tar.bz2'):
    return dist2pair(dist)[1] + suffix


def dist2dirname(dist):
    return dist2filename(dist, '')


def create_meta(prefix, dist, info_dir, extra_info):
    """
    Create the conda metadata, in a given prefix, for a given package.
    """
    # read info/index.json first
    with open(join(info_dir, 'index.json')) as fi:
        meta = json.load(fi)
    # add extra info, add to our intenral cache
    meta.update(extra_info)
    if not meta.get('url'):
        meta['url'] = read_url(dist)
    # write into <env>/conda-meta/<dist>.json
    meta_dir = join(prefix, 'conda-meta')
    if not isdir(meta_dir):
        os.makedirs(meta_dir)
    with open(join(meta_dir, dist2filename(dist, '.json')), 'w') as fo:
        json.dump(meta, fo, indent=2, sort_keys=True)
    if prefix in linked_data_:
        load_linked_data(prefix, dist, meta)


def mk_menus(prefix, files, remove=False):
    """
    Create cross-platform menu items (e.g. Windows Start Menu)

    Passes all menu config files %PREFIX%/Menu/*.json to ``menuinst.install``.
    ``remove=True`` will remove the menu items.
    """
    menu_files = [f for f in files
                  if (f.lower().startswith('menu/') and
                      f.lower().endswith('.json'))]
    if not menu_files:
        return
    elif basename(abspath(prefix)).startswith('_'):
        logging.warn("Environment name starts with underscore '_'.  "
                     "Skipping menu installation.")
        return

    try:
        import menuinst
    except:
        logging.warn("Menuinst could not be imported:")
        logging.warn(traceback.format_exc())
        return

    for f in menu_files:
        try:
            menuinst.install(join(prefix, f), remove, prefix)
        except:
            stdoutlog.error("menuinst Exception:")
            stdoutlog.error(traceback.format_exc())


def run_script(prefix, dist, action='post-link', env_prefix=None):
    """
    call the post-link (or pre-unlink) script, and return True on success,
    False on failure
    """
    path = join(prefix, 'Scripts' if on_win else 'bin', '.%s-%s.%s' % (
            name_dist(dist),
            action,
            'bat' if on_win else 'sh'))
    if not isfile(path):
        return True
    if on_win:
        try:
            args = [os.environ['COMSPEC'], '/c', path]
        except KeyError:
            return False
    else:
        shell_path = '/bin/sh' if 'bsd' in sys.platform else '/bin/bash'
        args = [shell_path, path]
    env = os.environ.copy()
    env[str('ROOT_PREFIX')] = sys.prefix
    env[str('PREFIX')] = str(env_prefix or prefix)
    env[str('PKG_NAME')], env[str('PKG_VERSION')], env[str('PKG_BUILDNUM')], _ = dist2quad(dist)
    if action == 'pre-link':
        is_old_noarch = False
        try:
            with open(path) as f:
                script_text = ensure_text_type(f.read())
            if "This is code that is added to noarch Python packages." in script_text:
                is_old_noarch = True
        except Exception as e:
            import traceback
            log.debug(e)
            log.debug(traceback.format_exc())

        if not is_old_noarch:
            sys.stderr.write("""
    Package %s uses a pre-link script. Pre-link scripts are potentially dangerous  discouraged.
    This is because pre-link scripts have the ability to change the package contents in the
    package cache, and therefore modify the underlying files for already-created conda
    environments.  Future versions of conda may deprecate and ignore pre-link scripts.\n""" % dist)

        env[str('SOURCE_DIR')] = str(prefix)
    try:
        subprocess.check_call(args, env=env)
    except subprocess.CalledProcessError:
        return False
    return True


def read_url(dist):
    res = package_cache().get(dist, {}).get('urls', (None,))
    return res[0] if res else None


def read_icondata(source_dir):
    import base64

    try:
        data = open(join(source_dir, 'info', 'icon.png'), 'rb').read()
        return base64.b64encode(data).decode(UTF8)
    except IOError:
        pass
    return None


def read_no_link(info_dir):
    return set(chain(yield_lines(join(info_dir, 'no_link')),
                     yield_lines(join(info_dir, 'no_softlink'))))


# Should this be an API function?
def symlink_conda(prefix, root_dir, shell=None):  # pragma: no cover
    # do not symlink root env - this clobbers activate incorrectly.
    # prefix should always be longer than, or outside the root dir.
    if normcase(normpath(prefix)) in normcase(normpath(root_dir)):
        return
    if on_win:
        where = 'Scripts'
        symlink_fn = functools.partial(win_conda_bat_redirect, shell=shell)
    else:
        where = 'bin'
        symlink_fn = os.symlink
    if not isdir(join(prefix, where)):
        os.makedirs(join(prefix, where))
    symlink_conda_hlp(prefix, root_dir, where, symlink_fn)


def symlink_conda_hlp(prefix, root_dir, where, symlink_fn):  # pragma: no cover
    scripts = ["conda", "activate", "deactivate"]
    prefix_where = join(prefix, where)
    if not isdir(prefix_where):
        os.makedirs(prefix_where)
    for f in scripts:
        root_file = join(root_dir, where, f)
        prefix_file = join(prefix_where, f)
        try:
            # try to kill stale links if they exist
            if os.path.lexists(prefix_file):
                rm_rf(prefix_file)
            # if they're in use, they won't be killed.  Skip making new symlink.
            if not os.path.lexists(prefix_file):
                symlink_fn(root_file, prefix_file)
        except (IOError, OSError) as e:
            if (os.path.lexists(prefix_file) and
                    e.errno in (errno.EPERM, errno.EACCES, errno.EROFS, errno.EEXIST)):
                log.debug("Cannot symlink {0} to {1}. Ignoring since link already exists."
                          .format(root_file, prefix_file))
            elif e.errno == errno.ENOENT:
                log.debug("Problem with symlink management {0} {1}. File may have been removed by "
                          "another concurrent process." .format(root_file, prefix_file))
            elif e.errno == errno.EEXIST:
                log.debug("Problem with symlink management {0} {1}. File may have been created by "
                          "another concurrent process." .format(root_file, prefix_file))
            else:
                raise


def linked_data(prefix, ignore_channels=False):
    """
    Return a dictionary of the linked packages in prefix.
    """
    from .core.prefix_data import PrefixData
    from .models.dist import Dist
    pd = PrefixData(prefix)
    return {Dist(prefix_record): prefix_record for prefix_record in itervalues(pd._prefix_records)}


def linked(prefix, ignore_channels=False):
    """
    Return the Dists of linked packages in prefix.
    """
    conda_package_types = PackageType.conda_package_types()
    ld = iteritems(linked_data(prefix, ignore_channels=ignore_channels))
    return set(dist for dist, prefix_rec in ld if prefix_rec.package_type in conda_package_types)


# exports
def is_linked(prefix, dist):
    """
    Return the install metadata for a linked package in a prefix, or None
    if the package is not linked in the prefix.
    """
    # FIXME Functions that begin with `is_` should return True/False
    from .core.prefix_data import PrefixData
    pd = PrefixData(prefix)
    prefix_record = pd.get(dist.name, None)
    if prefix_record is None:
        return None
    elif MatchSpec(dist).match(prefix_record):
        return prefix_record
    else:
        return None


print("WARNING: The conda.install module is deprecated and will be removed in a future release.",
      file=sys.stderr)
