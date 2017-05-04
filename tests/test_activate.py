# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, unicode_literals

from datetime import datetime
import os
from os.path import join, isdir
import sys
from tempfile import gettempdir
from unittest import TestCase
from uuid import uuid4

from conda._vendor.auxlib.ish import dals
import pytest

from conda.base.context import context
from conda.cli.activate import _get_prefix_paths, binpath_from_arg
from conda.compat import TemporaryDirectory
from conda.config import platform, root_dir
from conda.install import symlink_conda
from conda.utils import on_win, shells, translate_stream
from tests.helpers import assert_equals, assert_in, assert_not_in

# ENVS_PREFIX = "envs" if PY2 else "envsßôç"
ENVS_PREFIX = "envs"


def gen_test_env_paths(envs, shell, num_test_folders=5):
    """People need not use all the test folders listed here.
    This is only for shortening the environment string generation.

    Also encapsulates paths in double quotes.
    """
    paths = [os.path.join(envs, "test {}".format(test_folder+1)) for test_folder in range(num_test_folders)]
    for path in paths[:2]:      # Create symlinks ONLY for the first two folders.
        symlink_conda(path, sys.prefix, shell)
    converter = shells[shell]["path_to"]
    paths = [converter(path) for path in paths]
    return paths

def _envpaths(env_root, env_name="", shell=None):
    """Supply the appropriate platform executable folders.  rstrip on root removes
       trailing slash if env_name is empty (the default)

    Assumes that any prefix used here exists.  Will not work on prefixes that don't.
    """
    sep = shells[shell]['sep']
    return binpath_from_arg(sep.join([env_root, env_name]), shell)


PYTHONPATH = os.path.dirname(os.path.dirname(__file__))

# Make sure the subprocess activate calls this python
syspath = os.pathsep.join(_get_prefix_paths(context.root_prefix))

def print_ps1(env_dirs, raw_ps, number):
    return (u"({}) ".format(env_dirs[number]) + raw_ps)


CONDA_ENTRY_POINT = """\
#!{syspath}/python
import sys
from conda.cli import main

sys.exit(main())
"""

def raw_string(s):
    if isinstance(s, str):
        s = s.encode('string-escape')
    elif isinstance(s, unicode):
        s = s.encode('unicode-escape')
    return s

def strip_leading_library_bin(path_string, shelldict):
    entries = path_string.split(shelldict['pathsep'])
    if "library{}bin".format(shelldict['sep']) in entries[0].lower():
        entries = entries[1:]
    return shelldict['pathsep'].join(entries)


def _format_vars(shell):
    shelldict = shells[shell]

    base_path, _ = run_in(shelldict['printpath'], shell)
    # windows forces Library/bin onto PATH when starting up.  Strip it for the purposes of this test.
    if on_win:
        base_path = strip_leading_library_bin(base_path, shelldict)

    raw_ps, _ = run_in(shelldict["printps1"], shell)

    command_setup = """\
{set} PYTHONPATH="{PYTHONPATH}"
{set} CONDARC=
{set} CONDA_PATH_BACKUP=
""".format(here=dirname(__file__), PYTHONPATH=shelldict['path_to'](PYTHONPATH),
           set=shelldict["set_var"])
    if shelldict["shell_suffix"] == '.bat':
        command_setup = "@echo off\n" + command_setup

    return {
        'echo': shelldict['echo'],
        'nul': shelldict['nul'],
        'printpath': shelldict['printpath'],
        'printdefaultenv': shelldict['printdefaultenv'],
        'printps1': shelldict['printps1'],
        'raw_ps': raw_ps,
        'set_var': shelldict['set_var'],
        'source': shelldict['source_setup'],
        'binpath': shelldict['binpath'],
        'shell_suffix': shelldict['shell_suffix'],
        'syspath': shelldict['path_to'](sys.prefix),
        'binpath': shelldict['binpath'],
        'command_setup': command_setup,
        'base_path': base_path,
}


@pytest.fixture(scope="module")
def bash_profile(request):
    profile=os.path.join(os.path.expanduser("~"), ".bash_profile")
    if os.path.isfile(profile):
        os.rename(profile, profile+"_backup")
    with open(profile, "w") as f:
        f.write("export PS1=test_ps1\n")
        f.write("export PROMPT=test_ps1\n")
    def fin():
        if os.path.isfile(profile+"_backup"):
            os.remove(profile)
            os.rename(profile+"_backup", profile)
    request.addfinalizer(fin)
    return request  # provide the fixture value


@pytest.mark.installed
def test_activate_test1(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix=ENVS_PREFIX, dir=dirname(__file__)) as envs:
        commands = (shell_vars['command_setup'] + """
        {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}"
        {printpath}
        """).format(envs=envs, env_dirs=gen_test_env_paths(envs, shell), **shell_vars)

        stdout, stderr = run_in(commands, shell)
        assert_in(shells[shell]['pathsep'].join(_envpaths(envs, 'test 1', shell)),
                 stdout, shell)


@pytest.mark.installed
def test_activate_env_from_env_with_root_activate(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix=ENVS_PREFIX, dir=dirname(__file__)) as envs:
        commands = (shell_vars['command_setup'] + """
        {source} "{syspath}{binpath}activate" "{env_dirs[0]}" {nul}
        {source} "{syspath}{binpath}activate" "{env_dirs[1]}"
        {printpath}
        """).format(envs=envs, env_dirs=gen_test_env_paths(envs, shell), **shell_vars)

        stdout, stderr = run_in(commands, shell)
        assert_in(shells[shell]['pathsep'].join(_envpaths(envs, 'test 2', shell)), stdout)


@pytest.mark.installed
def test_activate_bad_directory(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix=ENVS_PREFIX, dir=dirname(__file__)) as envs:
        env_dirs = gen_test_env_paths(envs, shell)
        # Strange semicolons are here to defeat MSYS' automatic path conversion.
        #   See http://www.mingw.org/wiki/Posix_path_conversion
        commands = (shell_vars['command_setup'] + """
        {source} "{syspath}{binpath}activate" "{env_dirs[2]}"
        {printpath}
        """).format(envs=envs, env_dirs=env_dirs, **shell_vars)
        stdout, stderr = run_in(commands, shell)
        # another semicolon here for comparison reasons with one above.
        assert 'Could not find environment' in stderr
        assert_not_in(env_dirs[2], stdout)


@pytest.mark.installed
def test_activate_bad_env_keeps_existing_good_env(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix=ENVS_PREFIX, dir=dirname(__file__)) as envs:
        commands = (shell_vars['command_setup'] + """
        {source} {syspath}{binpath}activate "{env_dirs[0]}" {nul}
        {source} "{syspath}{binpath}activate" "{env_dirs[2]}"
        {printpath}
        """).format(envs=envs, env_dirs=gen_test_env_paths(envs, shell), **shell_vars)

        stdout, stderr = run_in(commands, shell)
        assert_in(shells[shell]['pathsep'].join(_envpaths(envs, 'test 1', shell)),stdout)


@pytest.mark.installed
def test_activate_deactivate(shell):
    if shell == "bash.exe" and datetime.now() < datetime(2017, 6, 1):
        pytest.xfail("fix this soon")
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix=ENVS_PREFIX, dir=dirname(__file__)) as envs:
        commands = (shell_vars['command_setup'] + """
        {source} "{syspath}{binpath}activate" "{env_dirs[0]}" {nul}
        {source} "{syspath}{binpath}deactivate"
        {printpath}
        """).format(envs=envs, env_dirs=gen_test_env_paths(envs, shell), **shell_vars)

        stdout, stderr = run_in(commands, shell)
        stdout = strip_leading_library_bin(stdout, shells[shell])
        assert_equals(stdout, u"%s" % shell_vars['base_path'])


@pytest.mark.installed
def test_activate_root_simple(shell):
    if shell == "bash.exe" and datetime.now() < datetime(2017, 6, 1):
        pytest.xfail("fix this soon")
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix=ENVS_PREFIX, dir=dirname(__file__)) as envs:
        commands = (shell_vars['command_setup'] + """
        {source} "{syspath}{binpath}activate" root
        {printpath}
        """).format(envs=envs, **shell_vars)

        stdout, stderr = run_in(commands, shell)
        assert_in(shells[shell]['pathsep'].join(_envpaths(root_dir, shell=shell)), stdout, stderr)

        commands = (shell_vars['command_setup'] + """
        {source} "{syspath}{binpath}activate" root
        {source} "{syspath}{binpath}deactivate"
        {printpath}
        """).format(envs=envs, **shell_vars)

        stdout, stderr = run_in(commands, shell)
        stdout = strip_leading_library_bin(stdout, shells[shell])
        assert_equals(stdout, u"%s" % shell_vars['base_path'], stderr)


@pytest.mark.installed
def test_activate_root_env_from_other_env(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix=ENVS_PREFIX, dir=dirname(__file__)) as envs:
        commands = (shell_vars['command_setup'] + """
        {source} "{syspath}{binpath}activate" "{env_dirs[0]}" {nul}
        {source} "{syspath}{binpath}activate" root
        {printpath}
        """).format(envs=envs, env_dirs=gen_test_env_paths(envs, shell), **shell_vars)

        stdout, stderr = run_in(commands, shell)
        assert_in(shells[shell]['pathsep'].join(_envpaths(root_dir, shell=shell)),
                  stdout)
        assert_not_in(shells[shell]['pathsep'].join(_envpaths(envs, 'test 1', shell)), stdout)


@pytest.mark.installed
def test_wrong_args(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix=ENVS_PREFIX, dir=dirname(__file__)) as envs:
        commands = (shell_vars['command_setup'] + """
        {source} "{syspath}{binpath}activate" two args
        {printpath}
        """).format(envs=envs, **shell_vars)

        stdout, stderr = run_in(commands, shell)
        stdout = strip_leading_library_bin(stdout, shells[shell])
        assert_equals(stderr, u'Error: did not expect more than one argument.\n    (got two args)')
        assert_equals(stdout, shell_vars['base_path'], stderr)


@pytest.mark.installed
def test_activate_help(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix=ENVS_PREFIX, dir=dirname(__file__)) as envs:
        if shell not in ['powershell.exe', 'cmd.exe']:
            commands = (shell_vars['command_setup'] + """
            "{syspath}{binpath}activate" Zanzibar
            """).format(envs=envs, **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, '')
            assert_in("activate must be sourced", stderr)
            # assert_in("Usage: source activate ENV", stderr)

        commands = (shell_vars['command_setup'] + """
        {source} "{syspath}{binpath}activate" --help
        """).format(envs=envs, **shell_vars)

        stdout, stderr = run_in(commands, shell)
        assert_equals(stdout, '')

        if shell in ["cmd.exe", "powershell"]:
            # assert_in("Usage: activate ENV", stderr)
            pass
        else:
            assert native_path_to_unix(path1) == path1

        if on_win:
            assert all(assert_unix_path(p) for p in native_path_to_unix(*paths))
        else:
            assert native_path_to_unix(*paths) == paths

    def test_posix_basic(self):
        activator = Activator('posix')
        self.make_dot_d_files(activator.script_extension)

        activate_data = activator.activate(self.prefix)

        new_path_parts = activator._add_prefix_to_path(self.prefix)
        assert activate_data == dals("""
        export CONDA_DEFAULT_ENV="%(prefix)s"
        export CONDA_PREFIX="%(prefix)s"
        export CONDA_PROMPT_MODIFIER="(%(prefix)s) "
        export CONDA_PYTHON_EXE="%(sys_executable)s"
        export CONDA_SHLVL="1"
        export PATH="%(new_path)s"
        . "%(activate1)s"
        """) % {
            'prefix': self.prefix,
            'new_path': activator.pathsep_join(new_path_parts),
            'sys_executable': sys.executable,
            'activate1': join(self.prefix, 'etc', 'conda', 'activate.d', 'activate1.sh')
        }

        with env_var('CONDA_PREFIX', self.prefix):
            with env_var('CONDA_SHLVL', '1'):
                with env_var('PATH', os.pathsep.join(concatv(new_path_parts, (os.environ['PATH'],)))):
                    reactivate_data = activator.reactivate()

                    assert reactivate_data == dals("""
                    . "%(deactivate1)s"
                    . "%(activate1)s"
                    """) % {
                        'activate1': join(self.prefix, 'etc', 'conda', 'activate.d', 'activate1.sh'),
                        'deactivate1': join(self.prefix, 'etc', 'conda', 'deactivate.d', 'deactivate1.sh'),
                    }

                    deactivate_data = activator.deactivate()

                    new_path = activator.pathsep_join(activator._remove_prefix_from_path(self.prefix))
                    assert deactivate_data == dals("""
                    unset CONDA_DEFAULT_ENV
                    unset CONDA_PREFIX
                    unset CONDA_PROMPT_MODIFIER
                    unset CONDA_PYTHON_EXE
                    export CONDA_SHLVL="0"
                    export PATH="%(new_path)s"
                    . "%(deactivate1)s"
                    """) % {
                        'new_path': new_path,
                        'deactivate1': join(self.prefix, 'etc', 'conda', 'deactivate.d', 'deactivate1.sh'),

                    }

    def test_xonsh_basic(self):
        activator = Activator('xonsh')
        self.make_dot_d_files(activator.script_extension)

        activate_result = activator.activate(self.prefix)
        with open(activate_result) as fh:
            activate_data = fh.read()
        rm_rf(activate_result)

        new_path_parts = activator._add_prefix_to_path(self.prefix)
        assert activate_data == dals("""
        $CONDA_DEFAULT_ENV = "%(prefix)s"
        $CONDA_PREFIX = "%(prefix)s"
        $CONDA_PROMPT_MODIFIER = "(%(prefix)s) "
        $CONDA_PYTHON_EXE = "%(sys_executable)s"
        $CONDA_SHLVL = "1"
        $PATH = "%(new_path)s"
        source "%(activate1)s"
        """) % {
            'prefix': self.prefix,
            'new_path': activator.pathsep_join(new_path_parts),
            'sys_executable': sys.executable,
            'activate1': join(self.prefix, 'etc', 'conda', 'activate.d', 'activate1.xsh')
        }

        with env_var('CONDA_PREFIX', self.prefix):
            with env_var('CONDA_SHLVL', '1'):
                with env_var('PATH', os.pathsep.join(concatv(new_path_parts, (os.environ['PATH'],)))):
                    reactivate_result = activator.reactivate()
                    with open(reactivate_result) as fh:
                        reactivate_data = fh.read()
                    rm_rf(reactivate_result)

                    assert reactivate_data == dals("""
                    source "%(deactivate1)s"
                    source "%(activate1)s"
                    """) % {
                        'activate1': join(self.prefix, 'etc', 'conda', 'activate.d', 'activate1.xsh'),
                        'deactivate1': join(self.prefix, 'etc', 'conda', 'deactivate.d', 'deactivate1.xsh'),
                    }

                    deactivate_result = activator.deactivate()
                    with open(deactivate_result) as fh:
                        deactivate_data = fh.read()
                    rm_rf(deactivate_result)

                    new_path = activator.pathsep_join(activator._remove_prefix_from_path(self.prefix))
                    assert deactivate_data == dals("""
                    del $CONDA_DEFAULT_ENV
                    del $CONDA_PREFIX
                    del $CONDA_PROMPT_MODIFIER
                    del $CONDA_PYTHON_EXE
                    $CONDA_SHLVL = "0"
                    $PATH = "%(new_path)s"
                    source "%(deactivate1)s"
                    """) % {
                        'new_path': new_path,
                        'deactivate1': join(self.prefix, 'etc', 'conda', 'deactivate.d', 'deactivate1.xsh'),
                    }

    def test_cmd_exe_basic(self):
        activator = Activator('cmd.exe')
        self.make_dot_d_files(activator.script_extension)

        activate_result = activator.activate(self.prefix)
        with open(activate_result) as fh:
            activate_data = fh.read()
        rm_rf(activate_result)

        new_path_parts = activator._add_prefix_to_path(self.prefix)
        assert activate_data == dals("""
        @SET "CONDA_DEFAULT_ENV=%(prefix)s"
        @SET "CONDA_PREFIX=%(prefix)s"
        @SET "CONDA_PROMPT_MODIFIER=(%(prefix)s) "
        @SET "CONDA_PYTHON_EXE=%(sys_executable)s"
        @SET "CONDA_SHLVL=1"
        @SET "PATH=%(new_path)s"
        @CALL "%(activate1)s"
        """) % {
            'prefix': self.prefix,
            'new_path': activator.pathsep_join(new_path_parts),
            'sys_executable': sys.executable,
            'activate1': join(self.prefix, 'etc', 'conda', 'activate.d', 'activate1.bat')
        }

        with env_var('CONDA_PREFIX', self.prefix):
            with env_var('CONDA_SHLVL', '1'):
                with env_var('PATH', os.pathsep.join(concatv(new_path_parts, (os.environ['PATH'],)))):
                    reactivate_result = activator.reactivate()
                    with open(reactivate_result) as fh:
                        reactivate_data = fh.read()
                    rm_rf(reactivate_result)

                    assert reactivate_data == dals("""
                    @CALL "%(deactivate1)s"
                    @CALL "%(activate1)s"
                    """) % {
                        'activate1': join(self.prefix, 'etc', 'conda', 'activate.d', 'activate1.bat'),
                        'deactivate1': join(self.prefix, 'etc', 'conda', 'deactivate.d', 'deactivate1.bat'),
                    }

                    deactivate_result = activator.deactivate()
                    with open(deactivate_result) as fh:
                        deactivate_data = fh.read()
                    rm_rf(deactivate_result)

                    new_path = activator.pathsep_join(activator._remove_prefix_from_path(self.prefix))
                    assert deactivate_data == dals("""
                    @SET CONDA_DEFAULT_ENV=
                    @SET CONDA_PREFIX=
                    @SET CONDA_PROMPT_MODIFIER=
                    @SET CONDA_PYTHON_EXE=
                    @SET "CONDA_SHLVL=0"
                    @SET "PATH=%(new_path)s"
                    @CALL "%(deactivate1)s"
                    """) % {
                        'new_path': new_path,
                        'deactivate1': join(self.prefix, 'etc', 'conda', 'deactivate.d', 'deactivate1.bat'),
                    }

    def test_fish_basic(self):
        activator = Activator('fish')
        self.make_dot_d_files(activator.script_extension)

        activate_data = activator.activate(self.prefix)

        new_path_parts = activator._add_prefix_to_path(self.prefix)
        assert activate_data == dals("""
        set -gx CONDA_DEFAULT_ENV "%(prefix)s"
        set -gx CONDA_PREFIX "%(prefix)s"
        set -gx CONDA_PROMPT_MODIFIER "(%(prefix)s) "
        set -gx CONDA_PYTHON_EXE "%(sys_executable)s"
        set -gx CONDA_SHLVL "1"
        set -gx PATH "%(new_path)s"
        source "%(activate1)s"
        """) % {
            'prefix': self.prefix,
            'new_path': activator.pathsep_join(new_path_parts),
            'sys_executable': sys.executable,
            'activate1': join(self.prefix, 'etc', 'conda', 'activate.d', 'activate1.fish')
        }

        with env_var('CONDA_PREFIX', self.prefix):
            with env_var('CONDA_SHLVL', '1'):
                with env_var('PATH', os.pathsep.join(concatv(new_path_parts, (os.environ['PATH'],)))):
                    reactivate_data = activator.reactivate()

                    assert reactivate_data == dals("""
                    source "%(deactivate1)s"
                    source "%(activate1)s"
                    """) % {
                        'activate1': join(self.prefix, 'etc', 'conda', 'activate.d', 'activate1.fish'),
                        'deactivate1': join(self.prefix, 'etc', 'conda', 'deactivate.d', 'deactivate1.fish'),
                    }

                    deactivate_data = activator.deactivate()

                    new_path = activator.pathsep_join(activator._remove_prefix_from_path(self.prefix))
                    assert deactivate_data == dals("""
                    set -e CONDA_DEFAULT_ENV
                    set -e CONDA_PREFIX
                    set -e CONDA_PROMPT_MODIFIER
                    set -e CONDA_PYTHON_EXE
                    set -gx CONDA_SHLVL "0"
                    set -gx PATH "%(new_path)s"
                    source "%(deactivate1)s"
                    """) % {
                        'new_path': new_path,
                        'deactivate1': join(self.prefix, 'etc', 'conda', 'deactivate.d', 'deactivate1.fish'),

                    }

    def test_powershell_basic(self):
        activator = Activator('powershell')
        self.make_dot_d_files(activator.script_extension)

        activate_data = activator.activate(self.prefix)

        new_path_parts = activator._add_prefix_to_path(self.prefix)
        assert activate_data == dals("""
        $env:CONDA_DEFAULT_ENV = "%(prefix)s"
        $env:CONDA_PREFIX = "%(prefix)s"
        $env:CONDA_PROMPT_MODIFIER = "(%(prefix)s) "
        $env:CONDA_PYTHON_EXE = "%(sys_executable)s"
        $env:CONDA_SHLVL = "1"
        $env:PATH = "%(new_path)s"
        . "%(activate1)s"
        """) % {
            'prefix': self.prefix,
            'new_path': activator.pathsep_join(new_path_parts),
            'sys_executable': sys.executable,
            'activate1': join(self.prefix, 'etc', 'conda', 'activate.d', 'activate1.ps1')
        }

        with env_var('CONDA_PREFIX', self.prefix):
            with env_var('CONDA_SHLVL', '1'):
                with env_var('PATH', os.pathsep.join(concatv(new_path_parts, (os.environ['PATH'],)))):
                    reactivate_data = activator.reactivate()

                    assert reactivate_data == dals("""
                    . "%(deactivate1)s"
                    . "%(activate1)s"
                    """) % {
                        'activate1': join(self.prefix, 'etc', 'conda', 'activate.d', 'activate1.ps1'),
                        'deactivate1': join(self.prefix, 'etc', 'conda', 'deactivate.d', 'deactivate1.ps1'),
                    }

                    deactivate_data = activator.deactivate()

                    new_path = activator.pathsep_join(activator._remove_prefix_from_path(self.prefix))
                    assert deactivate_data == dals("""
                    Remove-Variable CONDA_DEFAULT_ENV
                    Remove-Variable CONDA_PREFIX
                    Remove-Variable CONDA_PROMPT_MODIFIER
                    Remove-Variable CONDA_PYTHON_EXE
                    $env:CONDA_SHLVL = "0"
                    $env:PATH = "%(new_path)s"
                    . "%(deactivate1)s"
                    """) % {
                        'new_path': new_path,
                        'deactivate1': join(self.prefix, 'etc', 'conda', 'deactivate.d', 'deactivate1.ps1'),

                    }
