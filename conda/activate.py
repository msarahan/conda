# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from glob import glob
from logging import getLogger
import os
import sys

from os.path import isdir, join, dirname, basename
import re

try:
    from cytoolz.itertoolz import concatv
except ImportError:
    from ._vendor.toolz.itertoolz import concatv  # NOQA

on_win = bool(sys.platform == "win32")
PY2 = sys.version_info[0] == 2
if PY2:  # pragma: py3 no cover
    def iteritems(d, **kw):
        return d.iteritems(**kw)
else:
    def iteritems(d, **kw):
        return iter(d.items(**kw))


log = getLogger(__name__)


# CONDA_DEFAULT_ENV is the shortest representation of how conda recognizes your env.
#    It can be an env name, or a full path.
#    Last date of change: 2016-06-21
# If the string contains / it's a path


# CONDA_PREFIX is always the full path to the activated environment.  It is not set
#    when no environment is active.
# Legacy support: CONDA_DEFAULT_ENV is either env name or full path if given as path.
#    CONDA_PREFIX is always the full path, for consistency.
#    Last date of change: 2016-06-21


# if CONDA_DEFAULT_ENV not in PS1, prepend it with parentheses


# Need to answer 3 questions.
#  1. what is the new state of all environment variables?
#  2. what scripts do I need to run?
#  3. what prompt should I prepend?




# def pathsep(shell):
#     if shell is None:
#         return os.pathsep
#     else:
#         raise NotImplementedError()

pathsep = {
    None: os.pathsep,
}

path_convert = {
    None: lambda path: path,
}

script_extension = {
    None: '.bat' if on_win else '.sh',
}


def deactivate_env_vars(shell=None):
    old_conda_shlvl = int(os.getenv('CONDA_SHLVL', 0))
    new_conda_shlvl = old_conda_shlvl - 1
    old_conda_prefix = os.environ['CONDA_PREFIX']
    new_path = _remove_prefix_from_path(os.environ['PATH'], old_conda_prefix, shell=None)
    deactivate_scripts = glob(join(old_conda_prefix, 'etc', 'conda', 'deactivate.d', '*' + script_extension[shell]))

    if old_conda_shlvl == 1:
        # TODO: warn conda floor
        unset_vars = [
            'CONDA_SHLVL',
            'CONDA_PREFIX',
            'CONDA_DEFAULT_ENV',
            'CONDA_PYTHON_PATH',
            'CONDA_PROMPT_MODIFIER',
        ]
        set_vars = {
            'PATH': new_path,
        }
    elif old_conda_shlvl == 2:
        new_prefix = os.getenv('CONDA_PREFIX_%d' % new_conda_shlvl)
        conda_default_env = basename(new_prefix) if basename(dirname(new_prefix)) == 'envs' else new_prefix
        unset_vars = [
            'CONDA_PREFIX_%d' % new_conda_shlvl,
        ]
        set_vars = {
            'PATH': new_path,
            'CONDA_SHLVL': new_conda_shlvl,
            'CONDA_PREFIX': new_prefix,
            'CONDA_DEFAULT_ENV': conda_default_env,
            'CONDA_PROMPT_MODIFIER': "(%s) " % conda_default_env,
        }
    else:
        raise NotImplementedError()

    set_cmds = ('export %s="%s"' % kv for kv in iteritems(set_vars))
    unset_cmds = ('unset %s' % k for k in unset_vars)
    deactivate_cmds = (('source %s' % s) for s in deactivate_scripts)
    return '\n'.join(concatv(set_cmds, unset_cmds, deactivate_cmds))



def _add_prefix_to_path(old_path, prefix, shell=None):
    return pathsep[shell].join(concatv(
        _get_path_dirs(prefix, shell),
        (old_path,),
    ))


def _get_path_dirs(prefix, shell=None):
    _path_convert = path_convert[shell]
    if on_win:
        yield _path_convert(prefix.rstrip("\\"))
        yield _path_convert(join(prefix, 'Library', 'mingw-w64', 'bin'))
        yield _path_convert(join(prefix, 'Library', 'usr', 'bin'))
        yield _path_convert(join(prefix, 'Library', 'bin'))
        yield _path_convert(join(prefix, 'Scripts'))
    else:
        yield _path_convert(join(prefix, 'bin'))


def _remove_prefix_from_path(current_path, prefix, shell=None):
    _prefix_paths = re.escape(pathsep[shell].join(_get_path_dirs(prefix, shell)))
    return re.sub(_prefix_paths, r'', current_path, 1)


def _replace_prefix_in_path(current_path, old_prefix, new_prefix, shell=None):
    _old_prefix_paths = re.escape(pathsep[shell].join(_get_path_dirs(old_prefix, shell)))
    _new_prefix_paths = re.escape(pathsep[shell].join(_get_path_dirs(new_prefix, shell)))
    return re.sub(_old_prefix_paths, _new_prefix_paths, current_path, 1)



def activate_env_vars(name_or_prefix, shell=None):
    from ._vendor.auxlib.path import expand
    from .base.context import context, locate_prefix_by_name
    if isdir(expand(name_or_prefix)):
        prefix = name_or_prefix
    elif re.search(r'\\|/', name_or_prefix):
        prefix = name_or_prefix
    else:
        prefix = locate_prefix_by_name(context, name_or_prefix)
    conda_default_env = basename(prefix) if basename(dirname(prefix)) == 'envs' else prefix

    old_conda_shlvl = int(os.getenv('CONDA_SHLVL', 0))
    old_conda_prefix = os.getenv('CONDA_PREFIX')
    old_path = os.environ['PATH']


    set_vars = {}
    deactivate_scripts = []
    activate_scripts = glob(join(prefix, 'etc', 'conda', 'activate.d', '*' + script_extension[shell]))

    if old_conda_shlvl == 0:
        set_vars.update({
            'CONDA_PYTHON_PATH': sys.executable,
            'PATH': _add_prefix_to_path(old_path, prefix, shell),
            'CONDA_PREFIX': prefix,
            'CONDA_SHLVL': old_conda_shlvl + 1,
            'CONDA_DEFAULT_ENV': conda_default_env,
        })
    elif old_conda_shlvl == 1:
        set_vars.update({
            'PATH': _add_prefix_to_path(old_path, prefix, shell),
            'CONDA_PREFIX': prefix,
            'CONDA_PREFIX_%d' % old_conda_shlvl: old_conda_prefix,
            'CONDA_SHLVL': old_conda_shlvl + 1,
            'CONDA_DEFAULT_ENV': conda_default_env,
        })
    elif old_conda_shlvl == 2:
        new_path = _replace_prefix_in_path(old_path, old_conda_prefix, prefix, shell)
        set_vars.update({
            'PATH': new_path,
            'CONDA_PREFIX': prefix,
            'CONDA_DEFAULT_ENV': conda_default_env,
        })
        deactivate_scripts = glob(join(old_conda_prefix, 'etc', 'conda', 'deactivate.d', '*' + script_extension[shell]))
    else:
        raise NotImplementedError()

    if context.changeps1:
        set_vars['CONDA_PROMPT_MODIFIER'] = "(%s) " % conda_default_env

    set_cmds = ('export %s="%s"' % kv for kv in iteritems(set_vars))
    deactivate_cmds = (('source %s') % s for s in deactivate_scripts)
    activate_cmds = (('source %s') % s for s in activate_scripts)
    return '\n'.join(concatv(set_cmds, deactivate_cmds, activate_cmds))




if __name__ == '__main__':
    command = sys.argv[1]
    if command == 'activate':
        name_or_prefix = sys.argv[2]
        shell = sys.argv[3] if len(sys.argv) > 3 else None
        print(activate_env_vars(name_or_prefix, shell))
    elif command == 'deactivate':
        shell = sys.argv[2] if len(sys.argv) > 2 else None
        print(deactivate_env_vars(shell))
