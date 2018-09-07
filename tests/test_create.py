# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

import bz2
from contextlib import contextmanager
from glob import glob

from conda._vendor.toolz.itertoolz import groupby
from conda.gateways.disk.permissions import make_read_only
from conda.models.channel import Channel
from conda.resolve import Resolve
from itertools import chain
import json
from json import loads as json_loads
from logging import DEBUG, INFO, getLogger
import os
from os.path import basename, dirname, exists, isdir, isfile, join, lexists, relpath, islink
from random import sample
import re
from shlex import split
import shutil
from shutil import copyfile, rmtree
from subprocess import check_call, CalledProcessError, check_output
import sys
from tempfile import gettempdir
from unittest import TestCase
from uuid import uuid4

import shutil

from datetime import datetime

import conda
from conda._vendor.auxlib.ish import dals
from conda.gateways.anaconda_client import read_binstar_tokens
import pytest
import requests

from conda import CondaError, CondaMultiError, plan, __version__ as CONDA_VERSION, \
    CONDA_PACKAGE_ROOT
from conda._vendor.auxlib.entity import EntityEncoder
from conda._vendor.auxlib.ish import dals
from conda.base.constants import CONDA_TARBALL_EXTENSION, PACKAGE_CACHE_MAGIC_FILE, SafetyChecks, \
    PREFIX_MAGIC_FILE
from conda.base.context import Context, context, reset_context
from conda.cli.conda_argparse import do_call
from conda.cli.main import generate_parser, init_loggers
from conda.common.compat import PY2, iteritems, itervalues, text_type, ensure_text_type
from conda.common.io import argv, captured, disable_logger, env_var, stderr_log_level, dashlist
from conda.common.path import get_bin_directory_short_path, get_python_site_packages_short_path, \
    pyc_path
from conda.common.serialize import yaml_load, json_dump
from conda.common.url import path_to_url
from conda.core.index import get_reduced_index
from conda.core.prefix_data import PrefixData, get_python_version_for_prefix
from conda.core.package_cache_data import PackageCacheData
from conda.core.subdir_data import create_cache_dir
from conda.exceptions import CommandArgumentError, DryRunExit, OperationNotAllowed, \
    PackagesNotFoundError, RemoveError, conda_exception_handler, PackageNotInstalledError, \
    DisallowedPackageError, UnsatisfiableError, DirectoryNotACondaEnvironmentError
from conda.gateways.anaconda_client import read_binstar_tokens
from conda.gateways.disk.create import mkdir_p
from conda.gateways.disk.delete import rm_rf
from conda.gateways.disk.update import touch
from conda.gateways.logging import TRACE
from conda.gateways.subprocess import subprocess_call
from conda.models.match_spec import MatchSpec
from conda.models.records import PackageRecord
from conda.models.version import VersionOrder
from conda.utils import on_win

try:
    from unittest.mock import Mock, patch
except ImportError:
    from mock import Mock, patch


log = getLogger(__name__)
TRACE, DEBUG, INFO = TRACE, DEBUG, INFO  # these are so the imports aren't cleared, but it's easy to switch back and forth
TEST_LOG_LEVEL = DEBUG
stderr_log_level(TEST_LOG_LEVEL, 'conda')
stderr_log_level(TEST_LOG_LEVEL, 'requests')
PYTHON_BINARY = 'python.exe' if on_win else 'bin/python'
BIN_DIRECTORY = 'Scripts' if on_win else 'bin'
UINCODE_CHARACTERS = u"ōγђ家固한"
UINCODE_CHARACTERS = u"áêñßôç"


def escape_for_winpath(p):
    return p.replace('\\', '\\\\')


def make_temp_prefix(name=None, create_directory=True):
    tempdir = gettempdir()
    if PY2:
        dirpath = str(uuid4())[:8] if name is None else name
    else:
        random_unicode = ''.join(sample(UINCODE_CHARACTERS, len(UINCODE_CHARACTERS)))
        dirpath = (str(uuid4())[:4] + ' ' + random_unicode) if name is None else name
    prefix = join(tempdir, dirpath)
    os.makedirs(prefix)
    if create_directory:
        assert isdir(prefix)
    else:
        os.removedirs(prefix)
    return prefix


class Commands:
    CONFIG = "config"
    CLEAN = "clean"
    CREATE = "create"
    INFO = "info"
    INSTALL = "install"
    LIST = "list"
    REMOVE = "remove"
    SEARCH = "search"
    UPDATE = "update"


def run_command(command, prefix, *arguments, **kwargs):
    use_exception_handler = kwargs.get('use_exception_handler', False)
    arguments = list(arguments)
    p = generate_parser()

    if command is Commands.CONFIG:
        arguments.append('--file "{0}"'.format(join(prefix, 'condarc')))
    if command in (Commands.LIST, Commands.CREATE, Commands.INSTALL,
                   Commands.REMOVE, Commands.UPDATE):
        arguments.append('-p "{0}"'.format(prefix))
    if command in (Commands.CREATE, Commands.INSTALL, Commands.REMOVE, Commands.UPDATE):
        arguments.extend(["-y", "-q"])

    arguments = list(map(escape_for_winpath, arguments))
    command_line = "{0} {1}".format(command, " ".join(arguments))
    split_command_line = split(command_line)

    args = p.parse_args(split_command_line)
    context._set_argparse_args(args)
    init_loggers(context)
    print("\n\nEXECUTING COMMAND >>> $ conda %s\n\n" % command_line, file=sys.stderr)
    with stderr_log_level(TEST_LOG_LEVEL, 'conda'), stderr_log_level(TEST_LOG_LEVEL, 'requests'):
        with argv(['python_api'] + split_command_line), captured() as c:
            if use_exception_handler:
                conda_exception_handler(do_call, args, p)
            else:
                do_call(args, p)
    print(c.stderr, file=sys.stderr)
    print(c.stdout, file=sys.stderr)
    if command is Commands.CONFIG:
        reload_config(prefix)
    return c.stdout, c.stderr


@contextmanager
def make_temp_env(*packages, **kwargs):
    name = kwargs.pop('name', None)
    prefix = kwargs.pop('prefix', None) or make_temp_prefix(name)
    assert isdir(prefix), prefix
    with disable_logger('fetch'), disable_logger('dotupdate'):
        try:
            # try to clear any config that's been set by other tests
            reset_context([os.path.join(prefix+os.sep, 'condarc')])
            run_command(Commands.CREATE, prefix, *packages, **kwargs)
            yield prefix
        finally:
            rmtree(prefix, ignore_errors=True)

@contextmanager
def make_temp_package_cache():
    prefix = make_temp_prefix()
    pkgs_dir = join(prefix, 'pkgs')
    mkdir_p(pkgs_dir)
    touch(join(pkgs_dir, PACKAGE_CACHE_MAGIC_FILE))

    try:
        with env_var('CONDA_PKGS_DIRS', pkgs_dir, reset_context):
            assert context.pkgs_dirs == (pkgs_dir,)
            yield pkgs_dir
    finally:
        rmtree(prefix, ignore_errors=True)
        if pkgs_dir in PackageCacheData._cache_:
            del PackageCacheData._cache_[pkgs_dir]

@contextmanager
def make_temp_channel(packages):
    package_reqs = [pkg.replace('-', '=') for pkg in packages]
    package_names = [pkg.split('-')[0] for pkg in packages]

    with make_temp_env(*package_reqs) as prefix:
        for package in packages:
            assert package_is_installed(prefix, package.replace('-', '='))
        data = [p for p in PrefixData(prefix).iter_records() if p['name'] in package_names]
        run_command(Commands.REMOVE, prefix, *package_names)
        for package in packages:
            assert not package_is_installed(prefix, package.replace('-', '='))
        assert package_is_installed(prefix, 'python')

    repodata = {'info': {}, 'packages': {}}
    tarfiles = {}
    for package_data in data:
        pkg_data = package_data
        fname = pkg_data['fn']
        tarfiles[fname] = join(PackageCacheData.first_writable().pkgs_dir, fname)

        pkg_data = pkg_data.dump()
        for field in ('url', 'channel', 'schannel'):
            pkg_data.pop(field, None)
        repodata['packages'][fname] = PackageRecord(**pkg_data)

    with make_temp_env() as channel:
        subchan = join(channel, context.subdir)
        noarch_dir = join(channel, 'noarch')
        channel = path_to_url(channel)
        os.makedirs(subchan)
        os.makedirs(noarch_dir)
        for fname, tar_old_path in tarfiles.items():
            tar_new_path = join(subchan, fname)
            copyfile(tar_old_path, tar_new_path)

        with open(join(subchan, 'repodata.json'), 'w') as f:
            f.write(json.dumps(repodata, cls=EntityEncoder))
        with open(join(noarch_dir, 'repodata.json'), 'w') as f:
            f.write(json.dumps({}, cls=EntityEncoder))

        yield channel


def reload_config(prefix):
    prefix_condarc = join(prefix+os.sep, 'condarc')
    reset_context([prefix_condarc])


def package_is_installed(prefix, spec):
    spec = MatchSpec(spec)
    prefix_recs = tuple(PrefixData(prefix).query(spec))
    if len(prefix_recs) > 1:
        raise AssertionError("Multiple packages installed.%s" 
                             % (dashlist(prec.dist_str() for prec in prefix_recs)))
    return bool(len(prefix_recs))


def get_conda_list_tuple(prefix, package_name):
    stdout, stderr = run_command(Commands.LIST, prefix)
    stdout_lines = stdout.split('\n')
    package_line = next((line for line in stdout_lines
                         if line.lower().startswith(package_name + " ")), None)
    return package_line.split()


def get_shortcut_dir():
    assert on_win
    user_mode = 'user' if exists(join(sys.prefix, u'.nonadmin')) else 'system'
    try:
        from menuinst.win32 import dirs_src as win_locations
        return win_locations[user_mode]["start"][0]
    except ImportError:
        try:
            from menuinst.win32 import dirs as win_locations
            return win_locations[user_mode]["start"]
        except ImportError:
            raise


@pytest.mark.integration
class IntegrationTests(TestCase):

    def setUp(self):
        PackageCacheData.clear()

    def test_install_python2_and_search(self):
        with env_var('CONDA_ALLOW_NON_CHANNEL_URLS', 'true', reset_context):
            with make_temp_env("python=2") as prefix:
                assert exists(join(prefix, PYTHON_BINARY))
                assert package_is_installed(prefix, 'python=2')

                # regression test for #4513
                run_command(Commands.CONFIG, prefix, "--add channels https://repo.continuum.io/pkgs/not-a-channel")
                stdout, stderr = run_command(Commands.SEARCH, prefix, "python --json")
                packages = json.loads(stdout)
                assert len(packages) >= 1

                stdout, stderr = run_command(Commands.SEARCH, prefix, "python --json --envs")
                envs_result = json.loads(stdout)
                assert any(match['location'] == prefix for match in envs_result)

                stdout, stderr = run_command(Commands.SEARCH, prefix, "python --envs")
                assert prefix in stdout

    def test_create_install_update_remove_smoketest(self):
        with make_temp_env("python=3.5") as prefix:
            assert exists(join(prefix, PYTHON_BINARY))
            assert package_is_installed(prefix, 'python=3')

            run_command(Commands.INSTALL, prefix, 'flask=0.10')
            assert package_is_installed(prefix, 'flask=0.10.1')
            assert package_is_installed(prefix, 'python=3')

            run_command(Commands.INSTALL, prefix, '--force-reinstall', 'flask=0.10')
            assert package_is_installed(prefix, 'flask=0.10.1')
            assert package_is_installed(prefix, 'python=3')

            run_command(Commands.UPDATE, prefix, 'flask')
            assert not package_is_installed(prefix, 'flask=0.10.1')
            assert package_is_installed(prefix, 'flask')
            assert package_is_installed(prefix, 'python=3')

            run_command(Commands.REMOVE, prefix, 'flask')
            assert not package_is_installed(prefix, 'flask=0.*')
            assert package_is_installed(prefix, 'python=3')

            stdout, stderr = run_command(Commands.LIST, prefix, '--revisions')
            assert not stderr
            assert " (rev 4)\n" in stdout
            assert " (rev 5)\n" not in stdout

            run_command(Commands.INSTALL, prefix, '--revision 0')
            assert not package_is_installed(prefix, 'flask')
            assert package_is_installed(prefix, 'python=3')

    def test_safety_checks(self):
        # This test uses https://anaconda.org/conda-test/spiffy-test-app/0.5/download/noarch/spiffy-test-app-0.5-pyh6afbcc8_0.tar.bz2
        # which is a modification of https://anaconda.org/conda-test/spiffy-test-app/1.0/download/noarch/spiffy-test-app-1.0-pyh6afabb7_0.tar.bz2
        # as documented in info/README within that package.
        # I also had to fix the post-link script in the package by adding quotation marks to handle
        # spaces in path names.

        with make_temp_env() as prefix:
            with open(join(prefix, 'condarc'), 'a') as fh:
                fh.write("safety_checks: enabled\n")
            reload_config(prefix)
            assert context.safety_checks is SafetyChecks.enabled

            with pytest.raises(CondaMultiError) as exc:
                run_command(Commands.INSTALL, prefix, '-c conda-test spiffy-test-app=0.5')

            error_message = text_type(exc.value)
            message1 = dals("""
            The path 'site-packages/spiffy_test_app-1.0-py2.7.egg-info/top_level.txt'
            has an incorrect size.
              reported size: 32 bytes
              actual size: 16 bytes
            """)
            message2 = dals("""
            The path 'site-packages/spiffy_test_app/__init__.py'
            has a sha256 mismatch.
              reported sha256: 1234567890123456789012345678901234567890123456789012345678901234
              actual sha256: 32d822669b582f82da97225f69e3ef01ab8b63094e447a9acca148a6e79afbed
            """)
            assert message1 in error_message
            assert message2 in error_message

            with open(join(prefix, 'condarc'), 'a') as fh:
                fh.write("safety_checks: warn\n")
            reload_config(prefix)
            assert context.safety_checks is SafetyChecks.warn

            stdout, stderr = run_command(Commands.INSTALL, prefix, '-c conda-test spiffy-test-app=0.5')
            assert message1 in stderr
            assert message2 in stderr
            assert package_is_installed(prefix, "spiffy-test-app=0.5")

        with make_temp_env() as prefix:
            with open(join(prefix, 'condarc'), 'a') as fh:
                fh.write("safety_checks: disabled\n")
            reload_config(prefix)
            assert context.safety_checks is SafetyChecks.disabled

            stdout, stderr = run_command(Commands.INSTALL, prefix, '-c conda-test spiffy-test-app=0.5')
            assert message1 not in stderr
            assert message2 not in stderr
            assert package_is_installed(prefix, "spiffy-test-app=0.5")

    def test_json_create_install_update_remove(self):
        # regression test for #5384

        def assert_json_parsable(content):
            string = None
            try:
                for string in content and content.split('\0') or ():
                    json.loads(string)
            except Exception as e:
                log.warn(
                    "Problem parsing json output.\n"
                    "  content: %s\n"
                    "  string: %s\n"
                    "  error: %r",
                    content, string, e
                )
                raise

        try:
            prefix = make_temp_prefix(str(uuid4())[:7])

            stdout, stderr = run_command(Commands.CREATE, prefix, "python=3.5 --json --dry-run", use_exception_handler=True)
            assert_json_parsable(stdout)

            # regression test for #5825
            # contents of LINK and UNLINK is expected to have Dist format
            json_obj = json.loads(stdout)
            dist_dump = json_obj['actions']['LINK'][0]
            assert 'dist_name' in dist_dump

            stdout, stderr = run_command(Commands.CREATE, prefix, "python=3.5 --json")
            assert_json_parsable(stdout)
            assert not stderr
            json_obj = json.loads(stdout)
            dist_dump = json_obj['actions']['LINK'][0]
            assert 'dist_name' in dist_dump

            stdout, stderr = run_command(Commands.INSTALL, prefix, 'flask=0.10 --json')
            assert_json_parsable(stdout)
            assert not stderr
            assert package_is_installed(prefix, 'flask=0.10.1')
            assert package_is_installed(prefix, 'python=3')

            # Test force reinstall
            stdout, stderr = run_command(Commands.INSTALL, prefix, '--force-reinstall', 'flask=0.10', '--json')
            assert_json_parsable(stdout)
            assert not stderr
            assert package_is_installed(prefix, 'flask=0.10.1')
            assert package_is_installed(prefix, 'python=3')

            stdout, stderr = run_command(Commands.UPDATE, prefix, 'flask --json')
            assert_json_parsable(stdout)
            assert not stderr
            assert not package_is_installed(prefix, 'flask=0.10.1')
            assert package_is_installed(prefix, 'flask')
            assert package_is_installed(prefix, 'python=3')

            stdout, stderr = run_command(Commands.REMOVE, prefix, 'flask --json')
            assert_json_parsable(stdout)
            assert not stderr
            assert not package_is_installed(prefix, 'flask=0.*')
            assert package_is_installed(prefix, 'python=3')

            # regression test for #5825
            # contents of LINK and UNLINK is expected to have Dist format
            json_obj = json.loads(stdout)
            dist_dump = json_obj['actions']['UNLINK'][0]
            assert 'dist_name' in dist_dump

            stdout, stderr = run_command(Commands.LIST, prefix, '--revisions --json')
            assert not stderr
            json_obj = json.loads(stdout)
            assert len(json_obj) == 5
            assert json_obj[4]["rev"] == 4

            stdout, stderr = run_command(Commands.INSTALL, prefix, '--revision 0', '--json')
            assert_json_parsable(stdout)
            assert not stderr
            assert not package_is_installed(prefix, 'flask')
            assert package_is_installed(prefix, 'python=3')
        finally:
            rmtree(prefix, ignore_errors=True)

    def test_not_writable_env_raises_EnvironmentNotWritableError(self):
        with make_temp_env() as prefix:
            make_read_only(join(prefix, PREFIX_MAGIC_FILE))
            stdout, stderr = run_command(Commands.INSTALL, prefix, "openssl", use_exception_handler=True)
            assert "EnvironmentNotWritableError" in stderr
            assert prefix in stderr

    def test_conda_update_package_not_installed(self):
        with make_temp_env() as prefix:
            with pytest.raises(PackageNotInstalledError):
                run_command(Commands.UPDATE, prefix, "sqlite openssl")

            with pytest.raises(CondaError) as conda_error:
                run_command(Commands.UPDATE, prefix, "conda-forge::*")
            assert conda_error.value.message.startswith("Invalid spec for 'conda update'")

    def test_noarch_python_package_with_entry_points(self):
        with make_temp_env("-c conda-test flask") as prefix:
            py_ver = get_python_version_for_prefix(prefix)
            sp_dir = get_python_site_packages_short_path(py_ver)
            py_file = sp_dir + "/flask/__init__.py"
            pyc_file = pyc_path(py_file, py_ver)
            assert isfile(join(prefix, py_file))
            assert isfile(join(prefix, pyc_file))
            exe_path = join(prefix, get_bin_directory_short_path(), 'flask')
            if on_win:
                exe_path += ".exe"
            assert isfile(exe_path)

            run_command(Commands.REMOVE, prefix, "flask")

            assert not isfile(join(prefix, py_file))
            assert not isfile(join(prefix, pyc_file))
            assert not isfile(exe_path)

    def test_noarch_python_package_without_entry_points(self):
        # regression test for #4546
        with make_temp_env("-c conda-test itsdangerous") as prefix:
            py_ver = get_python_version_for_prefix(prefix)
            sp_dir = get_python_site_packages_short_path(py_ver)
            py_file = sp_dir + "/itsdangerous.py"
            pyc_file = pyc_path(py_file, py_ver)
            assert isfile(join(prefix, py_file))
            assert isfile(join(prefix, pyc_file))

            run_command(Commands.REMOVE, prefix, "itsdangerous")

            assert not isfile(join(prefix, py_file))
            assert not isfile(join(prefix, pyc_file))

    def test_noarch_python_package_reinstall_on_pyver_change(self):
        with make_temp_env("-c conda-test itsdangerous python=3") as prefix:
            py_ver = get_python_version_for_prefix(prefix)
            assert py_ver.startswith('3')
            sp_dir = get_python_site_packages_short_path(py_ver)
            py_file = sp_dir + "/itsdangerous.py"
            pyc_file_py3 = pyc_path(py_file, py_ver)
            assert isfile(join(prefix, py_file))
            assert isfile(join(prefix, pyc_file_py3))

            run_command(Commands.INSTALL, prefix, "python=2")
            assert not isfile(join(prefix, pyc_file_py3))  # python3 pyc file should be gone

            py_ver = get_python_version_for_prefix(prefix)
            assert py_ver.startswith('2')
            sp_dir = get_python_site_packages_short_path(py_ver)
            py_file = sp_dir + "/itsdangerous.py"
            pyc_file_py2 = pyc_path(py_file, py_ver)

            assert isfile(join(prefix, py_file))
            assert isfile(join(prefix, pyc_file_py2))

    def test_noarch_generic_package(self):
        with make_temp_env("-c conda-test font-ttf-inconsolata") as prefix:
            assert isfile(join(prefix, 'fonts', 'Inconsolata-Regular.ttf'))

    def test_override_channels(self):
        with pytest.raises(OperationNotAllowed):
            with env_var('CONDA_OVERRIDE_CHANNELS_ENABLED', 'no', reset_context):
                with make_temp_env("--override-channels python") as prefix:
                    assert prefix

        with pytest.raises(CommandArgumentError):
            with make_temp_env("--override-channels python") as prefix:
                assert prefix

        stdout, stderr = run_command(Commands.SEARCH, None, "--override-channels -c conda-test flask --json")
        assert not stderr
        assert len(json.loads(stdout)["flask"]) < 3
        assert json.loads(stdout)["flask"][0]["noarch"] == "python"

    def test_create_empty_env(self):
        with make_temp_env() as prefix:
            assert exists(join(prefix, 'conda-meta/history'))

            list_output = run_command(Commands.LIST, prefix)
            stdout = list_output[0]
            stderr = list_output[1]
            expected_output = """# packages in environment at %s:
#
# Name                    Version                   Build  Channel
""" % prefix
            self.assertEqual(stdout, expected_output)
            self.assertEqual(stderr, '')

            revision_output = run_command(Commands.LIST, prefix, '--revisions')
            stdout = revision_output[0]
            stderr = revision_output[1]
            assert stderr == ''
            self.assertIsInstance(stdout, str)

    @pytest.mark.skipif(on_win and context.subdir == "win-32", reason="conda-forge doesn't do win-32")
    def test_strict_channel_priority(self):
        stdout, stderr = run_command(
            Commands.CREATE, "/",
            "-c conda-forge -c defaults python=3.6 fiona --strict-channel-priority --dry-run --json",
            use_exception_handler=True
        )
        assert not stderr
        json_obj = json_loads(stdout)
        channel_groups = groupby("channel",json_obj["actions"]["LINK"])
        # conda-forge should be the only channel in the solution on unix
        assert list(channel_groups) == ["conda-forge"]

    def test_strict_resolve_get_reduced_index(self):
        channels = (Channel("defaults"),)
        specs = (MatchSpec("anaconda"),)
        index = get_reduced_index(None, channels, context.subdirs, specs)
        r = Resolve(index, channels=channels)
        with env_var("CONDA_CHANNEL_PRIORITY", "strict", reset_context):
            reduced_index = r.get_reduced_index(specs)
            channel_name_groups = {
                name: {prec.channel.name for prec in group}
                for name, group in iteritems(groupby("name", reduced_index))
            }
            channel_name_groups = {
                name: channel_names for name, channel_names in iteritems(channel_name_groups)
                if len(channel_names) > 1
            }
            assert {} == channel_name_groups

    def test_list_with_pip_no_binary(self):
        from conda.exports import rm_rf as _rm_rf
        with make_temp_env("python=3.5 pip") as prefix:
            check_call(PYTHON_BINARY + " -m pip install --no-binary flask flask==0.10.1",
                       cwd=prefix, shell=True)
            PrefixData._cache_.clear()
            stdout, stderr = run_command(Commands.LIST, prefix)
            stdout_lines = stdout.split('\n')
            assert any(line.endswith("pypi") for line in stdout_lines
                       if line.lower().startswith("flask"))

            # regression test for #5847
            #   when using rm_rf on a directory
            assert prefix in PrefixData._cache_
            _rm_rf(join(prefix, get_python_site_packages_short_path("3.5")))
            assert prefix not in PrefixData._cache_

    def test_list_with_pip_wheel(self):
        from conda.exports import rm_rf as _rm_rf
        with make_temp_env("python=3.6 pip") as prefix:
            check_call(PYTHON_BINARY + " -m pip install flask==0.10.1",
                       cwd=prefix, shell=True)
            PrefixData._cache_.clear()
            stdout, stderr = run_command(Commands.LIST, prefix)
            stdout_lines = stdout.split('\n')
            assert any(line.endswith("pypi") for line in stdout_lines
                       if line.lower().startswith("flask"))

            # regression test for #3433
            run_command(Commands.INSTALL, prefix, "python=3.5")
            assert package_is_installed(prefix, 'python=3.5')

            # regression test for #5847
            #   when using rm_rf on a file
            assert prefix in PrefixData._cache_
            _rm_rf(join(prefix, get_python_site_packages_short_path("3.5")), "os.py")
            assert prefix not in PrefixData._cache_

        # regression test for #5980, related to #5847
        with make_temp_env() as prefix:
            assert isdir(prefix)
            assert prefix in PrefixData._cache_

            rmtree(prefix)
            assert not isdir(prefix)
            assert prefix in PrefixData._cache_

            _rm_rf(prefix)
            assert not isdir(prefix)
            assert prefix not in PrefixData._cache_

    def test_install_tarball_from_local_channel(self):
        # Regression test for #2812
        # install from local channel
        with make_temp_env() as prefix, make_temp_channel(["flask-0.10.1"]) as channel:
            run_command(Commands.INSTALL, prefix, '-c', channel, 'flask=0.10.1', '--json')
            assert package_is_installed(prefix, channel + '::' + 'flask')
            flask_fname = [p for p in PrefixData(prefix).iter_records() if p['name'] == 'flask'][0]['fn']

            run_command(Commands.REMOVE, prefix, 'flask')
            assert not package_is_installed(prefix, 'flask=0')

            # Regression test for 2970
            # install from build channel as a tarball
            tar_path = join(PackageCacheData.first_writable().pkgs_dir, flask_fname)
            conda_bld = join(dirname(PackageCacheData.first_writable().pkgs_dir), 'conda-bld')
            conda_bld_sub = join(conda_bld, context.subdir)
            if not isdir(conda_bld_sub):
                os.makedirs(conda_bld_sub)
            tar_bld_path = join(conda_bld_sub, basename(tar_path))
            copyfile(tar_path, tar_bld_path)
            # CondaFileNotFoundError: '/home/travis/virtualenv/python2.7.9/conda-bld/linux-64/flask-0.10.1-py27_2.tar.bz2'.
            run_command(Commands.INSTALL, prefix, tar_bld_path)
            assert package_is_installed(prefix, 'flask')

            # Regression test for #462
            with make_temp_env(tar_bld_path) as prefix2:
                assert package_is_installed(prefix2, 'flask')

    def test_tarball_install_and_bad_metadata(self):
        with make_temp_env("python flask=0.10.1 --json") as prefix:
            assert package_is_installed(prefix, 'flask==0.10.1')
            flask_data = [p for p in PrefixData(prefix).iter_records() if p['name'] == 'flask'][0]
            run_command(Commands.REMOVE, prefix, 'flask')
            assert not package_is_installed(prefix, 'flask==0.10.1')
            assert package_is_installed(prefix, 'python')

            flask_fname = flask_data['fn']
            tar_old_path = join(PackageCacheData.first_writable().pkgs_dir, flask_fname)

            assert isfile(tar_old_path)

            with pytest.raises(DryRunExit):
                run_command(Commands.INSTALL, prefix, tar_old_path, "--dry-run")
                assert not package_is_installed(prefix, 'flask=0.*')

            # regression test for #2886 (part 1 of 2)
            # install tarball from package cache, default channel
            run_command(Commands.INSTALL, prefix, tar_old_path)
            assert package_is_installed(prefix, 'flask=0.*')

            # regression test for #2626
            # install tarball with full path, outside channel
            tar_new_path = join(prefix, flask_fname)
            copyfile(tar_old_path, tar_new_path)
            run_command(Commands.INSTALL, prefix, '"%s"' % tar_new_path)
            assert package_is_installed(prefix, 'flask=0')

            # regression test for #2626
            # install tarball with relative path, outside channel
            run_command(Commands.REMOVE, prefix, 'flask')
            assert not package_is_installed(prefix, 'flask=0.10.1')
            tar_new_path = relpath(tar_new_path)
            run_command(Commands.INSTALL, prefix, '"%s"' % tar_new_path)
            assert package_is_installed(prefix, 'flask=0')

            # regression test for #2886 (part 2 of 2)
            # install tarball from package cache, local channel
            run_command(Commands.REMOVE, prefix, 'flask', '--json')
            assert not package_is_installed(prefix, 'flask=0')
            run_command(Commands.INSTALL, prefix, tar_old_path)
            # The last install was from the `local::` channel
            assert package_is_installed(prefix, 'flask')

            # regression test for #2599
            # ignore json files in conda-meta that don't conform to name-version-build.json
            if not on_win:
                # xz is only a python dependency on unix
                xz_prec = next(PrefixData(prefix).query("xz"))
                dist_name = xz_prec.dist_str().split('::')[-1]
                xz_prefix_data_json_path = join(prefix, 'conda-meta', dist_name + '.json')
                copyfile(xz_prefix_data_json_path,
                         join(prefix, 'conda-meta', 'xz.json'))
                rm_rf(xz_prefix_data_json_path)
                assert not lexists(xz_prefix_data_json_path)
                PrefixData._cache_ = {}
                assert not package_is_installed(prefix, 'xz')

    @pytest.mark.skipif(on_win, reason="windows python doesn't depend on readline")
    def test_update_with_pinned_packages(self):
        # regression test for #6914
        with make_temp_env("python=2.7.12") as prefix:
            assert package_is_installed(prefix, "readline=6.2")
            open(join(prefix, 'conda-meta', 'history'), 'w').close()
            PrefixData._cache_.clear()
            run_command(Commands.UPDATE, prefix, "readline")
            assert package_is_installed(prefix, "readline")
            assert not package_is_installed(prefix, "readline=6.2")
            assert package_is_installed(prefix, "python=2.7")
            assert not package_is_installed(prefix, "python=2.7.12")

    def test_remove_all(self):
        with make_temp_env("python") as prefix:
            assert exists(join(prefix, PYTHON_BINARY))
            assert package_is_installed(prefix, 'python')

            # regression test for #2154
            with pytest.raises(PackagesNotFoundError) as exc:
                run_command(Commands.REMOVE, prefix, 'python', 'foo', 'numpy')
            assert repr(exc.value) == dals("""
            PackagesNotFoundError: The following packages are missing from the target environment:
              - foo
              - numpy
            """)

            run_command(Commands.REMOVE, prefix, '--all')
            assert not exists(prefix)

    @pytest.mark.skipif(on_win, reason="windows usually doesn't support symlinks out-of-the box")
    @patch('conda.core.link.hardlink_supported', side_effect=lambda x, y: False)
    def test_allow_softlinks(self, hardlink_supported_mock):
        hardlink_supported_mock._result_cache.clear()
        with env_var("CONDA_ALLOW_SOFTLINKS", "true", reset_context):
            with make_temp_env("pip") as prefix:
                assert islink(join(prefix, get_python_site_packages_short_path(
                    get_python_version_for_prefix(prefix)), 'pip', '__init__.py'))
        hardlink_supported_mock._result_cache.clear()

    @pytest.mark.skipif(on_win, reason="nomkl not present on windows")
    def test_remove_features(self):
        with make_temp_env("python=2 numpy=1.13 nomkl") as prefix:
            assert exists(join(prefix, PYTHON_BINARY))
            assert package_is_installed(prefix, 'numpy')
            assert package_is_installed(prefix, 'nomkl')
            assert not package_is_installed(prefix, 'mkl')

            # A consequence of discontinuing use of the 'features' key and instead
            # using direct dependencies is that removing the feature means that
            # packages associated with the track_features base package are completely removed
            # and not replaced with equivalent non-variant packages as before.
            run_command(Commands.REMOVE, prefix, '--features', 'nomkl')
            # assert assert_package_is_installed(prefix, 'numpy')  # removed per above comment
            assert not package_is_installed(prefix, 'nomkl')
            # assert_package_is_installed(prefix, 'mkl')  # removed per above comment

    @pytest.mark.skipif(on_win and context.bits == 32, reason="no 32-bit windows python on conda-forge")
    @pytest.mark.skipif(on_win and datetime.now() <= datetime(2018, 10, 1), reason="conda-forge repodata needs vc patching")
    def test_dash_c_usage_replacing_python(self):
        # Regression test for #2606
        with make_temp_env("-c conda-forge python=3.5") as prefix:
            assert exists(join(prefix, PYTHON_BINARY))
            assert package_is_installed(prefix, 'conda-forge::python=3.5')
            run_command(Commands.INSTALL, prefix, "decorator")
            assert package_is_installed(prefix, 'conda-forge::python=3.5')

            with make_temp_env('--clone "%s"' % prefix) as clone_prefix:
                assert package_is_installed(clone_prefix, 'conda-forge::python=3.5')
                assert package_is_installed(clone_prefix, "decorator")

            # Regression test for #2645
            fn = glob(join(prefix, 'conda-meta', 'python-3.5*.json'))[-1]
            with open(fn) as f:
                data = json.load(f)
            for field in ('url', 'channel', 'schannel'):
                if field in data:
                    del data[field]
            with open(fn, 'w') as f:
                json.dump(data, f)
            PrefixData._cache_ = {}

            with make_temp_env('-c conda-forge --clone "%s"' % prefix) as clone_prefix:
                assert package_is_installed(clone_prefix, 'python=3.5')
                assert package_is_installed(clone_prefix, 'decorator')

    def test_install_prune_flag(self):
        with make_temp_env("python=3 flask") as prefix:
            assert package_is_installed(prefix, 'flask')
            assert package_is_installed(prefix, 'python=3')
            run_command(Commands.REMOVE, prefix, "flask")
            assert not package_is_installed(prefix, 'flask')
            assert package_is_installed(prefix, 'itsdangerous')
            assert package_is_installed(prefix, 'python=3')

            run_command(Commands.INSTALL, prefix, 'pytz --prune')

            assert not package_is_installed(prefix, 'itsdangerous')
            assert package_is_installed(prefix, 'pytz')
            assert package_is_installed(prefix, 'python=3')

    @pytest.mark.skipif(on_win, reason="readline is only a python dependency on unix")
    def test_remove_force_remove_flag(self):
        with make_temp_env("python") as prefix:
            assert package_is_installed(prefix, 'readline')
            assert package_is_installed(prefix, 'python')

            run_command(Commands.REMOVE, prefix, 'readline --force-remove')
            assert not package_is_installed(prefix, 'readline')
            assert package_is_installed(prefix, 'python')

    def test_install_force_reinstall_flag(self):
        with make_temp_env("python") as prefix:
            stdout, stderr = run_command(Commands.INSTALL, prefix,
                                         "--json --dry-run --force-reinstall python",
                                         use_exception_handler=True)
            output_obj = json.loads(stdout.strip())
            unlink_actions = output_obj['actions']['UNLINK']
            link_actions = output_obj['actions']['LINK']
            assert len(unlink_actions) == len(link_actions) == 1
            assert unlink_actions[0] == link_actions[0]
            assert unlink_actions[0]['name'] == 'python'

    def test_create_no_deps_flag(self):
        with make_temp_env("python=2 flask --no-deps") as prefix:
            assert package_is_installed(prefix, 'flask')
            assert package_is_installed(prefix, 'python=2')
            assert not package_is_installed(prefix, 'openssl')
            assert not package_is_installed(prefix, 'itsdangerous')

    def test_create_only_deps_flag(self):
        with make_temp_env("python=2 flask --only-deps") as prefix:
            assert not package_is_installed(prefix, 'flask')
            assert package_is_installed(prefix, 'python')
            if not on_win:
                # python on windows doesn't actually have real dependencies
                assert package_is_installed(prefix, 'openssl')
            assert package_is_installed(prefix, 'itsdangerous')

    @pytest.mark.xfail(on_win and datetime.now() < datetime(2018, 9, 15),
                       reason="need to talk with @msarahan about blas patches on Windows",
                       strict=True)
    def test_install_features(self):
        with make_temp_env("python=2 numpy=1.13 nomkl") as prefix:
            assert package_is_installed(prefix, "numpy")
            assert package_is_installed(prefix, "nomkl")
            assert not package_is_installed(prefix, "mkl")
            numpy_prec = PrefixData(prefix).get("numpy")
            assert "nomkl" in numpy_prec.build

        with make_temp_env("python=2 numpy=1.13") as prefix:
            assert package_is_installed(prefix, "numpy")
            assert not package_is_installed(prefix, "nomkl")
            assert package_is_installed(prefix, "mkl")
            numpy_prec = PrefixData(prefix).get("numpy")
            assert "nomkl" not in numpy_prec.build

            run_command(Commands.INSTALL, prefix, "nomkl")
            assert package_is_installed(prefix, "numpy")
            assert package_is_installed(prefix, "nomkl")
            assert package_is_installed(prefix, "mkl")  # it's fine for mkl to still be here I guess
            numpy_prec = PrefixData(prefix).get("numpy")
            assert "nomkl" in numpy_prec.build

    def test_clone_offline_simple(self):
        with make_temp_env("python flask=0.10.1") as prefix:
            assert package_is_installed(prefix, 'flask=0.10.1')
            assert package_is_installed(prefix, 'python')

            with make_temp_env('--clone "%s"' % prefix, "--offline") as clone_prefix:
                assert context.offline
                assert package_is_installed(clone_prefix, 'flask=0.10.1')
                assert package_is_installed(clone_prefix, 'python')

            with env_var('CONDA_DISALLOWED_PACKAGES', 'python', reset_context):
                with pytest.raises(DisallowedPackageError) as exc:
                    with make_temp_env('--clone "%s"' % prefix, "--offline"):
                        pass
                assert exc.value.dump_map()['package_ref']['name'] == 'python'

    def test_conda_config_describe(self):
        with make_temp_env() as prefix:
            stdout, stderr = run_command(Commands.CONFIG, prefix, "--describe")
            assert not stderr
            skip_categories = ('CLI-only', 'Hidden and Undocumented')
            documented_parameter_names = chain.from_iterable((
                parameter_names for category, parameter_names in iteritems(context.category_map)
                if category not in skip_categories
            ))

            for param_name in documented_parameter_names:
                assert re.search(r'^# # %s \(' % param_name, stdout, re.MULTILINE), param_name

            stdout, stderr = run_command(Commands.CONFIG, prefix, "--describe --json")
            assert not stderr
            json_obj = json.loads(stdout.strip())
            assert len(json_obj) >= 42
            assert 'description' in json_obj[0]

            with env_var('CONDA_QUIET', 'yes', reset_context):
                stdout, stderr = run_command(Commands.CONFIG, prefix, "--show-sources")
                assert not stderr
                assert 'envvars' in stdout.strip()

                stdout, stderr = run_command(Commands.CONFIG, prefix, "--show-sources --json")
                assert not stderr
                json_obj = json.loads(stdout.strip())
                assert json_obj['envvars'] == {'quiet': True}
                assert json_obj['cmd_line'] == {'json': True}

            run_command(Commands.CONFIG, prefix, "--set changeps1 false")
            with pytest.raises(CondaError):
                run_command(Commands.CONFIG, prefix, "--write-default")

            rm_rf(join(prefix, 'condarc'))
            run_command(Commands.CONFIG, prefix, "--write-default")

            with open(join(prefix, 'condarc')) as fh:
                data = fh.read()

            for param_name in documented_parameter_names:
                assert re.search(r'^# %s \(' % param_name, data, re.MULTILINE), param_name

            stdout, stderr = run_command(Commands.CONFIG, prefix, "--describe --json")
            assert not stderr
            json_obj = json.loads(stdout.strip())
            assert len(json_obj) >= 42
            assert 'description' in json_obj[0]

            with env_var('CONDA_QUIET', 'yes', reset_context):
                stdout, stderr = run_command(Commands.CONFIG, prefix, "--show-sources")
                assert not stderr
                assert 'envvars' in stdout.strip()

                stdout, stderr = run_command(Commands.CONFIG, prefix, "--show-sources --json")
                assert not stderr
                json_obj = json.loads(stdout.strip())
                assert json_obj['envvars'] == {'quiet': True}
                assert json_obj['cmd_line'] == {'json': True}

    def test_conda_config_validate(self):
        with make_temp_env() as prefix:
            run_command(Commands.CONFIG, prefix, "--set ssl_verify no")
            stdout, stderr = run_command(Commands.CONFIG, prefix, "--validate")
            assert not stdout
            assert not stderr

            try:
                with open(join(prefix, 'condarc'), 'a') as fh:
                    fh.write('default_python: anaconda\n')
                    fh.write('ssl_verify: /path/doesnt/exist\n')
                reload_config(prefix)

                with pytest.raises(CondaMultiError) as exc:
                    run_command(Commands.CONFIG, prefix, "--validate")

                assert len(exc.value.errors) == 2
                assert "must be a boolean, a path to a certificate bundle file, or a path to a directory containing certificates of trusted CAs" in str(exc.value)
                assert "default_python value 'anaconda' not of the form '[23].[0-9]'" in str(exc.value)
            finally:
                reset_context()

    def test_rpy_search(self):
        with make_temp_env("python=3.5") as prefix:
            run_command(Commands.CONFIG, prefix, "--add channels https://repo.anaconda.com/pkgs/free")
            run_command(Commands.CONFIG, prefix, "--remove channels defaults")
            stdout, stderr = run_command(Commands.CONFIG, prefix, "--show", "--json")
            json_obj = json_loads(stdout)
            assert 'defaults' not in json_obj['channels']

            assert package_is_installed(prefix, 'python')
            assert 'r' not in context.channels

            # assert conda search cannot find rpy2
            stdout, stderr = run_command(Commands.SEARCH, prefix, "rpy2", "--json", use_exception_handler=True)
            json_obj = json_loads(stdout.replace("Fetching package metadata ...", "").strip())
            assert json_obj['exception_name'] == 'PackagesNotFoundError'

            # add r channel
            run_command(Commands.CONFIG, prefix, "--add channels r")
            stdout, stderr = run_command(Commands.CONFIG, prefix, "--show", "--json")
            json_obj = json_loads(stdout)
            assert 'r' in json_obj['channels']

            # assert conda search can now find rpy2
            stdout, stderr = run_command(Commands.SEARCH, prefix, "rpy2", "--json")
            json_obj = json_loads(stdout.replace("Fetching package metadata ...", "").strip())

    def test_clone_offline_multichannel_with_untracked(self):
        with make_temp_env("python=3.5") as prefix:
            run_command(Commands.CONFIG, prefix, "--add channels https://repo.anaconda.com/pkgs/free")
            run_command(Commands.CONFIG, prefix, "--remove channels defaults")

            run_command(Commands.INSTALL, prefix, "-c conda-test flask")

            touch(join(prefix, 'test.file'))  # untracked file
            with make_temp_env("--clone '%s'" % prefix, "--offline") as clone_prefix:
                assert context.offline
                assert package_is_installed(clone_prefix, 'python=3.5')
                assert package_is_installed(clone_prefix, 'flask=0.11.1=py_0')
                assert isfile(join(clone_prefix, 'test.file'))  # untracked file

    def test_package_pinning(self):
        with make_temp_env("python=2.7 itsdangerous=0.23 pytz=2015.7") as prefix:
            assert package_is_installed(prefix, "itsdangerous=0.23")
            assert package_is_installed(prefix, "python=2.7")
            assert package_is_installed(prefix, "pytz=2015.7")

            with open(join(prefix, 'conda-meta', 'pinned'), 'w') as fh:
                fh.write("itsdangerous 0.23\n")

            run_command(Commands.UPDATE, prefix, "--all")
            assert package_is_installed(prefix, "itsdangerous=0.23")
            # assert not package_is_installed(prefix, "python-3.5")  # should be python-3.6, but it's not because of add_defaults_to_specs
            assert package_is_installed(prefix, "python=2.7")

            assert not package_is_installed(prefix, "pytz=2015.7")
            assert package_is_installed(prefix, "pytz")

            run_command(Commands.UPDATE, prefix, "--all --no-pin")
            assert package_is_installed(prefix, "python=2.7")
            assert not package_is_installed(prefix, "itsdangerous=0.23")

    def test_package_optional_pinning(self):
        with make_temp_env("") as prefix:
            run_command(Commands.CONFIG, prefix,
                        "--add pinned_packages", "python=3.6.1=2")
            run_command(Commands.INSTALL, prefix, "openssl")
            assert not package_is_installed(prefix, "python")
            run_command(Commands.INSTALL, prefix, "flask")
            assert package_is_installed(prefix, "python=3.6.1")

    def test_update_deps_flag_absent(self):
        with make_temp_env("python=2 itsdangerous=0.23") as prefix:
            assert package_is_installed(prefix, 'python=2')
            assert package_is_installed(prefix, 'itsdangerous=0.23')
            assert not package_is_installed(prefix, 'flask')

            run_command(Commands.INSTALL, prefix, 'flask')
            assert package_is_installed(prefix, 'python=2')
            assert package_is_installed(prefix, 'itsdangerous=0.23')
            assert package_is_installed(prefix, 'flask')

    @pytest.mark.xfail(datetime.now() < datetime(2018, 3, 1), reason="#5263", strict=True)
    def test_update_deps_flag_present(self):
        with make_temp_env("python=2 itsdangerous=0.23") as prefix:
            assert package_is_installed(prefix, 'python=2')
            assert package_is_installed(prefix, 'itsdangerous=0.23')
            assert not package_is_installed(prefix, 'flask')

            run_command(Commands.INSTALL, prefix, '--update-deps python=2 flask')
            assert package_is_installed(prefix, 'python=2')
            assert not package_is_installed(prefix, 'itsdangerous=0.23')
            assert package_is_installed(prefix, 'itsdangerous')
            assert package_is_installed(prefix, 'flask')

    @pytest.mark.skipif(True, reason="Add this test back someday.")
    # @pytest.mark.skipif(not on_win, reason="shortcuts only relevant on Windows")
    def test_shortcut_in_underscore_env_shows_message(self):
        prefix = make_temp_prefix("_" + str(uuid4())[:7])
        with make_temp_env(prefix=prefix):
            stdout, stderr = run_command(Commands.INSTALL, prefix, "console_shortcut")
            assert ("Environment name starts with underscore '_'.  "
                    "Skipping menu installation." in stderr)

    @pytest.mark.skipif(not on_win, reason="shortcuts only relevant on Windows")
    def test_shortcut_not_attempted_with_no_shortcuts_arg(self):
        prefix = make_temp_prefix("_" + str(uuid4())[:7])
        shortcut_dir = get_shortcut_dir()
        shortcut_file = join(shortcut_dir, "Anaconda Prompt ({0}).lnk".format(basename(prefix)))
        with make_temp_env(prefix=prefix):
            stdout, stderr = run_command(Commands.INSTALL, prefix, "console_shortcut",
                                         "--no-shortcuts")
            assert ("Environment name starts with underscore '_'.  Skipping menu installation."
                    not in stderr)
            assert not isfile(shortcut_file)

    @pytest.mark.skipif(not on_win, reason="shortcuts only relevant on Windows")
    def test_shortcut_creation_installs_shortcut(self):
        shortcut_dir = get_shortcut_dir()
        shortcut_dir = join(shortcut_dir, "Anaconda{0} ({1}-bit)"
                                          "".format(sys.version_info.major, config.bits))

        prefix = make_temp_prefix(str(uuid4())[:7])
        shortcut_file = join(shortcut_dir, "Anaconda Prompt ({0}).lnk".format(basename(prefix)))
        try:
            with make_temp_env("console_shortcut", prefix=prefix):
                assert package_is_installed(prefix, 'console_shortcut')
                assert isfile(shortcut_file), ("Shortcut not found in menu dir. "
                                               "Contents of dir:\n"
                                               "{0}".format(os.listdir(shortcut_dir)))

                # make sure that cleanup without specifying --shortcuts still removes shortcuts
                run_command(Commands.REMOVE, prefix, 'console_shortcut')
                assert not package_is_installed(prefix, 'console_shortcut')
                assert not isfile(shortcut_file)
        finally:
            rmtree(prefix, ignore_errors=True)
            if isfile(shortcut_file):
                os.remove(shortcut_file)

    @pytest.mark.skipif(not on_win, reason="shortcuts only relevant on Windows")
    def test_shortcut_absent_does_not_barf_on_uninstall(self):
        shortcut_dir = get_shortcut_dir()
        shortcut_dir = join(shortcut_dir, "Anaconda{0} ({1}-bit)"
                                          "".format(sys.version_info.major, config.bits))

        prefix = make_temp_prefix(str(uuid4())[:7])
        shortcut_file = join(shortcut_dir, "Anaconda Prompt ({0}).lnk".format(basename(prefix)))
        assert not isfile(shortcut_file)

        try:
            # including --no-shortcuts should not get shortcuts installed
            with make_temp_env("console_shortcut", "--no-shortcuts", prefix=prefix):
                assert package_is_installed(prefix, 'console_shortcut')
                assert not isfile(shortcut_file)

                # make sure that cleanup without specifying --shortcuts still removes shortcuts
                run_command(Commands.REMOVE, prefix, 'console_shortcut')
                assert not package_is_installed(prefix, 'console_shortcut')
                assert not isfile(shortcut_file)
        finally:
            rmtree(prefix, ignore_errors=True)
            if isfile(shortcut_file):
                os.remove(shortcut_file)

    @pytest.mark.skipif(not on_win, reason="shortcuts only relevant on Windows")
    @pytest.mark.xfail(reason="deal with this later")
    def test_shortcut_absent_when_condarc_set(self):
        shortcut_dir = get_shortcut_dir()
        shortcut_dir = join(shortcut_dir, "Anaconda{0} ({1}-bit)"
                                          "".format(sys.version_info.major, config.bits))

        prefix = make_temp_prefix(str(uuid4())[:7])
        shortcut_file = join(shortcut_dir, "Anaconda Prompt ({0}).lnk".format(basename(prefix)))
        assert not isfile(shortcut_file)

        # set condarc shortcuts: False
        run_command(Commands.CONFIG, prefix, "--set shortcuts false")
        stdout, stderr = run_command(Commands.CONFIG, prefix, "--get", "--json")
        json_obj = json_loads(stdout)
        assert json_obj['rc_path'] == join(prefix, 'condarc')
        assert json_obj['get']['shortcuts'] is False

        try:
            with make_temp_env("console_shortcut", prefix=prefix):
                # including shortcuts: False from condarc should not get shortcuts installed
                assert package_is_installed(prefix, 'console_shortcut')
                assert not isfile(shortcut_file)

                # make sure that cleanup without specifying --shortcuts still removes shortcuts
                run_command(Commands.REMOVE, prefix, 'console_shortcut')
                assert not package_is_installed(prefix, 'console_shortcut')
                assert not isfile(shortcut_file)
        finally:
            rmtree(prefix, ignore_errors=True)
            if isfile(shortcut_file):
                os.remove(shortcut_file)

    def test_create_default_packages(self):
        # Regression test for #3453
        try:
            prefix = make_temp_prefix(str(uuid4())[:7])

            # set packages
            run_command(Commands.CONFIG, prefix, "--add create_default_packages pip")
            run_command(Commands.CONFIG, prefix, "--add create_default_packages flask")
            stdout, stderr = run_command(Commands.CONFIG, prefix, "--show")
            yml_obj = yaml_load(stdout)
            assert yml_obj['create_default_packages'] == ['flask', 'pip']

            assert not package_is_installed(prefix, 'python=2')
            assert not package_is_installed(prefix, 'pytz')
            assert not package_is_installed(prefix, 'flask')

            with make_temp_env("python=2", "pytz", prefix=prefix):
                assert package_is_installed(prefix, 'python=2')
                assert package_is_installed(prefix, 'pytz')
                assert package_is_installed(prefix, 'flask')

        finally:
            rmtree(prefix, ignore_errors=True)

    def test_create_default_packages_no_default_packages(self):
        try:
            prefix = make_temp_prefix(str(uuid4())[:7])

            # set packages
            run_command(Commands.CONFIG, prefix, "--add create_default_packages pip")
            run_command(Commands.CONFIG, prefix, "--add create_default_packages flask")
            stdout, stderr = run_command(Commands.CONFIG, prefix, "--show")
            yml_obj = yaml_load(stdout)
            assert yml_obj['create_default_packages'] == ['flask', 'pip']

            assert not package_is_installed(prefix, 'python=2')
            assert not package_is_installed(prefix, 'pytz')
            assert not package_is_installed(prefix, 'flask')

            with make_temp_env("python=2", "pytz", "--no-default-packages", prefix=prefix):
                assert package_is_installed(prefix, 'python=2')
                assert package_is_installed(prefix, 'pytz')
                assert not package_is_installed(prefix, 'flask')

        finally:
            rmtree(prefix, ignore_errors=True)

    def test_create_dry_run(self):
        # Regression test for #3453
        prefix = '/some/place'
        with pytest.raises(DryRunExit):
            run_command(Commands.CREATE, prefix, "--dry-run")
        stdout, stderr = run_command(Commands.CREATE, prefix, "--dry-run", use_exception_handler=True)
        assert join('some', 'place') in stdout
        # TODO: This assert passes locally but fails on CI boxes; figure out why and re-enable
        # assert "The following empty environments will be CREATED" in stdout

        prefix = '/another/place'
        with pytest.raises(DryRunExit):
            run_command(Commands.CREATE, prefix, "flask", "--dry-run")
        stdout, stderr = run_command(Commands.CREATE, prefix, "flask", "--dry-run", use_exception_handler=True)
        assert ":flask" in stdout
        assert ":python" in stdout
        assert join('another', 'place') in stdout

    def test_create_dry_run_json(self):
        prefix = '/some/place'
        with pytest.raises(DryRunExit):
            run_command(Commands.CREATE, prefix, "flask", "--dry-run", "--json")
        stdout, stderr = run_command(Commands.CREATE, prefix, "flask", "--dry-run", "--json", use_exception_handler=True)
        loaded = json.loads(stdout)
        names = set(d['name'] for d in loaded['actions']['LINK'])
        assert "python" in names
        assert "flask" in names

    def test_packages_not_found(self):
        with make_temp_env() as prefix:
            with pytest.raises(PackagesNotFoundError) as exc:
                run_command(Commands.INSTALL, prefix, "not-a-real-package")
            assert "not-a-real-package" in text_type(exc.value)

            stdout, stderr = run_command(Commands.INSTALL, prefix, "not-a-real-package",
                                         use_exception_handler=True)
            assert "not-a-real-package" in stderr

    def test_conda_pip_interop_dependency_satisfied_by_pip(self):
        with make_temp_env("python") as prefix:
            check_call(PYTHON_BINARY + " -m pip install itsdangerous",
                       cwd=prefix, shell=True)

            PrefixData._cache_.clear()
            stdout, stderr = run_command(Commands.LIST, prefix)
            assert 'itsdangerous' in stdout
            assert not stderr

            stdout, stderr = run_command(Commands.INSTALL, prefix, 'flask --dry-run --json',
                                         use_exception_handler=True)
            json_obj = json.loads(stdout)
            print(json_obj)
            assert any(rec["name"] == "flask" for rec in json_obj["actions"]["LINK"])
            assert not any(rec["name"] == "itsdangerous" for rec in json_obj["actions"]["LINK"])

            stdout, stderr = run_command(Commands.SEARCH, prefix, "not-a-real-package", "--json",
                                         use_exception_handler=True)
            assert not stderr
            json_obj = json_loads(stdout.strip())
            assert json_obj['exception_name'] == 'PackagesNotFoundError'
            assert not len(json_obj.keys()) == 0

    @pytest.mark.skipif(context.subdir == "win-32", reason="metadata is wrong; give python2.7")
    def test_conda_pip_interop_pip_clobbers_conda(self):
        # 1. conda install old six
        # 2. pip install -U six
        # 3. conda list shows new six and deletes old conda record
        # 4. probably need to purge something with the history file too?
        with make_temp_env("six=1.9 pip=9.0.3") as prefix:
            assert package_is_installed(prefix, "six=1.9.0")
            assert package_is_installed(prefix, "python=3.5")
            output = check_output(PYTHON_BINARY + " -m pip freeze", cwd=prefix, shell=True)
            pkgs = set(ensure_text_type(v.strip()) for v in output.splitlines() if v.strip())
            assert "six==1.9.0" in pkgs

            py_ver = get_python_version_for_prefix(prefix)
            sp_dir = get_python_site_packages_short_path(py_ver)

            output = check_output(PYTHON_BINARY + " -m pip install -U six==1.10",
                                  cwd=prefix, shell=True)
            assert "Successfully installed six-1.10.0" in ensure_text_type(output)
            PrefixData._cache_.clear()
            stdout, stderr = run_command(Commands.LIST, prefix, "--json")
            assert not stderr
            json_obj = json.loads(stdout)
            six_info = next(info for info in json_obj if info["name"] == "six")
            assert six_info == {
                "base_url": "https://conda.anaconda.org/pypi",
                "build_number": 0,
                "build_string": "pypi_0",
                "channel": "pypi",
                "dist_name": "six-1.10.0-pypi_0",
                "name": "six",
                "platform": "pypi",
                "version": "1.10.0",
            }
            assert package_is_installed(prefix, "six=1.10.0")
            output = check_output(PYTHON_BINARY + " -m pip freeze", cwd=prefix, shell=True)
            pkgs = set(ensure_text_type(v.strip()) for v in output.splitlines() if v.strip())
            assert "six==1.10.0" in pkgs

            six_record = next(PrefixData(prefix).query("six"))
            print(json_dump(six_record))
            assert json_loads(json_dump(six_record)) == {
                "build": "pypi_0",
                "build_number": 0,
                "channel": "https://conda.anaconda.org/pypi",
                "constrains": [],
                "depends": [
                    "python 3.5.*"
                ],
                "files": [
                    sp_dir + "/" + "__pycache__/six.cpython-35.pyc",
                    sp_dir + "/" + "six-1.10.0.dist-info/DESCRIPTION.rst",
                    sp_dir + "/" + "six-1.10.0.dist-info/INSTALLER",
                    sp_dir + "/" + "six-1.10.0.dist-info/METADATA",
                    sp_dir + "/" + "six-1.10.0.dist-info/RECORD",
                    sp_dir + "/" + "six-1.10.0.dist-info/WHEEL",
                    sp_dir + "/" + "six-1.10.0.dist-info/metadata.json",
                    sp_dir + "/" + "six-1.10.0.dist-info/top_level.txt",
                    sp_dir + "/" + "six.py",
                ],
                "fn": "six-1.10.0.dist-info",
                "name": "six",
                "package_type": "virtual_python_wheel",
                "paths_data": {
                    "paths": [
                        {
                            "_path": sp_dir + "/" + "__pycache__/six.cpython-35.pyc",
                            "path_type": "hardlink",
                            "sha256": None,
                            "size_in_bytes": None
                        },
                        {
                            "_path": sp_dir + "/" + "six-1.10.0.dist-info/DESCRIPTION.rst",
                            "path_type": "hardlink",
                            "sha256": "QWBtSTT2zzabwJv1NQbTfClSX13m-Qc6tqU4TRL1RLs",
                            "size_in_bytes": 774
                        },
                        {
                            "_path": sp_dir + "/" + "six-1.10.0.dist-info/INSTALLER",
                            "path_type": "hardlink",
                            "sha256": "zuuue4knoyJ-UwPPXg8fezS7VCrXJQrAP7zeNuwvFQg",
                            "size_in_bytes": 4
                        },
                        {
                            "_path": sp_dir + "/" + "six-1.10.0.dist-info/METADATA",
                            "path_type": "hardlink",
                            "sha256": "5HceJsUnHof2IRamlCKO2MwNjve1eSP4rLzVQDfwpCQ",
                            "size_in_bytes": 1283
                        },
                        {
                            "_path": sp_dir + "/" + "six-1.10.0.dist-info/RECORD",
                            "path_type": "hardlink",
                            "sha256": None,
                            "size_in_bytes": None
                        },
                        {
                            "_path": sp_dir + "/" + "six-1.10.0.dist-info/WHEEL",
                            "path_type": "hardlink",
                            "sha256": "GrqQvamwgBV4nLoJe0vhYRSWzWsx7xjlt74FT0SWYfE",
                            "size_in_bytes": 110
                        },
                        {
                            "_path": sp_dir + "/" + "six-1.10.0.dist-info/metadata.json",
                            "path_type": "hardlink",
                            "sha256": "jtOeeTBubYDChl_5Ql5ZPlKoHgg6rdqRIjOz1e5Ek2U",
                            "size_in_bytes": 658
                        },
                        {
                            "_path": sp_dir + "/" + "six-1.10.0.dist-info/top_level.txt",
                            "path_type": "hardlink",
                            "sha256": "_iVH_iYEtEXnD8nYGQYpYFUvkUW9sEO1GYbkeKSAais",
                            "size_in_bytes": 4
                        },
                        {
                            "_path": sp_dir + "/" + "six.py",
                            "path_type": "hardlink",
                            "sha256": "A6hdJZVjI3t_geebZ9BzUvwRrIXo0lfwzQlM2LcKyas",
                            "size_in_bytes": 30098
                        }
                    ],
                    "paths_version": 1
                },
                "subdir": "pypi",
                "version": "1.10.0"
            }

            stdout, stderr = run_command(Commands.INSTALL, prefix, "six --satisfied-skip-solve")
            assert not stderr
            assert "All requested packages already installed." in stdout

            stdout, stderr = run_command(Commands.INSTALL, prefix, "six")
            assert not stderr
            assert package_is_installed(prefix, "six>=1.11")
            output = check_output(PYTHON_BINARY + " -m pip freeze", cwd=prefix, shell=True)
            pkgs = set(ensure_text_type(v.strip()) for v in output.splitlines() if v.strip())
            six_record = next(PrefixData(prefix).query("six"))
            assert "six==%s" % six_record.version in pkgs

            assert len(glob(join(prefix, "conda-meta", "six-*.json"))) == 1

            output = check_output(PYTHON_BINARY + " -m pip install -U six==1.10",
                                  cwd=prefix, shell=True)
            print(output)
            assert "Successfully installed six-1.10.0" in ensure_text_type(output)
            PrefixData._cache_.clear()
            assert package_is_installed(prefix, "six=1.10.0")

            stdout, stderr = run_command(Commands.REMOVE, prefix, "six")
            assert not stderr
            assert "six-1.10.0-pypi_0" in stdout
            assert not package_is_installed(prefix, "six")

            assert not glob(join(prefix, sp_dir, "six*"))

    def test_conda_pip_interop_conda_editable_package(self):
        with make_temp_env("python=2.7") as prefix:
            assert package_is_installed(prefix, "python")

            # install an "editable" urllib3 that cannot be managed
            output = check_output(PYTHON_BINARY + " -m pip install -e git://github.com/urllib3/urllib3.git@1.19.1#egg=urllib3",
                                  cwd=prefix, shell=True)
            print(output)
            assert isfile(join(prefix, "src", "urllib3", "urllib3", "__init__.py"))
            PrefixData._cache_.clear()
            assert package_is_installed(prefix, "urllib3")
            urllib3_record = next(PrefixData(prefix).query("urllib3"))
            urllib3_record_dump = urllib3_record.dump()
            files = urllib3_record_dump.pop("files")
            paths_data = urllib3_record_dump.pop("paths_data")
            print(json_dump(urllib3_record_dump))

            assert json_loads(json_dump(urllib3_record_dump)) == {
                "build": "dev_0",
                "build_number": 0,
                "channel": "https://conda.anaconda.org/<develop>",
                "constrains": [
                    "cryptography >=1.3.4",
                    "idna >=2.0.0",
                    "pyopenssl >=0.14",
                    "pysocks !=1.5.7,<2.0,>=1.5.6"
                ],
                "depends": [
                    "python 2.7.*"
                ],
                "fn": "urllib3-1.19.1-dev_0",
                "name": "urllib3",
                "package_type": "virtual_python_egg_link",
                "subdir": "pypi",
                "version": "1.19.1"
            }

            # the unmanageable urllib3 should prevent a new requests from being installed
            stdout, stderr = run_command(Commands.INSTALL, prefix, "requests --dry-run --json",
                                         use_exception_handler=True)
            assert not stderr
            json_obj = json_loads(stdout)
            assert "UNLINK" not in json_obj["actions"]
            link_dists = json_obj["actions"]["LINK"]
            assert len(link_dists) == 1
            assert link_dists[0]["name"] == "requests"
            assert VersionOrder(link_dists[0]["version"]) < VersionOrder("2.16")

            # should already be satisfied
            stdout, stderr = run_command(Commands.INSTALL, prefix, "urllib3 -S")
            assert "All requested packages already installed." in stdout

            # should raise an error
            with pytest.raises(PackagesNotFoundError):
                # TODO: This raises PackagesNotFoundError, but the error should really explain
                #       that we can't install urllib3 because it's already installed and
                #       unmanageable. The error should suggest trying to use pip to uninstall it.
                stdout, stderr = run_command(Commands.INSTALL, prefix, "urllib3=1.20 --dry-run")

            # Now install a manageable urllib3.
            output = check_output(PYTHON_BINARY + " -m pip install -U urllib3==1.20",
                                  cwd=prefix, shell=True)
            print(output)
            PrefixData._cache_.clear()
            assert package_is_installed(prefix, "urllib3")
            urllib3_record = next(PrefixData(prefix).query("urllib3"))
            urllib3_record_dump = urllib3_record.dump()
            files = urllib3_record_dump.pop("files")
            paths_data = urllib3_record_dump.pop("paths_data")
            print(json_dump(urllib3_record_dump))

            assert json_loads(json_dump(urllib3_record_dump)) == {
                "build": "pypi_0",
                "build_number": 0,
                "channel": "https://conda.anaconda.org/pypi",
                "constrains": [
                    "pysocks >=1.5.6,<2.0,!=1.5.7"
                ],
                "depends": [
                    "python 2.7.*"
                ],
                "fn": "urllib3-1.20.dist-info",
                "name": "urllib3",
                "package_type": "virtual_python_wheel",
                "subdir": "pypi",
                "version": "1.20"
            }

            # we should be able to install an unbundled requests that upgrades urllib3 in the process
            stdout, stderr = run_command(Commands.INSTALL, prefix, "requests=2.18 --json")
            assert package_is_installed(prefix, "requests")
            assert package_is_installed(prefix, "urllib3>=1.21")
            assert not stderr
            json_obj = json_loads(stdout)
            unlink_dists = json_obj["actions"]["UNLINK"]
            assert len(unlink_dists) == 1
            assert unlink_dists[0]["name"] == "urllib3"
            assert unlink_dists[0]["channel"] == "pypi"

    @pytest.mark.skipif(on_win, reason="gawk is a windows only package")
    def test_search_gawk_not_win_filter(self):
        with make_temp_env() as prefix:
            stdout, stderr = run_command(
                Commands.SEARCH, prefix, "*gawk", "--platform", "win-64", "--json",
                "-c", "https://repo.anaconda.com/pkgs/msys2 --json",
                use_exception_handler=True,
            )
            json_obj = json_loads(stdout.replace("Fetching package metadata ...", "").strip())
            assert "gawk" in json_obj.keys()
            assert "m2-gawk" in json_obj.keys()
            assert len(json_obj.keys()) == 2

    @pytest.mark.skipif(not on_win, reason="gawk is a windows only package")
    def test_search_gawk_on_win(self):
        with make_temp_env() as prefix:
            stdout, stderr = run_command(Commands.SEARCH, prefix, "*gawk", "--json", use_exception_handler=True)
            json_obj = json_loads(stdout.replace("Fetching package metadata ...", "").strip())
            assert "gawk" in json_obj.keys()
            assert "m2-gawk" in json_obj.keys()
            assert len(json_obj.keys()) == 2

    @pytest.mark.skipif(not on_win, reason="gawk is a windows only package")
    def test_search_gawk_on_win_filter(self):
        with make_temp_env() as prefix:
            stdout, stderr = run_command(Commands.SEARCH, prefix, "gawk", "--platform",
                                         "linux-64", "--json", use_exception_handler=True)
            json_obj = json_loads(stdout.replace("Fetching package metadata ...", "").strip())
            assert not len(json_obj.keys()) == 0

    def test_bad_anaconda_token_infinite_loop(self):
        # This test is being changed around 2017-10-17, when the behavior of anaconda.org
        # was changed.  Previously, an expired token would return with a 401 response.
        # Now, a 200 response is always given, with any public packages available on the channel.
        response = requests.get("https://conda.anaconda.org/t/cqgccfm1mfma/data-portal/"
                                "%s/repodata.json" % context.subdir)
        assert response.status_code == 200

        try:
            prefix = make_temp_prefix(str(uuid4())[:7])
            channel_url = "https://conda.anaconda.org/t/cqgccfm1mfma/data-portal"
            run_command(Commands.CONFIG, prefix, "--add channels %s" % channel_url)
            stdout, stderr = run_command(Commands.CONFIG, prefix, "--show")
            yml_obj = yaml_load(stdout)
            assert yml_obj['channels'] == [channel_url, 'defaults']

            with pytest.raises(PackageNotFoundError):
                run_command(Commands.SEARCH, prefix, "boltons", "--json")

            stdout, stderr = run_command(Commands.SEARCH, prefix, "anaconda-mosaic", "--json")

            json_obj = json.loads(stdout)
            assert "anaconda-mosaic" in json_obj
            assert len(json_obj["anaconda-mosaic"]) > 0

        finally:
            rmtree(prefix, ignore_errors=True)
            reset_context()

    def test_anaconda_token_with_private_package(self):
        # TODO: should also write a test to use binstar_client to set the token,
        # then let conda load the token

        # Step 0. xfail if a token is set, for example when testing locally
        tokens = read_binstar_tokens()
        if tokens:
            pytest.xfail("binstar token found in global configuration")

        # Step 1. Make sure without the token we don't see the anyjson package
        try:
            prefix = make_temp_prefix(str(uuid4())[:7])
            channel_url = "https://conda.anaconda.org/kalefranz"
            run_command(Commands.CONFIG, prefix, "--add channels %s" % channel_url)
            run_command(Commands.CONFIG, prefix, "--remove channels defaults")
            stdout, stderr = run_command(Commands.CONFIG, prefix, "--show")
            yml_obj = yaml_load(stdout)
            assert yml_obj['channels'] == [channel_url]

            stdout, stderr = run_command(Commands.SEARCH, prefix, "anyjson", "--platform",
                                         "linux-64", "--json", use_exception_handler=True)
            json_obj = json_loads(stdout)
            assert json_obj['exception_name'] == 'PackagesNotFoundError'

        finally:
            rmtree(prefix, ignore_errors=True)
            reset_context()

        # Step 2. Now with the token make sure we can see the anyjson package
        try:
            prefix = make_temp_prefix(str(uuid4())[:7])
            channel_url = "https://conda.anaconda.org/t/zlZvSlMGN7CB/kalefranz"
            run_command(Commands.CONFIG, prefix, "--add channels %s" % channel_url)
            run_command(Commands.CONFIG, prefix, "--remove channels defaults")
            stdout, stderr = run_command(Commands.CONFIG, prefix, "--show")
            yml_obj = yaml_load(stdout)
            assert yml_obj['channels'] == [channel_url]

            stdout, stderr = run_command(Commands.SEARCH, prefix, "anyjson", "--platform",
                                         "linux-64", "--json")
            json_obj = json_loads(stdout)
            assert 'anyjson' in json_obj

        finally:
            rmtree(prefix, ignore_errors=True)

    def test_clean_index_cache(self):
        prefix = ''

        # make sure we have something in the index cache
        stdout, stderr = run_command(Commands.INFO, prefix, "flask --json")
        assert "flask" in json_loads(stdout)
        index_cache_dir = create_cache_dir()
        assert glob(join(index_cache_dir, "*.json"))

        # now clear it
        run_command(Commands.CLEAN, prefix, "--index-cache")
        assert not glob(join(index_cache_dir, "*.json"))

    def test_use_index_cache(self):
        from conda.gateways.connection.session import CondaSession
        from conda.core.subdir_data import SubdirData
        SubdirData._cache_.clear()

        prefix = make_temp_prefix("_" + str(uuid4())[:7])
        with make_temp_env(prefix=prefix):
            # First, clear the index cache to make sure we start with an empty cache.
            index_cache_dir = create_cache_dir()
            run_command(Commands.CLEAN, '', "--index-cache")
            assert not glob(join(index_cache_dir, "*.json"))

            # Then, populate the index cache.
            orig_get = CondaSession.get
            with patch.object(CondaSession, 'get', autospec=True) as mock_method:
                def side_effect(self, url, **kwargs):
                    # Make sure that we don't use the cache because of the
                    # corresponding HTTP header. This test is supposed to test
                    # whether the --use-index-cache causes the cache to be used.
                    result = orig_get(self, url, **kwargs)
                    for header in ['Etag', 'Last-Modified', 'Cache-Control']:
                        if header in result.headers:
                            del result.headers[header]
                    return result

                SubdirData._cache_.clear()
                mock_method.side_effect = side_effect
                stdout, stderr = run_command(Commands.INFO, prefix, "flask --json")
                assert mock_method.called

            # Next run with --use-index-cache and make sure it actually hits the cache
            # and does not go out fetching index data remotely.
            with patch.object(CondaSession, 'get', autospec=True) as mock_method:
                def side_effect(self, url, **kwargs):
                    if url.endswith('/repodata.json') or url.endswith('/repodata.json.bz2'):
                        raise AssertionError('Index cache was not hit')
                    else:
                        return orig_get(self, url, **kwargs)

                mock_method.side_effect = side_effect
                run_command(Commands.INSTALL, prefix, "flask", "--json", "--use-index-cache")

    def test_offline_with_empty_index_cache(self):
        from conda.core.subdir_data import SubdirData
        SubdirData._cache_.clear()

        try:
            with make_temp_env() as prefix:
                pkgs_dir = join(prefix, 'pkgs')
                with env_var('CONDA_PKGS_DIRS', pkgs_dir, reset_context):
                    with make_temp_channel(['flask-0.10.1']) as channel:
                        # Clear the index cache.
                        index_cache_dir = create_cache_dir()
                        run_command(Commands.CLEAN, '', "--index-cache")
                        assert not exists(index_cache_dir)

                        # Then attempt to install a package with --offline. The package (flask) is
                        # available in a local channel, however its dependencies are not. Make sure
                        # that a) it fails because the dependencies are not available and b)
                        # we don't try to download the repodata from non-local channels but we do
                        # download repodata from local channels.
                        from conda.gateways.connection.session import CondaSession

                        orig_get = CondaSession.get

                        result_dict = {}
                        def side_effect(self, url, **kwargs):
                            if not url.startswith('file://'):
                                raise AssertionError('Attempt to fetch repodata: {}'.format(url))
                            if url.startswith(channel):
                                result_dict['local_channel_seen'] = True
                            return orig_get(self, url, **kwargs)

                        with patch.object(CondaSession, 'get', autospec=True) as mock_method:
                            mock_method.side_effect = side_effect

                            SubdirData._cache_.clear()

                            # This first install passes because flask and its dependencies are in the
                            # package cache.
                            assert not package_is_installed(prefix, "flask")
                            run_command(Commands.INSTALL, prefix, "-c", channel, "flask", "--offline")
                            assert package_is_installed(prefix, "flask")

                            # The mock should have been called with our local channel URL though.
                            assert result_dict.get('local_channel_seen')

                            # Fails because pytz cannot be found in available channels.
                            with pytest.raises(PackagesNotFoundError):
                                run_command(Commands.INSTALL, prefix, "-c", channel, "pytz", "--offline")
                            assert not package_is_installed(prefix, "pytz")
        finally:
            SubdirData._cache_.clear()

    def test_create_from_extracted(self):
        with make_temp_package_cache() as pkgs_dir:
            assert context.pkgs_dirs == (pkgs_dir,)
            def pkgs_dir_has_tarball(tarball_prefix):
                return any(f.startswith(tarball_prefix) and f.endswith(CONDA_TARBALL_EXTENSION)
                           for f in os.listdir(pkgs_dir))

            with make_temp_env() as prefix:
                # First, make sure the openssl package is present in the cache,
                # downloading it if needed
                assert not pkgs_dir_has_tarball('openssl-')
                run_command(Commands.INSTALL, prefix, 'openssl')
                assert pkgs_dir_has_tarball('openssl-')

                # Then, remove the tarball but keep the extracted directory around
                run_command(Commands.CLEAN, prefix, '--tarballs --yes')
                assert not pkgs_dir_has_tarball('openssl-')

            with make_temp_env() as prefix:
                # Finally, install openssl, enforcing the use of the extracted package.
                # We expect that the tarball does not appear again because we simply
                # linked the package from the extracted directory. If the tarball
                # appeared again, we decided to re-download the package for some reason.
                run_command(Commands.INSTALL, prefix, 'openssl --offline')
                assert not pkgs_dir_has_tarball('openssl-')

    def test_clean_tarballs_and_packages(self):
        with make_temp_package_cache() as pkgs_dir:
            with make_temp_env("flask") as prefix:
                pkgs_dir_contents = [join(pkgs_dir, d) for d in os.listdir(pkgs_dir)]
                pkgs_dir_dirs = [d for d in pkgs_dir_contents if isdir(d)]
                pkgs_dir_tarballs = [f for f in pkgs_dir_contents if f.endswith('.tar.bz2')]
                assert any(basename(d).startswith('flask-') for d in pkgs_dir_dirs)
                assert any(basename(f).startswith('flask-') for f in pkgs_dir_tarballs)

                # --json flag is regression test for #5451
                run_command(Commands.CLEAN, prefix, "--packages --yes --json")

                # --json flag is regression test for #5451
                run_command(Commands.CLEAN, prefix, "--tarballs --yes --json")

                pkgs_dir_contents = [join(pkgs_dir, d) for d in os.listdir(pkgs_dir)]
                pkgs_dir_dirs = [d for d in pkgs_dir_contents if isdir(d)]
                pkgs_dir_tarballs = [f for f in pkgs_dir_contents if f.endswith('.tar.bz2')]

                assert any(basename(d).startswith('flask-') for d in pkgs_dir_dirs)
                assert not any(basename(f).startswith('flask-') for f in pkgs_dir_tarballs)

            run_command(Commands.CLEAN, prefix, "--packages --yes")

            pkgs_dir_contents = [join(pkgs_dir, d) for d in os.listdir(pkgs_dir)]
            pkgs_dir_dirs = [d for d in pkgs_dir_contents if isdir(d)]
            assert not any(basename(d).startswith('flask-') for d in pkgs_dir_dirs)

    def test_install_mkdir(self):
        try:
            prefix = make_temp_prefix()
            assert isdir(prefix)
            with pytest.raises(DirectoryNotACondaEnvironmentError):
                run_command(Commands.INSTALL, prefix, "python=3.5.2", "--mkdir")

            run_command(Commands.CREATE, prefix)
            run_command(Commands.INSTALL, prefix, "python=3.5.2", "--mkdir")
            assert package_is_installed(prefix, "python=3.5.2")

            rm_rf(prefix)
            assert not isdir(prefix)

            # this part also a regression test for #4849
            run_command(Commands.INSTALL, prefix, "python-dateutil=2.6.0", "python=3.5.2", "--mkdir")
            assert package_is_installed(prefix, "python=3.5.2")
            assert package_is_installed(prefix, "python-dateutil=2.6.0")

        finally:
            rmtree(prefix, ignore_errors=True)

    @pytest.mark.skipif(on_win, reason="python doesn't have dependencies on windows")
    def test_disallowed_packages(self):
        with env_var('CONDA_DISALLOWED_PACKAGES', 'sqlite&flask', reset_context):
            with make_temp_env() as prefix:
                with pytest.raises(CondaMultiError) as exc:
                    run_command(Commands.INSTALL, prefix, 'python')
            exc_val = exc.value.errors[0]
            assert isinstance(exc_val, DisallowedPackageError)
            assert exc_val.dump_map()['package_ref']['name'] == 'sqlite'

    def test_dont_remove_conda_1(self):
        pkgs_dirs = context.pkgs_dirs
        prefix = make_temp_prefix()
        with env_var('CONDA_ROOT_PREFIX', prefix, reset_context):
            with env_var('CONDA_PKGS_DIRS', ','.join(pkgs_dirs), reset_context):
                with make_temp_env(prefix=prefix):
                    stdout, stderr = run_command(Commands.INSTALL, prefix, "conda conda-build")
                    assert package_is_installed(prefix, "conda")
                    assert package_is_installed(prefix, "pycosat")
                    assert package_is_installed(prefix, "conda-build")

                    with pytest.raises(CondaMultiError) as exc:
                        run_command(Commands.REMOVE, prefix, 'conda')

                    assert any(isinstance(e, RemoveError) for e in exc.value.errors)
                    assert package_is_installed(prefix, "conda")
                    assert package_is_installed(prefix, "pycosat")

                    with pytest.raises(CondaMultiError) as exc:
                        run_command(Commands.REMOVE, prefix, 'pycosat')

                    assert any(isinstance(e, RemoveError) for e in exc.value.errors)
                    assert package_is_installed(prefix, "conda")
                    assert package_is_installed(prefix, "pycosat")
                    assert package_is_installed(prefix, "conda-build")

    def test_dont_remove_conda_2(self):
        # regression test for #6904
        pkgs_dirs = context.pkgs_dirs
        prefix = make_temp_prefix()
        with env_var('CONDA_ROOT_PREFIX', prefix, reset_context):
            with env_var('CONDA_PKGS_DIRS', ','.join(pkgs_dirs), reset_context):
                with make_temp_env(prefix=prefix):
                    stdout, stderr = run_command(Commands.INSTALL, prefix, "conda")
                    assert package_is_installed(prefix, "conda")
                    assert package_is_installed(prefix, "pycosat")

                    with pytest.raises(CondaMultiError) as exc:
                        run_command(Commands.REMOVE, prefix, 'pycosat')

                    assert any(isinstance(e, RemoveError) for e in exc.value.errors)
                    assert package_is_installed(prefix, "conda")
                    assert package_is_installed(prefix, "pycosat")

                    with pytest.raises(CondaMultiError) as exc:
                        run_command(Commands.REMOVE, prefix, 'conda')

                    assert any(isinstance(e, RemoveError) for e in exc.value.errors)
                    assert package_is_installed(prefix, "conda")
                    assert package_is_installed(prefix, "pycosat")

    def test_force_remove(self):
        with make_temp_env() as prefix:
            stdout, stderr = run_command(Commands.INSTALL, prefix, "flask")
            assert package_is_installed(prefix, "flask")
            assert package_is_installed(prefix, "jinja2")

            stdout, stderr = run_command(Commands.REMOVE, prefix, "jinja2", "--force")
            assert not package_is_installed(prefix, "jinja2")
            assert package_is_installed(prefix, "flask")

            stdout, stderr = run_command(Commands.REMOVE, prefix, "flask")
            assert not package_is_installed(prefix, "flask")

        # regression test for #3489
        # don't raise for remove --all if environment doesn't exist
        rm_rf(prefix)
        run_command(Commands.REMOVE, prefix, "--all")

    def test_download_only_flag(self):
        from conda.core.link import UnlinkLinkTransaction
        with patch.object(UnlinkLinkTransaction, 'execute') as mock_method:
            with make_temp_env('openssl --download-only', use_exception_handler=True) as prefix:
                assert mock_method.call_count == 0
            with make_temp_env('openssl', use_exception_handler=True) as prefix:
                assert mock_method.call_count == 1

    def test_transactional_rollback_simple(self):
        from conda.core.path_actions import CreatePrefixRecordAction
        with patch.object(CreatePrefixRecordAction, 'execute') as mock_method:
            with make_temp_env() as prefix:
                mock_method.side_effect = KeyError('Bang bang!!')
                with pytest.raises(CondaMultiError):
                    run_command(Commands.INSTALL, prefix, 'openssl')
                assert not package_is_installed(prefix, 'openssl')

    def test_transactional_rollback_upgrade_downgrade(self):
        with make_temp_env("python=3.5") as prefix:
            assert exists(join(prefix, PYTHON_BINARY))
            assert package_is_installed(prefix, 'python=3')

            run_command(Commands.INSTALL, prefix, 'flask=0.10.1')
            assert package_is_installed(prefix, 'flask=0.10.1')

            from conda.core.path_actions import CreatePrefixRecordAction
            with patch.object(CreatePrefixRecordAction, 'execute') as mock_method:
                mock_method.side_effect = KeyError('Bang bang!!')
                with pytest.raises(CondaMultiError):
                    run_command(Commands.INSTALL, prefix, 'flask=0.11.1')
                assert package_is_installed(prefix, 'flask=0.10.1')

    def test_directory_not_a_conda_environment(self):
        prefix = make_temp_prefix(str(uuid4())[:7])
        try:
            with pytest.raises(DirectoryNotACondaEnvironmentError):
                run_command(Commands.INSTALL, prefix, "sqlite")
        finally:
            rm_rf(prefix)

    def test_init_dev_and_NoBaseEnvironmentError(self):
        conda_exe = join('Scripts', 'conda.exe') if on_win else join('bin', 'conda')
        python_exe = 'python.exe' if on_win else join('bin', 'python')
        with make_temp_env("conda=4.5.0", name='_' + str(uuid4())[:8]) as prefix:
            result = subprocess_call("%s --version" % join(prefix, conda_exe))
            assert result.rc == 0
            assert not result.stderr
            assert result.stdout.startswith("conda ")
            conda_version = result.stdout.strip()[6:]
            assert conda_version == "4.5.0"

            result = subprocess_call("%s -m conda init --dev" % join(prefix, python_exe),
                                     path=dirname(CONDA_PACKAGE_ROOT))

            result = subprocess_call("%s --version" % join(prefix, conda_exe))
            assert result.rc == 0
            assert not result.stderr
            assert result.stdout.startswith("conda ")
            conda_version = result.stdout.strip()[6:]
            assert conda_version == CONDA_VERSION

            rm_rf(join(prefix, 'conda-meta', 'history'))

            result = subprocess_call("%s info -a" % join(prefix, conda_exe))
            print(result.stdout)

            if not on_win:
                # Windows has: Fatal Python error: failed to get random numbers to initialize Python
                result = subprocess_call("%s install python" % join(prefix, conda_exe), env={"SHLVL": "1"},
                                         raise_on_error=False)
                assert result.rc == 1
                assert "NoBaseEnvironmentError: This conda installation has no default base environment." in result.stderr

    def test_conda_downgrade(self):
        # Create an environment with the current conda under test, but include an earlier
        # version of conda and other packages in that environment.
        # Make sure we can flip back and forth.
        conda_exe = join('Scripts', 'conda.exe') if on_win else join('bin', 'conda')
        with env_var("CONDA_AUTO_UPDATE_CONDA", "false", reset_context):
            with make_temp_env("conda=4.3.27 python=%s" % sys.version_info[0],
                               name='_' + str(uuid4())[:8]) as prefix:  # rev 0
                assert package_is_installed(prefix, "conda")

                run_command(Commands.INSTALL, prefix, "mccabe")  # rev 1
                assert package_is_installed(prefix, "mccabe")

                subprocess_call("%s install -p %s -y itsdangerous" % (join(prefix, conda_exe), prefix))  # rev 2
                linked_data_.clear()
                assert package_is_installed(prefix, "itsdangerous")

                run_command(Commands.INSTALL, prefix, "lockfile")  # rev 3
                assert package_is_installed(prefix, "lockfile")

                subprocess_call("%s install -p %s -y conda=4.3" % (join(prefix, conda_exe), prefix))  # rev 4
                PrefixData._cache_.clear()
                assert not package_is_installed(prefix, "conda=4.3.27")

                subprocess_call("%s install -p %s -y colorama" % (join(prefix, conda_exe), prefix))  # rev 5
                linked_data_.clear()
                assert package_is_installed(prefix, "colorama")

                stdout, stderr = run_command(Commands.LIST, prefix, "--revisions")
                print(stdout)

                PrefixData._cache_.clear()
                run_command(Commands.INSTALL, prefix, "--rev 3")
                PrefixData._cache_.clear()
                assert package_is_installed(prefix, "conda=4.3.27")
                assert not package_is_installed(prefix, "colorama")

                subprocess_call("%s install -y -p %s --rev 1" % (join(prefix, conda_exe), prefix))
                linked_data_.clear()
                assert not package_is_installed(prefix, "itsdangerous")
                PrefixData._cache_.clear()
                assert package_is_installed(prefix, "conda=4.3.27")
                assert package_is_installed(prefix, "python=%s" % sys.version_info[0])

                result = subprocess_call("%s info --json" % join(prefix, conda_exe))
                conda_info = json.loads(result.stdout)
                assert conda_info["conda_version"] == "4.3.27"

    @pytest.mark.skipif(on_win, reason="openssl only has a postlink script on unix")
    def test_run_script_called(self):
        import conda.core.link
        with patch.object(conda.core.link, 'subprocess_call') as rs:
            with make_temp_env("openssl=1.0.2j --no-deps") as prefix:
                assert package_is_installed(prefix, 'openssl')
                assert rs.call_count == 1

    def test_conda_info_python(self):
        stdout, stderr = run_command(Commands.INFO, None, "python=3.5")
        assert "python 3.5.1 0" in stdout

    def test_toolz_cytoolz_package_cache_regression(self):
        with make_temp_env("python=3.5") as prefix:
            pkgs_dir = join(prefix, 'pkgs')
            with env_var('CONDA_PKGS_DIRS', pkgs_dir, reset_context):
                assert context.pkgs_dirs == (pkgs_dir,)
                run_command(Commands.INSTALL, prefix, "-c conda-forge toolz cytoolz")
                assert package_is_installed(prefix, 'toolz')

    def test_remove_spellcheck(self):
        with make_temp_env("numpy=1.12") as prefix:
            assert exists(join(prefix, PYTHON_BINARY))
            assert package_is_installed(prefix, 'numpy')

            with pytest.raises(PackagesNotFoundError) as exc:
                run_command(Commands.REMOVE, prefix, 'numpi')

            exc_string = '%r' % exc.value
            assert exc_string.strip() == dals("""
            PackagesNotFoundError: The following packages are missing from the target environment:
              - numpi
            """).strip()
            assert package_is_installed(prefix, 'numpy')


    def test_conda_list_json(self):
        def pkg_info(s):
            # function from nb_conda/envmanager.py
            if hasattr(s, 'rsplit'):  # proxy for isinstance(s, six.string_types)
                name, version, build = s.rsplit('-', 2)
                return {
                    'name': name,
                    'version': version,
                    'build': build
                }
            else:
                return {
                    'name': s['name'],
                    'version': s['version'],
                    'build': s.get('build_string') or s['build']
                }

        with make_temp_env("python=3") as prefix:
            stdout, stderr = run_command(Commands.LIST, prefix, '--json')
            stdout_json = json.loads(stdout)
            packages = [pkg_info(package) for package in stdout_json]
            python_package = next(p for p in packages if p['name'] == 'python')
            assert python_package['version'].startswith('3')


@pytest.mark.skipif(True, reason="get the rest of Solve API worked out first")
@pytest.mark.integration
class PrivateEnvIntegrationTests(TestCase):

    def setUp(self):
        PackageCacheData.clear()

        self.pkgs_dirs = ','.join(context.pkgs_dirs)
        self.prefix = create_temp_location()
        run_command(Commands.CREATE, self.prefix)

        self.preferred_env = "_spiffy-test-app_"
        self.preferred_env_prefix = join(self.prefix, 'envs', self.preferred_env)

        # self.save_path_conflict = os.environ.get('CONDA_PATH_CONFLICT')
        self.saved_values = {}
        self.saved_values['CONDA_ROOT_PREFIX'] = os.environ.get('CONDA_ROOT_PREFIX')
        self.saved_values['CONDA_PKGS_DIRS'] = os.environ.get('CONDA_PKGS_DIRS')
        self.saved_values['CONDA_ENABLE_PRIVATE_ENVS'] = os.environ.get('CONDA_ENABLE_PRIVATE_ENVS')

        # os.environ['CONDA_PATH_CONFLICT'] = 'prevent'
        os.environ['CONDA_ROOT_PREFIX'] = self.prefix
        os.environ['CONDA_PKGS_DIRS'] = self.pkgs_dirs
        os.environ['CONDA_ENABLE_PRIVATE_ENVS'] = 'true'

        reset_context()

    def tearDown(self):
        rm_rf(self.prefix)

        for key, value in iteritems(self.saved_values):
            if value is not None:
                os.environ[key] = value
            else:
                del os.environ[key]

        reset_context()

    def exe_file(self, prefix, exe_name):
        if on_win:
            exe_name = exe_name + '.exe'
        return join(prefix, get_bin_directory_short_path(), exe_name)

    @patch.object(Context, 'prefix_specified')
    def test_simple_install_uninstall(self, prefix_specified):
        prefix_specified.__get__ = Mock(return_value=False)

        # >> simple progression install then uninstall <<
        run_command(Commands.INSTALL, self.prefix, "-c conda-test spiffy-test-app")
        assert not package_is_installed(self.prefix, "spiffy-test-app")
        assert isfile(self.exe_file(self.prefix, 'spiffy-test-app'))
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app")
        with env_var('YABBA-DABBA', 'doo'):
            stdout, stderr, rc = subprocess_call(self.exe_file(self.prefix, 'spiffy-test-app'))
        assert not stderr
        assert rc == 0
        json_d = json.loads(stdout)
        assert json_d['YABBA-DABBA'] == 'doo'

        run_command(Commands.INSTALL, self.prefix, "-c conda-test uses-spiffy-test-app")
        assert not package_is_installed(self.prefix, "uses-spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app")

        run_command(Commands.REMOVE, self.prefix, "uses-spiffy-test-app")
        assert not package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app")

        run_command(Commands.REMOVE, self.prefix, "spiffy-test-app")
        assert not package_is_installed(self.prefix, "spiffy-test-app")
        assert not isfile(self.exe_file(self.prefix, 'spiffy-test-app'))
        assert not package_is_installed(self.preferred_env_prefix, "spiffy-test-app")
        assert not isfile(self.exe_file(self.preferred_env_prefix, 'spiffy-test-app'))

    @patch.object(Context, 'prefix_specified')
    def test_install_dep_uninstall_base(self, prefix_specified):
        prefix_specified.__get__ = Mock(return_value=False)

        # >> install uses-spiffy-test-app, uninstall spiffy-test-app <<
        run_command(Commands.INSTALL, self.prefix, "-c conda-test uses-spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app")
        assert not package_is_installed(self.prefix, "spiffy-test-app")
        assert not package_is_installed(self.prefix, "uses-spiffy-test-app")

        with pytest.raises(PackagesNotFoundError):
            run_command(Commands.REMOVE, self.prefix, "spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app")
        assert isfile(self.exe_file(self.preferred_env_prefix, 'spiffy-test-app'))
        assert package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app")
        assert not package_is_installed(self.prefix, "spiffy-test-app")
        assert not isfile(self.exe_file(self.prefix, 'spiffy-test-app'))

        run_command(Commands.REMOVE, self.prefix, "uses-spiffy-test-app")
        assert not package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app")

        # this part tests that the private environment was fully pruned
        assert not package_is_installed(self.preferred_env_prefix, "spiffy-test-app")
        assert not isfile(self.exe_file(self.preferred_env_prefix, 'spiffy-test-app'))

    @patch.object(Context, 'prefix_specified')
    def test_install_base_1_then_update(self, prefix_specified):
        prefix_specified.__get__ = Mock(return_value=False)

        # >> install spiffy-test-app 1.0, then update <<
        run_command(Commands.INSTALL, self.prefix, "-c conda-test spiffy-test-app=1")
        assert package_is_installed(self.prefix, "spiffy-test-app")

        run_command(Commands.UPDATE, self.prefix, "-c conda-test spiffy-test-app")
        assert not package_is_installed(self.prefix, "spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app")

        run_command(Commands.REMOVE, self.prefix, "spiffy-test-app")
        assert not package_is_installed(self.preferred_env_prefix, "spiffy-test-app")

    @patch.object(Context, 'prefix_specified')
    def test_install_base_then_remove_from_private_env(self, prefix_specified):
        prefix_specified.__get__ = Mock(return_value=False)

        # >> install spiffy-test-app, then remove from preferred env <<
        run_command(Commands.INSTALL, self.prefix, "-c conda-test spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app")

        run_command(Commands.REMOVE, self.preferred_env_prefix, "spiffy-test-app")
        assert not package_is_installed(self.prefix, "spiffy-test-app")
        assert not isfile(self.exe_file(self.prefix, 'spiffy-test-app'))
        assert not package_is_installed(self.preferred_env_prefix, "spiffy-test-app")
        assert not isfile(self.exe_file(self.preferred_env_prefix, 'spiffy-test-app'))

    @patch.object(Context, 'prefix_specified')
    def test_install_base_1_then_install_base_2(self, prefix_specified):
        prefix_specified.__get__ = Mock(return_value=False)

        # >> install spiffy-test-app 1.0, then install spiffy-test-app 2.0 <<
        run_command(Commands.INSTALL, self.prefix, "-c conda-test spiffy-test-app=1")
        assert package_is_installed(self.prefix, "spiffy-test-app")

        run_command(Commands.INSTALL, self.prefix, "-c conda-test spiffy-test-app=2")
        assert not package_is_installed(self.prefix, "spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app")

        run_command(Commands.REMOVE, self.prefix, "spiffy-test-app")
        assert not package_is_installed(self.preferred_env_prefix, "spiffy-test-app")

    @patch.object(Context, 'prefix_specified')
    def test_install_base_2_then_install_base_1(self, prefix_specified):
        prefix_specified.__get__ = Mock(return_value=False)

        # >> install spiffy-test-app 2.0, then spiffy-test-app 1.0 <<
        run_command(Commands.INSTALL, self.prefix, "-c conda-test spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app")

        run_command(Commands.INSTALL, self.prefix, "-c conda-test spiffy-test-app=1")
        assert not package_is_installed(self.preferred_env_prefix, "spiffy-test-app")
        assert package_is_installed(self.prefix, "spiffy-test-app")

    @patch.object(Context, 'prefix_specified')
    def test_install_base_2_then_install_dep_1(self, prefix_specified):
        prefix_specified.__get__ = Mock(return_value=False)

        # install spiffy-test-app 2.0, then uses-spiffy-test-app 1.0,
        #   which should suck spiffy-test-app back to the root prefix
        run_command(Commands.INSTALL, self.prefix, "-c conda-test spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app")
        assert not package_is_installed(self.prefix, "spiffy-test-app")
        assert not package_is_installed(self.prefix, "uses-spiffy-test-app")
        assert not package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app")

        run_command(Commands.INSTALL, self.prefix, "-c conda-test uses-spiffy-test-app=1")
        assert package_is_installed(self.prefix, "spiffy-test-app-2")
        assert package_is_installed(self.prefix, "uses-spiffy-test-app")
        assert not package_is_installed(self.preferred_env_prefix, "spiffy-test-app")
        assert not package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app")

    @patch.object(Context, 'prefix_specified')
    def test_install_dep_2_then_install_base_1(self, prefix_specified):
        prefix_specified.__get__ = Mock(return_value=False)

        # install uses-spiffy-test-app 2.0, then spiffy-test-app 1.0,
        run_command(Commands.INSTALL, self.prefix, "-c conda-test uses-spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app")
        assert not isfile(self.exe_file(self.prefix, 'spiffy-test-app'))

        run_command(Commands.INSTALL, self.prefix, "-c conda-test spiffy-test-app=1")
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app=2")
        assert package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app=2")
        assert package_is_installed(self.prefix, "spiffy-test-app=1")
        assert isfile(self.exe_file(self.prefix, 'spiffy-test-app'))

    @patch.object(Context, 'prefix_specified')
    def test_install_base_1_dep_2_together(self, prefix_specified):
        prefix_specified.__get__ = Mock(return_value=False)

        run_command(Commands.INSTALL, self.prefix, "-c conda-test spiffy-test-app=1 uses-spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app=2")
        assert package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app=2")
        assert package_is_installed(self.prefix, "spiffy-test-app-1")

    @patch.object(Context, 'prefix_specified')
    def test_a2(self, prefix_specified):
        prefix_specified.__get__ = Mock(return_value=False)

        run_command(Commands.INSTALL, self.prefix, "-c conda-test uses-spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app=2")
        assert package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app=2")
        assert not isfile(self.exe_file(self.prefix, 'spiffy-test-app'))
        assert isfile(self.exe_file(self.preferred_env_prefix, 'spiffy-test-app'))

        run_command(Commands.INSTALL, self.prefix, "-c conda-test needs-spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app=2")
        assert package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app=2")
        assert package_is_installed(self.prefix, "needs-spiffy-test-app")
        assert not package_is_installed(self.prefix, "uses-spiffy-test-app=2")
        assert isfile(self.exe_file(self.prefix, 'spiffy-test-app'))
        assert isfile(self.exe_file(self.preferred_env_prefix, 'spiffy-test-app'))

        run_command(Commands.REMOVE, self.prefix, "uses-spiffy-test-app")
        assert not package_is_installed(self.preferred_env_prefix, "spiffy-test-app=2")
        assert not package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app=2")
        assert package_is_installed(self.prefix, "needs-spiffy-test-app")
        assert not package_is_installed(self.prefix, "uses-spiffy-test-app=2")
        assert isfile(self.exe_file(self.prefix, 'spiffy-test-app'))
        assert not isfile(self.exe_file(self.preferred_env_prefix, 'spiffy-test-app'))

        run_command(Commands.REMOVE, self.prefix, "needs-spiffy-test-app")
        assert not package_is_installed(self.prefix, "needs-spiffy-test-app")
        assert package_is_installed(self.prefix, "spiffy-test-app-2")
        assert isfile(self.exe_file(self.prefix, 'spiffy-test-app'))

    @patch.object(Context, 'prefix_specified')
    def test_b2(self, prefix_specified):
        prefix_specified.__get__ = Mock(return_value=False)

        run_command(Commands.INSTALL, self.prefix, "-c conda-test spiffy-test-app uses-spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app=2")
        assert package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app")
        assert isfile(self.exe_file(self.prefix, 'spiffy-test-app'))

        run_command(Commands.INSTALL, self.prefix, "-c conda-test needs-spiffy-test-app")
        assert not package_is_installed(self.preferred_env_prefix, "spiffy-test-app=2")
        assert not package_is_installed(self.preferred_env_prefix, "uses-spiffy-test-app=2")
        assert package_is_installed(self.prefix, "needs-spiffy-test-app")
        assert package_is_installed(self.prefix, "spiffy-test-app=2")
        assert package_is_installed(self.prefix, "uses-spiffy-test-app")

    @patch.object(Context, 'prefix_specified')
    def test_c2(self, prefix_specified):
        prefix_specified.__get__ = Mock(return_value=False)

        run_command(Commands.INSTALL, self.prefix, "-c conda-test needs-spiffy-test-app")
        assert package_is_installed(self.prefix, "spiffy-test-app=2")
        assert package_is_installed(self.prefix, "needs-spiffy-test-app")
        assert not package_is_installed(self.preferred_env_prefix, "spiffy-test-app=2")

        run_command(Commands.INSTALL, self.prefix, "-c conda-test spiffy-test-app=2")  # nothing to do
        assert package_is_installed(self.prefix, "spiffy-test-app=2")
        assert package_is_installed(self.prefix, "needs-spiffy-test-app")
        assert not package_is_installed(self.preferred_env_prefix, "spiffy-test-app=2")

    @patch.object(Context, 'prefix_specified')
    def test_d2(self, prefix_specified):
        prefix_specified.__get__ = Mock(return_value=False)

        run_command(Commands.INSTALL, self.prefix, "-c conda-test spiffy-test-app")
        assert package_is_installed(self.preferred_env_prefix, "spiffy-test-app=2")
        assert isfile(self.exe_file(self.prefix, 'spiffy-test-app'))
        assert isfile(self.exe_file(self.preferred_env_prefix, 'spiffy-test-app'))

        run_command(Commands.INSTALL, self.prefix, "-c conda-test needs-spiffy-test-app")
        assert not package_is_installed(self.preferred_env_prefix, "spiffy-test-app=2")
        assert package_is_installed(self.prefix, "spiffy-test-app=2")
        assert package_is_installed(self.prefix, "needs-spiffy-test-app")
        assert not isfile(self.exe_file(self.preferred_env_prefix, 'spiffy-test-app'))
