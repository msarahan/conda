# -*- coding: utf-8 -*-
from __future__ import print_function, absolute_import

import subprocess
import tempfile

import os,stat
from os.path import dirname
import stat
import sys
from textwrap import dedent

import pytest

from conda.compat import TemporaryDirectory
from conda.config import root_dir, platform
from conda.install import symlink_conda
from conda.utils import path_identity, shells, on_win, translate_stream
from conda.cli.activate import binpath_from_arg

from tests.helpers import assert_equals, assert_in, assert_not_in


def gen_test_env_paths(envs, shell, num_test_folders=5):
    """People need not use all the test folders listed here.
    This is only for shortening the environment string generation.

    Also encapsulates paths in double quotes.
    """
    paths = [os.path.join(envs, "test {}".format(test_folder+1)) for test_folder in range(num_test_folders)]
    for path in paths[:2]:      # Create symlinks ONLY for the first two folders.
        symlink_conda(path, sys.prefix, shell)
    converter = shells[shell]["path_to"]
    paths = {i:converter(path) for i, path in enumerate(paths)}
    paths["root"]="root"
    paths["bad"]="foo bar baz qux"
    envname = {k:shells[shell]["setvar"].format(variable="CONDA_ENVNAME",value=path) for k,path in paths.items()}
    return (paths, envname)

def _envpaths(env_root, env_name="", shelldict={}):
    """Supply the appropriate platform executable folders.  rstrip on root removes
       trailing slash if env_name is empty (the default)

    Assumes that any prefix used here exists.  Will not work on prefixes that don't.
    """
    sep = shelldict['sep']
    return binpath_from_arg(sep.join([env_root, env_name]), shelldict=shelldict)


PYTHONPATH = os.path.dirname(os.path.dirname(__file__))

# Make sure the subprocess activate calls this python
syspath = os.pathsep.join(_envpaths(root_dir, shelldict={"path_to": path_identity,
                                                         "path_from": path_identity,
                                                         "sep": os.sep}))

def print_ps1(env_dirs, base_prompt, number):
    return u"({}) {}".format(env_dirs[number],base_prompt)


CONDA_ENTRY_POINT = dedent("""\
    #!{syspath}/python
    import sys
    from conda.cli import main

    sys.exit(main())
    """)

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

    # base_prompt, _ = run_in(shelldict["printprompt"], shell)
    base_prompt = "test_prompt"

    syspath = shelldict['path_to'](sys.prefix)

    pythonpath=shelldict["setenvvar"].format(
        variable="PYTHONPATH",
        value=shelldict['path_to'](PYTHONPATH))
    # remove any conda RC references
    condarc=shelldict["unsetenvvar"].format(
        variable="CONDARC")
    # clear any preset conda environment
    condadefaultenv=shelldict["unsetenvvar"].format(
        variable="CONDA_DEFAULT_ENV")
    # set prompt such that we have a prompt to play
    # around and test with since most of the below
    # tests will not be invoked in an interactive
    # login shell and hence wont have the prompt initialized
    #
    # setting this here also means that we no longer have to
    # mess with the .bash_profile during testing to
    # standardize the base prompt
    setprompt=shelldict["setprompt"].format(
        value=base_prompt)
    command_setup = dedent("""\
        {pythonpath}
        {condarc}
        {condadefaultenv}
        {setprompt}
        """).format(pythonpath=pythonpath,
                    condarc=condarc,
                    condadefaultenv=condadefaultenv,
                    setprompt=setprompt)

    if shelldict["shell_suffix"] == '.bat':
        command_setup = "@echo off\n" + command_setup

    shelldict.update({
        'base_prompt': base_prompt,
        'syspath': syspath,
        'command_setup': command_setup,
        'base_path': base_path,
    })

    return shelldict


# temporarily standardize the user profile to make testing simpler
# @pytest.fixture(scope="module")
# def bash_profile(request):
#     profile=os.path.join(os.path.expanduser("~"), ".bash_profile")
#     profile_backup=profile+"_backup"

#     if os.path.isfile(profile):
#         os.rename(profile, profile_backup)

#     with open(profile, "w") as f:
#         f.write("export PS1=test_ps1\n")
#         f.write("export PROMPT=test_ps1\n")

#     def fin():
#         if os.path.isfile(profile_backup):
#             os.remove(profile)
#             os.rename(profile_backup, profile)
#     request.addfinalizer(fin)

#     return request  # provide the fixture value


@pytest.mark.installed
def test_activate_test1(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printpath}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}"
                {printpath}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_in(shell_vars['pathsep'].join(_envpaths(envs, 'test 1', shelldict=shell_vars)),
                stdout, shell)
            assert_equals(stderr,'')


@pytest.mark.installed
def test_activate_env_from_env_with_root_activate(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {env_vars[1]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printpath}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}" {nul}
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[1]}"
                {printpath}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)

            print("commands:", commands)
            print("stdout:", stdout)
            print("stderr:", stderr)

            assert_in(shell_vars['pathsep'].join(_envpaths(envs, 'test 2', shelldict=shell_vars)),
                stdout, shell)
            assert_equals(stderr,'')


@pytest.mark.installed
def test_activate_bad_directory(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        # Strange semicolons are here to defeat MSYS' automatic path conversion.
        # See http://www.mingw.org/wiki/Posix_path_conversion
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[2]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printpath}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[2]}"
                {printpath}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            # another semicolon here for comparison reasons with one above.
            assert_in('could not find environment',stderr,shell)
            assert_not_in(env_dirs[2], stdout, shell)


@pytest.mark.installed
def test_activate_bad_env_keeps_existing_good_env(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {env_vars[2]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printpath}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}" {nul}
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[2]}"
                {printpath}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_in(shell_vars['pathsep'].join(_envpaths(envs, 'test 1', shelldict=shell_vars)),
                stdout, shell)
            assert_in("Could not find environment",stderr)


@pytest.mark.installed
def test_activate_deactivate(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
            {printpath}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}" {nul}
                {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
                {printpath}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            stdout = strip_leading_library_bin(stdout, shell_vars)
            assert_equals(stdout, u"%s" % shell_vars['base_path'], stderr)
            assert_equals(stderr,'')


@pytest.mark.installed
def test_activate_root(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[root]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printpath}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[root]}"
                {printpath}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_in(shell_vars['pathsep'].join(_envpaths(root_dir, shelldict=shell_vars)),
                stdout, shell)
            assert_equals(stderr,'')


@pytest.mark.installed
def test_activate_deactivate_root(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[root]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {source} "{syspath}{binpath}deactivate{shell_suffix}"
            {printpath}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[root]}"
                {source} "{syspath}{binpath}deactivate{shell_suffix}"
                {printpath}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            stdout = strip_leading_library_bin(stdout, shell_vars)
            assert_equals(stdout, u"%s" % shell_vars['base_path'], stderr)
            assert_equals(stderr,'')


@pytest.mark.installed
def test_activate_root_env_from_other_env(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {env_vars[root]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printpath}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}" {nul}
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[root]}"
                {printpath}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                    env_vars=env_vars,
                    env_dirs=env_dirs,
                    **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_in(shell_vars['pathsep'].join(_envpaths(root_dir, shelldict=shell_vars)),
                stdout, shell)
            assert_not_in(shell_vars['pathsep'].join(_envpaths(envs, 'test 1', shelldict=shell_vars)),
                stdout, shell)
            assert_equals(stderr,'')


@pytest.mark.installed
def test_wrong_args(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        # cannot accidentally pass too many args to program when setting environment variables
        scripts=[]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
            {source} "{syspath}{binpath}activate{shell_suffix}" two args
            {printpath}
            """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                    env_vars=env_vars,
                    env_dirs=env_dirs,
                    **shell_vars)
            stdout, stderr = run_in(commands, shell)
            stdout = strip_leading_library_bin(stdout, shell_vars)
            assert_equals(stdout, shell_vars['base_path'], stderr)
            assert_in("[ACTIVATE]: ERROR: Unknown/Invalid flag/parameter (args)",
                stderr, shell)


@pytest.mark.installed
def test_activate_check_sourcing(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        if shell not in ['powershell.exe', 'cmd.exe']:
            # all unix shells support environment variables instead of parameter passing
            scripts=[dedent("""\
                {env_vars[0]} ; "{syspath}{binpath}activate{shell_suffix}"
                """)]
            # most unix shells support parameter passing, dash is the exception
            if shell not in ["dash","sh","csh","posh"]:
                scripts+=[dedent("""\
                    "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}"
                    """)]

            for script in scripts:
                commands = shell_vars['command_setup'] + script.format(
                    env_vars=env_vars,
                    env_dirs=env_dirs,
                    **shell_vars)
                stdout, stderr = run_in(commands, shell)
                assert_equals(stdout, '', stderr)
                assert_in(dedent("""\
                    [ACTIVATE]: ERROR: Parsing failure.
                    [ACTIVATE]: ERROR: This most likely means you executed the script instead of sourcing it."""),
                    stderr, shell)


@pytest.mark.installed
def test_activate_help(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        help=shell_vars["setvar"].format(variable="CONDA_HELP",value="true")
        scripts=[dedent("""\
            {help} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" --help
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                help=help,
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, '')
            assert_in("activate must be sourced", stderr)
            # assert_in("Usage: source activate ENV", stderr)

            if shell in ["cmd.exe", "powershell"]:
                assert_in('Usage: activate [ENV] [-h] [-v]', stderr, shell)
            elif shell in ["csh","tcsh"]:
                assert_in('Usage: source "`which activate`" [ENV] [-h] [-v]', stderr, shell)
            else:
                assert_in('Usage: . activate [ENV] [-h] [-v]', stderr, shell)


        if shell in ["cmd.exe", "powershell"]:
            # assert_in("Usage: activate ENV", stderr)
            pass
        else:
            # assert_in("Usage: source activate ENV", stderr)

        if shell not in ['powershell.exe', 'cmd.exe']:
            # all unix shells support environment variables instead of parameter passing
            scripts=[dedent("""\
                "{syspath}{binpath}deactivate{shell_suffix}"
                """)]
            # most unix shells support parameter passing, dash is the exception
            if shell not in ["dash","sh","csh","posh"]:
                scripts+=[dedent("""\
                    "{syspath}{binpath}deactivate{shell_suffix}"
                    """)]

            for script in scripts:
                commands = shell_vars['command_setup'] + script.format(
                    env_vars=env_vars,
                    env_dirs=env_dirs,
                    **shell_vars)
                stdout, stderr = run_in(commands, shell)
                assert_equals(stdout, '', stderr)
                assert_in(dedent("""\
                    [DEACTIVATE]: ERROR: Parsing failure.
                    [DEACTIVATE]: ERROR: This most likely means you executed the script instead of sourcing it."""),
                    stderr, shell)


@pytest.mark.installed
def test_deactivate_help(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        help=shell_vars["setvar"].format(variable="CONDA_HELP",value="true")
        scripts=[dedent("""\
            {help} ; {source} "{syspath}{binpath}deactivate{shell_suffix}"
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}deactivate{shell_suffix}" --help
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                help=help,
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, '')
            assert_in("deactivate must be sourced", stderr)
            # assert_in("Usage: source deactivate", stderr)

        commands = (shell_vars['command_setup'] + """
        {source} {syspath}{binpath}deactivate --help
        """).format(envs=envs, **shell_vars)
        stdout, stderr = run_in(commands, shell)
        assert_equals(stdout, '')
        # if shell in ["cmd.exe", "powershell"]:
        #     assert_in("Usage: deactivate", stderr)
        # else:
        #     assert_in("Usage: source deactivate", stderr)


@pytest.mark.installed
def test_activate_symlinking(shell):
    """Symlinks or bat file redirects are created at activation time.  Make sure that the
    files/links exist, and that they point where they should."""
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)
        for k in [0,1]:
            for f in ["conda", "activate", "deactivate"]:
                file_path = "{env_dir}{binpath}{f}{shell_suffix}".format(
                    env_dir=env_dirs[k],
                    f=f,
                    **shell_vars)
                if on_win:
                    # must translate path to windows representation for Python's sake
                    file_path = shell_vars["path_from"](file_path)
                    assert(os.path.lexists(file_path))
                else:
                    real_path = "{syspath}{binpath}{f}{shell_suffix}".format(
                        f=f,
                        **shell_vars)
                    assert(os.path.lexists(file_path))
                    assert(stat.S_ISLNK(os.lstat(file_path).st_mode))
                    assert(os.readlink(file_path) == real_path)

        if platform != 'win':
            # Test activate when there are no write permissions in the
            # env.

            # all unix shells support environment variables instead of parameter passing
            scripts=[dedent("""\
                mkdir -p "{env_dirs[2]}{binpath}"
                chmod 444 "{env_dirs[2]}{binpath}"
                {env_vars[2]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
                """)]
            # most unix shells support parameter passing, dash is the exception
            if shell not in ["dash","sh","csh","posh"]:
                scripts+=[dedent("""\
                    mkdir -p "{env_dirs[2]}{binpath}"
                    chmod 444 "{env_dirs[2]}{binpath}"
                    {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[2]}"
                    """)]

            for script in scripts:
                commands = shell_vars['command_setup'] + script.format(
                    env_vars=env_vars,
                    env_dirs=env_dirs,
                    **shell_vars)
                stdout, stderr = run_in(commands, shell)
                assert_equals(stdout,'')
                assert_in("not have write access", stderr, shell)

            # restore permissions so the dir will get cleaned up
            commands = dedent("""\
                chmod 777 "{env_dirs[2]}{binpath}"
                """).format(
                    env_vars=env_vars,
                    env_dirs=env_dirs,
                    **shell_vars)
            run_in(commands, shell)


@pytest.mark.installed
def test_PS1(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        #-----------------------------------------------------------------------
        # TEST 1: activate changes PS1 correctly
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printprompt}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}"
                {printprompt}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                    env_vars=env_vars,
                    env_dirs=env_dirs,
                    **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, print_ps1(env_dirs=env_dirs,
                                            base_prompt=shell_vars["base_prompt"],
                                            number=0), stderr)
            assert_equals(stderr,'')

        #-----------------------------------------------------------------------
        # TEST 2: second activate replaces earlier activated env PS1
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {env_vars[1]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printprompt}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}" {nul}
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[1]}"
                {printprompt}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                    env_vars=env_vars,
                    env_dirs=env_dirs,
                    **shell_vars)
            stdout, sterr = run_in(commands, shell)
            assert_equals(stdout, print_ps1(env_dirs=env_dirs,
                                            base_prompt=shell_vars["base_prompt"],
                                            number=1), stderr)
            assert_equals(stderr,'')

        #-----------------------------------------------------------------------
        # TEST 3: failed activate does not touch raw PS1
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[2]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printprompt}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[2]}"
                {printprompt}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, shell_vars['base_prompt'], stderr)
            assert_in("Could not find environment",stderr)

        #-----------------------------------------------------------------------
        # TEST 4: ensure that a failed activate does not touch PS1
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {env_vars[2]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printprompt}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
            {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}" {nul}
            {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[2]}"
            {printprompt}
            """)]

        if script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, print_ps1(env_dirs=env_dirs,
                                            base_prompt=shell_vars["base_prompt"],
                                            number=0), stderr)
            assert_equals(stderr,'')

        #-----------------------------------------------------------------------
        # TEST 5: deactivate doesn't do anything bad to PS1 when no env active to deactivate
        #-----------------------------------------------------------------------
        scripts=[dedent("""\
            {source} "{syspath}{binpath}deactivate{shell_suffix}"
            {printprompt}
            """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, shell_vars['base_prompt'], stderr)
            assert_equals(stderr,'')

        #-----------------------------------------------------------------------
        # TEST 6: deactivate script in activated env returns us to raw PS1
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
            {printprompt}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}" {nul}
                {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
                {printprompt}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, shell_vars['base_prompt'], stderr)
            assert_equals(stderr,'')

        #-----------------------------------------------------------------------
        # TEST 7: make sure PS1 is unchanged by faulty activate input
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        # cannot accidentally pass too many args to program when setting environment variables
        scripts=[]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" two args
                {printprompt}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, shell_vars['base_prompt'], stderr)
            assert_in('[ACTIVATE]: ERROR: Unknown/invalid flag/parameter',stderr)

@pytest.mark.installed
def test_PS1_no_changeps1(shell):
    """Ensure that people's PS1 remains unchanged if they have that setting in their RC file."""
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        rc_file = os.path.join(envs, ".condarc")
        with open(rc_file, 'w') as f:
            f.write("changeps1: False\n")
        condarc = shell_vars["setenvvar"].format(
            variable="CONDARC",
            value=rc_file)

        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        scripts_with_stderr=[(dedent("""
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printprompt}
            """),None),(dedent("""
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {env_vars[1]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printprompt}
            """),None),(dedent("""
            {env_vars[2]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printprompt}
            """),'Could not find environment'),(dedent("""
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {env_vars[2]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printprompt}
            """),'Could not find environment'),(dedent("""
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
            {printprompt}
            """),None)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts_with_stderr+=[(dedent("""
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}"
                {printprompt}
                """),None),(dedent("""
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}" {nul}
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[1]}"
                {printprompt}
                """),None),(dedent("""
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[2]}"
                {printprompt}
                """),'Could not find environment'),(dedent("""
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}" {nul}
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[2]}"
                {printprompt}
                """),'Could not find environment'),(dedent("""
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}" {nul}
                {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
                {printprompt}
                """),None),(dedent("""
                {source} "{syspath}{binpath}activate{shell_suffix}" two args
                {printprompt}
                """),'[ACTIVATE]: ERROR: Unknown/invalid flag/parameter')]

        for script,err in scripts_with_stderr:
            commands = shell_vars['command_setup'] + condarc + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, shell_vars['base_prompt'], stderr)
            if err is None:
                assert_equals(stderr,'')
            else:
                assert_in(err,stderr)


@pytest.mark.installed
def test_CONDA_DEFAULT_ENV(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        #-----------------------------------------------------------------------
        # TEST 1
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printdefaultenv}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}"
                {printdefaultenv}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout.rstrip(), env_dirs[0], stderr)
            assert_equals(stderr,'')

        #-----------------------------------------------------------------------
        # TEST 2
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {env_vars[1]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printdefaultenv}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}" {nul}
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[1]}"
                {printdefaultenv}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout.rstrip(), env_dirs[1], stderr)
            assert_equals(stderr,'')

        #-----------------------------------------------------------------------
        # TEST 3
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[2]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printdefaultenv}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts=[dedent("""\
            {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[2]}"
            {printdefaultenv}
            """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, '', stderr)
            assert_in("Could not find environment",stderr)

        #-----------------------------------------------------------------------
        # TEST 4
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {env_vars[2]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printdefaultenv}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}" {nul}
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[2]}"
                {printdefaultenv}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout.rstrip(), env_dirs[0], stderr)
            assert_in("Could not find environment",stderr)

        #-----------------------------------------------------------------------
        # TEST 5
        #-----------------------------------------------------------------------
        scripts=[dedent("""\
            {source} "{syspath}{binpath}deactivate{shell_suffix}"
            echo "`env | grep {var}`."
            """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                var="CONDA_DEFAULT_ENV",
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, '.', stderr)
            assert_equals(stderr,'')

        #-----------------------------------------------------------------------
        # TEST 6
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
            echo "`env | grep {var}`."
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}" {nul}
                {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
                echo "`env | grep {var}`."
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                var="CONDA_DEFAULT_ENV",
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout,'.',stderr)
            assert_equals(stderr,'')

        #-----------------------------------------------------------------------
        # TEST 7
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        # cannot accidentally pass too many args to program when setting environment variables
        scripts=[]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" two args
                {printdefaultenv}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, '', stderr)
            assert_in('[ACTIVATE]: ERROR: Unknown/invalid flag/parameter',stderr)

        #-----------------------------------------------------------------------
        # TEST 8
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[root]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {printdefaultenv}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[root]}" {nul}
                {printdefaultenv}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout.rstrip(), 'root', stderr)
            assert_equals(stderr,'')

        #-----------------------------------------------------------------------
        # TEST 9
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[root]} ; {source} "{syspath}{binpath}activate{shell_suffix}" {nul}
            {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}" {nul}
            echo "`env | grep {var}`."
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[root]}" {nul}
                {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}" {nul}
                echo "`env | grep {var}`."
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                var="CONDA_DEFAULT_ENV",
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout,'.',stderr)
            assert_equals(stderr,'')


@pytest.mark.installed
def test_activate_from_env(shell):
    """Tests whether the activate bat file or link in the activated environment works OK"""
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {env_vars[1]} ; {source} "{env_dirs[0]}{binpath}activate{shell_suffix}"
            {printdefaultenv}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}"
                {source} "{env_dirs[0]}{binpath}activate{shell_suffix}" "{env_dirs[1]}"
                {printdefaultenv}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            # rstrip on output is because the printing to console picks up an extra space
            assert_equals(stdout.rstrip(), env_dirs[1], stderr)
            assert_equals(stderr,'')


@pytest.mark.installed
def test_deactivate_from_env(shell):
    """Tests whether the deactivate bat file or link in the activated environment works OK"""
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
            echo "`env | grep {var}`."
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}"
                {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
                echo "`env | grep {var}`."
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                var="CONDA_DEFAULT_ENV",
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout,'.',stderr)
            assert_equals(stderr,'')


@pytest.mark.installed
def test_activate_relative_path(shell):
    """
    current directory should be searched for environments
    """
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        work_dir = os.path.dirname(env_dirs[0])
        env_dir = os.path.basename(env_dirs[0])
        env_var = shell_vars["setvar"].format(variable="CONDA_ENVNAME",value=env_dir)

        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            cd {work_dir}
            {env_var} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printdefaultenv}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                cd {work_dir}
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dir}"
                {printdefaultenv}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                work_dir=work_dir,
                env_var=env_var,
                env_dir=env_dir,
                **shell_vars)
            cwd = os.getcwd()
            # this is not effective for running bash on windows.  It starts
            # in your home dir no matter what. That's what the cd is for above.
            os.chdir(envs)
            try:
                stdout, stderr = run_in(commands, shell, cwd=envs)
            except:
                raise
            finally:
                os.chdir(cwd)
            assert_equals(stdout.rstrip(), env_dir, stderr)
            assert_equals(stderr,'')


@pytest.mark.skipif(not on_win, reason="only relevant on windows")
def test_activate_does_not_leak_echo_setting(shell):
    """Test that activate's setting of echo to off does not disrupt later echo calls"""

    if not on_win or shell != "cmd.exe":
        pytest.skip("test only relevant for cmd.exe on win")
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        commands = shell_vars['command_setup'] + dedent("""\
            @echo on
            @call "{syspath}{binpath}activate.bat" "{env_dirs[0]}"
            @echo
            """).format(
                envs=envs,
                env_dirs=gen_test_env_paths(envs, shell),
                **shell_vars)
        stdout, stderr = run_in(commands, shell)
        assert_equals(stdout, u'ECHO is on.', stderr)


@pytest.mark.xfail(reason="subprocess with python 2.7 is broken with unicode")
@pytest.mark.installed
def test_activate_non_ascii_char_in_path(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='Ånvs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
            {printdefaultenv}.
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}"
                {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
                {printdefaultenv}.
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, u'.', stderr)
            assert_equals(stderr,'')


@pytest.mark.installed
def test_activate_has_extra_env_vars(shell):
    """Test that environment variables in activate.d show up when activated"""
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        testvariable="TEST_VAR"

        dir=os.path.join(shell_vars['path_from'](env_dirs[0]), "etc", "conda", "activate.d")
        os.makedirs(dir)
        file="test{}".format(shell_vars["env_script_suffix"])
        file=os.path.join(dir,file)
        with open(file, 'w') as f:
            f.write(shell_vars["setenvvar"].format(
                variable=testvariable,
                value="test"))

        dir=os.path.join(shell_vars['path_from'](env_dirs[0]), "etc", "conda", "deactivate.d")
        os.makedirs(dir)
        file="test{}".format(shell_vars["env_script_suffix"])
        file=os.path.join(dir,file)
        with open(file, 'w') as f:
            f.write(shell_vars["unsetenvvar"].format(
                variable=testvariable))

        #-----------------------------------------------------------------------
        # TEST ACTIVATE
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {echo} {var}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}"
                {echo} {var}
                """)]

            for script in scripts:
                commands = shell_vars['command_setup'] + script.format(
                    env_vars=env_vars,
                    env_dirs=env_dirs,
                    var=shell_vars["var_format"].format(testvariable),
                    **shell_vars)
                stdout, stderr = run_in(commands, shell)
                assert_equals(stdout, u'test', stderr)
                assert_equals(stderr,'')

        #-----------------------------------------------------------------------
        # TEST DEACTIVATE
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
            echo "`env | grep {var}`."
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}"
                {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
                echo "`env | grep {var}`."
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                    env_vars=env_vars,
                    env_dirs=env_dirs,
                    var=testvariable,
                    **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_equals(stdout, u'.', stderr)
            assert_equals(stderr,'')


@pytest.mark.installed
def test_activate_verbose(shell):
    """Test that environment variables in activate.d show up when activated"""
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        testvariable="TEST_VAR"

        dir=os.path.join(shell_vars['path_from'](env_dirs[0]), "etc", "conda", "activate.d")
        os.makedirs(dir)
        file="test{}".format(shell_vars["env_script_suffix"])
        file=os.path.join(dir,file)
        with open(file, 'w') as f:
            f.write(shell_vars["setenvvar"].format(
                variable=testvariable,
                value="test"))

        dir=os.path.join(shell_vars['path_from'](env_dirs[0]), "etc", "conda", "deactivate.d")
        os.makedirs(dir)
        file="test{}".format(shell_vars["env_script_suffix"])
        file=os.path.join(dir,file)
        with open(file, 'w') as f:
            f.write(shell_vars["unsetenvvar"].format(
                variable=testvariable))

        #-----------------------------------------------------------------------
        # TEST ACTIVATE
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {verbose_var} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}" "--verbose"
                """)]

            for script in scripts:
                commands = shell_vars['command_setup'] + script.format(
                    verbose_var=shell_vars["setvar"].format(variable="CONDA_VERBOSE",value="true"),
                    env_vars=env_vars,
                    env_dirs=env_dirs,
                    **shell_vars)
                stdout, stderr = run_in(commands, shell)
                assert_in('[ACTIVATE]: Sourcing',stdout,shell)
                assert_equals(stderr,'')

        #-----------------------------------------------------------------------
        # TEST DEACTIVATE
        #-----------------------------------------------------------------------
        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {verbose_var} ; {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}"
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}"
                {source} "{env_dirs[0]}{binpath}deactivate{shell_suffix}" "--verbose"
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                    verbose_var=shell_vars["setvar"].format(variable="CONDA_VERBOSE",value="true"),
                    env_vars=env_vars,
                    env_dirs=env_dirs,
                    **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_in('[DEACTIVATE]: Sourcing',stdout,shell)
            assert_equals(stderr,'')


@pytest.mark.installed
def test_activate_noPS1(shell):
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        env_dirs,env_vars=gen_test_env_paths(envs, shell)

        # all unix shells support environment variables instead of parameter passing
        scripts=[dedent("""\
            {unsetprompt}
            {env_vars[0]} ; {source} "{syspath}{binpath}activate{shell_suffix}"
            {printpath}
            """)]
        # most unix shells support parameter passing, dash is the exception
        if shell not in ["dash","sh","csh","posh"]:
            scripts+=[dedent("""\
                {unsetprompt}
                {source} "{syspath}{binpath}activate{shell_suffix}" "{env_dirs[0]}"
                {printpath}
                """)]

        for script in scripts:
            commands = shell_vars['command_setup'] + script.format(
                env_vars=env_vars,
                env_dirs=env_dirs,
                **shell_vars)
            stdout, stderr = run_in(commands, shell)
            assert_in(shell_vars['pathsep'].join(_envpaths(envs, 'test 1', shelldict=shell_vars)),
                stdout, shell)
            assert_equals(stderr,'')


@pytest.mark.slow
def test_activate_keeps_PATH_order(shell):
    if not on_win or shell != "cmd.exe":
        pytest.xfail("test only implemented for cmd.exe on win")
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        commands = shell_vars['command_setup'] + dedent("""\
            @set "PATH=somepath;CONDA_PATH_PLACEHOLDER;%PATH%"
            @call "{syspath}{binpath}activate.bat"
            {printpath}
            """).format(
                envs=envs,
                env_dirs=gen_test_env_paths(envs, shell),
                **shell_vars)
        stdout, stderr = run_in(commands, shell)
        assert stdout.startswith("somepath;" + sys.prefix)

@pytest.mark.slow
def test_deactivate_placeholder(shell):
    if not on_win or shell != "cmd.exe":
        pytest.xfail("test only implemented for cmd.exe on win")
    shell_vars = _format_vars(shell)
    with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
        commands = shell_vars['command_setup'] + dedent("""\
            @set "PATH=flag;%PATH%"
            @call "{syspath}{binpath}activate.bat"
            @call "{syspath}{binpath}deactivate.bat" "hold"
            {printpath}
            """).format(
                envs=envs,
                env_dirs=gen_test_env_paths(envs, shell),
                **shell_vars)
        stdout, stderr = run_in(commands, shell)
        assert stdout.startswith("CONDA_PATH_PLACEHOLDER;flag")


# This test depends on files that are copied/linked in the conda recipe.  It is unfortunately not going to run after
#    a setup.py install step
# @pytest.mark.slow
# def test_activate_from_exec_folder(shell):
#     """The exec folder contains only the activate and conda commands.  It is for users
#     who want to avoid conda packages shadowing system ones."""
#     shell_vars = _format_vars(shell)
#     with TemporaryDirectory(prefix='envs', dir=dirname(__file__)) as envs:
#         env_dirs=gen_test_env_paths(envs, shell)
#         commands = shell_vars['command_setup'] + dedent("""\
#             {source} "{syspath}/exec/activate{shell_suffix}" "{env_dirs[0]}"
#             {echo} {var}
#             """).format(
#                 envs=envs,
#                 env_dirs=env_dirs,
#                 var=shell_vars["var_format"].format("TEST_VAR"),
#                 **shell_vars)
#         stdout, stderr = run_in(commands, shell)
#         assert_equals(stdout, u'test', stderr)


def run_in(command, shell, cwd=None, env=None):
    if hasattr(shell, "keys"):
        shell = shell["exe"]
    if shell == 'cmd.exe':
        cmd_script = tempfile.NamedTemporaryFile(suffix='.bat', mode='wt', delete=False)
        cmd_script.write(command)
        cmd_script.close()
        cmd_bits = dedent("""\
            {exe} {shell_args} {script}
            """).format(
                script=cmd_script.name,
                **shells[shell])
        try:
            p = subprocess.Popen(cmd_bits, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 cwd=cwd, env=env)
            stdout, stderr = p.communicate()
        finally:
            os.unlink(cmd_script.name)
    elif shell == 'powershell':
        raise NotImplementedError
    else:
        # heredoc/hereword are the closest we can get to truly mimicking a
        # proper sourcing of the activate/deactivate scripts
        #
        # must use heredoc to avoid Ubuntu/dash incompatibility with hereword
        cmd_bits = dedent("""\
            {exe} <<- 'RUNINCMD'
            {command}
            RUNINCMD
            """).format(
                command=translate_stream(command, shells[shell]["path_to"]),
                **shells[shell])
        print("cmd_bits:",cmd_bits)
        p = subprocess.Popen(cmd_bits, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = p.communicate()
    streams = [u"%s" % stream.decode('utf-8').replace('\r\n', '\n').rstrip("\n")
               for stream in (stdout, stderr)]
    return streams
