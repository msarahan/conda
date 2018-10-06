"""
Helpers for the tests
"""
from __future__ import absolute_import, division, print_function

from copy import copy
import json
import os
from os.path import dirname, join
import re
from shlex import split
import sys
from tempfile import gettempdir
from uuid import uuid4

from conda._vendor.auxlib.collection import frozendict

from conda import cli
from conda._vendor.toolz.functoolz import memoize
from conda.base.context import context, reset_context
from conda.common.io import argv, captured, replace_log_streams
from conda.core.index import _supplement_index_with_features
from conda.core.repodata import make_feature_record
from conda.gateways.disk.delete import rm_rf
from conda.gateways.disk.read import lexists
from conda.gateways.logging import initialize_logging
from conda.models.channel import Channel
from conda.models.dist import Dist
from conda.models.index_record import IndexRecord
from conda.resolve import Resolve

try:
    from unittest import mock
    from unittest.mock import patch
except ImportError:
    import mock
    from mock import patch


from contextlib import contextmanager

from conda.common.compat import StringIO, iteritems, itervalues

expected_error_prefix = 'Using Anaconda Cloud api site https://api.anaconda.org'
def strip_expected(stderr):
    if expected_error_prefix and stderr.startswith(expected_error_prefix):
        stderr = stderr[len(expected_error_prefix):].lstrip()
    return stderr

def raises(exception, func, string=None):
    try:
        a = func()
    except exception as e:
        if string:
            assert string in e.args[0]
        print(e)
        return True
    raise Exception("did not raise, gave %s" % a)


@contextmanager
def captured(disallow_stderr=True):
    # same as common.io.captured but raises Exception if unexpected output was written to stderr
    try:
        with common_io_captured() as c:
            yield c
    finally:
        c.stderr = strip_expected(c.stderr)
        if disallow_stderr and c.stderr:
            raise Exception("Got stderr output: %s" % c.stderr)

    stdout.seek(0)
    stderr.seek(0)
    return stdout.read(), stderr.read().replace(
        "Using Anaconda API: https://api.anaconda.org\n", "")


def capture_json_with_argv(command, **kwargs):
    stdout, stderr, exit_code = run_inprocess_conda_command(command)
    if kwargs.get('relaxed'):
        match = re.match('\A.*?({.*})', stdout, re.DOTALL)
        if match:
            stdout = match.groups()[0]
    elif stderr:
        # TODO should be exception
        return stderr
    try:
        return json.loads(stdout.strip())
    except ValueError:
        raise


def assert_equals(a, b, output=""):
    output = "%r != %r" % (a.lower(), b.lower()) + "\n\n" + output
    assert a.lower() == b.lower(), output


def assert_not_in(a, b, output=""):
    assert a.lower() not in b.lower(), "%s %r should not be found in %r" % (output, a.lower(), b.lower())


def assert_in(a, b, output=""):
    assert a.lower() in b.lower(), "%s %r cannot be found in %r" % (output, a.lower(), b.lower())


def run_inprocess_conda_command(command):
    # anything that uses this function is an integration test
    reset_context(())
    with argv(split(command)), captured() as c:
        initialize_logging()
        try:
            exit_code = cli.main()
        except SystemExit:
            pass
    print(c.stderr, file=sys.stderr)
    print(c.stdout)
    return c.stdout, c.stderr, exit_code


@contextmanager
def tempdir():
    tempdirdir = gettempdir()
    dirname = str(uuid4())[:8]
    prefix = join(tempdirdir, dirname)
    try:
        os.makedirs(prefix)
        yield prefix
    finally:
        if lexists(prefix):
            rm_rf(prefix)


def supplement_index_with_repodata(index, repodata, channel, priority):
    repodata_info = repodata['info']
    arch = repodata_info.get('arch')
    platform = repodata_info.get('platform')
    subdir = repodata_info.get('subdir')
    if not subdir:
        subdir = "%s-%s" % (repodata_info['platform'], repodata_info['arch'])
    auth = channel.auth
    for fn, info in iteritems(repodata['packages']):
        rec = IndexRecord.from_objects(info,
                                       fn=fn,
                                       arch=arch,
                                       platform=platform,
                                       channel=channel,
                                       subdir=subdir,
                                       # schannel=schannel,
                                       priority=priority,
                                       # url=join_url(channel_url, fn),
                                       auth=auth)
        dist = Dist(rec)
        index[dist] = rec


def add_feature_records(index):
    feature_names = set()
    for record in itervalues(index):
        if record.features:
            feature_names.update(record.features)
        if record.track_features:
            feature_names.update(record.track_features)

    for feature_name in feature_names:
        rec = make_feature_record(feature_name)
        index[Dist(rec)] = rec


@memoize
def get_index_r_1():
    with open(join(dirname(__file__), 'index.json')) as fi:
        packages = json.load(fi)
        repodata = {
            "info": {
                "subdir": context.subdir,
                "arch": context.arch_name,
                "platform": context.platform,
            },
            "packages": packages,
        }

    index = {}
    channel = Channel('defaults')
    supplement_index_with_repodata(index, repodata, channel, 1)
    add_feature_records(index)
    index = frozendict(index)
    r = Resolve(index)
    index = r.index
    return index, r


@memoize
def get_index_r_2():
    with open(join(dirname(__file__), 'index2.json')) as fi:
        packages = json.load(fi)
        repodata = {
            "info": {
                "subdir": context.subdir,
                "arch": context.arch_name,
                "platform": context.platform,
            },
            "packages": packages,
        }

    index = {}
    channel = Channel('defaults')
    supplement_index_with_repodata(index, repodata, channel, 1)
    add_feature_records(index)
    index = frozendict(index)
    r = Resolve(index)
    index = r.index
    return index, r


@memoize
def get_index_r_3():
    with open(join(dirname(__file__), 'index3.json')) as fi:
        packages = json.load(fi)
        repodata = {
            "info": {
                "subdir": context.subdir,
                "arch": context.arch_name,
                "platform": context.platform,
            },
            "packages": packages,
        }

    index = {}
    channel = Channel('defaults')
    supplement_index_with_repodata(index, repodata, channel, 1)
    add_feature_records(index)
    index = frozendict(index)
    r = Resolve(index)
    index = r.index
    return index, r


