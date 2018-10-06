import os

from conda.gateways.disk.create import mkdir_p

from conda.common.io import env_var

from conda._vendor.boltons.setutils import IndexedSet

from conda.cli import common
from conda.cli.python_api import run_command, Commands
from conda.core import linked_data
from conda.core.package_cache import ProgressiveFetchExtract
from conda.exceptions import NoPackagesFoundError, InstallError
from conda.models.channel import prioritize_channels
from conda.models.dist import Dist
from conda.models.index_record import IndexRecord
from conda.common.io import env_var

from contextlib import contextmanager
import json
import os
from os.path import dirname, join
import random
import sys
import unittest

import pytest

from conda import CondaError
from conda._vendor.boltons.setutils import IndexedSet
from conda.base.context import context, reset_context
from conda.cli.python_api import Commands, run_command
from conda.common.compat import iteritems
from conda.common.io import env_var
from conda.core.index import supplement_index_with_repodata, supplement_index_with_features
from conda.core.package_cache import ProgressiveFetchExtract
import conda.core.solve
from conda.exceptions import NoPackagesFoundError
from conda.gateways.disk.create import mkdir_p
from conda.gateways.disk.delete import rm_rf
from conda.gateways.disk.update import touch
import conda.instructions as inst
from conda.models.channel import Channel
from conda.models.dist import Dist
from conda.models.index_record import IndexRecord
from conda.plan import display_actions
import conda.plan as plan
from conda.resolve import MatchSpec, Resolve
from conda.utils import on_win
from .decorators import skip_if_no_mock
from .gateways.disk.test_permissions import create_temp_location, tempdir
from .helpers import captured, mock, tempdir

try:
    from unittest.mock import patch
except ImportError:
    from mock import patch

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
supplement_index_with_features(index, ('mkl',))
r = Resolve(index)
index = r.index


def DPkg(s, **kwargs):
    d = Dist(s)
    _kwargs = dict(
        fn=d.to_filename(),
        name=d.name,
        version=d.version,
        build=d.build_string,
        build_number=int(d.build_string.rsplit('_', 1)[-1]),
        channel=d.channel,
        subdir=context.subdir,
        md5="012345789",
    )
    _kwargs.update(kwargs)
    return IndexRecord(**_kwargs)

def solve(specs):
    return [Dist.from_string(fn) for fn in r.solve(specs)]


class add_unlink_TestCase(unittest.TestCase):
    def generate_random_dist(self):
        return "foobar-%s-0" % random.randint(100, 200)

    @contextmanager
    def mock_platform(self, windows=False):
        with mock.patch.object(plan, "sys") as sys:
            sys.platform = "win32" if windows else "not win32"
            yield sys

    @skip_if_no_mock
    def test_simply_adds_unlink_on_non_windows(self):
        actions = {}
        dist = Dist.from_string(self.generate_random_dist())
        with self.mock_platform(windows=False):
            plan.add_unlink(actions, dist)
        self.assertIn(inst.UNLINK, actions)
        self.assertEqual(actions[inst.UNLINK], [dist, ])

    @skip_if_no_mock
    def test_adds_to_existing_actions(self):
        actions = {inst.UNLINK: [{"foo": "bar"}]}
        dist = Dist.from_string(self.generate_random_dist())
        with self.mock_platform(windows=False):
            plan.add_unlink(actions, dist)
        self.assertEqual(2, len(actions[inst.UNLINK]))


class TestAddDeaultsToSpec(unittest.TestCase):
    # tests for plan.add_defaults_to_specs(r, linked, specs)

    def check(self, specs, added):
        new_specs = list(specs + added)
        plan.add_defaults_to_specs(r, self.linked, specs)
        specs = [s.split(' (')[0] for s in specs]
        self.assertEqual(specs, new_specs)

    # def test_1(self):
    #     self.linked = solve(['anaconda 1.5.0', 'python 2.7*', 'numpy 1.7*'])
    #     for specs, added in [
    #         (['python 3*'], []),
    #         (['python'], ['python 2.7*']),
    #         (['scipy'], ['python 2.7*']),
    #         ]:
    #         self.check(specs, added)
    #
    # def test_2(self):
    #     self.linked = solve(['anaconda 1.5.0', 'python 2.6*', 'numpy 1.6*'])
    #     for specs, added in [
    #         (['python'], ['python 2.6*']),
    #         (['numpy'], ['python 2.6*']),
    #         (['pandas'], ['python 2.6*']),
    #         # however, this would then be unsatisfiable
    #         (['python 3*', 'numpy'], []),
    #         ]:
    #         self.check(specs, added)
    #
    # def test_3(self):
    #     self.linked = solve(['anaconda 1.5.0', 'python 3.3*'])
    #     for specs, added in [
    #         (['python'], ['python 3.3*']),
    #         (['numpy'], ['python 3.3*']),
    #         (['scipy'], ['python 3.3*']),
    #         ]:
    #         self.check(specs, added)
    #
    # def test_4(self):
    #     self.linked = []
    #     for dp in ('2.7', '3.5'):
    #         with env_var('CONDA_DEFAULT_PYTHON', dp, reset_context):
    #             ps = ['python 2.7*'] if context.default_python == '2.7' else []
    #             for specs, added in [
    #                 (['python'], ps),
    #                 (['numpy'], ps),
    #                 (['scipy'], ps),
    #                 (['anaconda'], ps),
    #                 (['anaconda 1.5.0 np17py27_0'], []),
    #                 (['sympy 0.7.2 py27_0'], []),
    #                 (['scipy 0.12.0 np16py27_0'], []),
    #                 (['anaconda', 'python 3*'], []),
    #                 ]:
    #                 self.check(specs, added)


def test_display_actions_0():
    os.environ['CONDA_SHOW_CHANNEL_URLS'] = 'False'
    reset_context(())
    actions = defaultdict(list, {"FETCH": [Dist('defaults::sympy-0.7.2-py27_0'), Dist("defaults::numpy-1.7.1-py27_0")]})
    # The older test index doesn't have the size metadata
    d = Dist.from_string('defaults::sympy-0.7.2-py27_0.tar.bz2')
    index[d] = IndexRecord.from_objects(index[d], size=4374752)
    d = Dist.from_string("defaults::numpy-1.7.1-py27_0.tar.bz2")
    index[d] = IndexRecord.from_objects(index[d], size=5994338)

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be downloaded:

    package                    |            build
    ---------------------------|-----------------
    sympy-0.7.2                |           py27_0         4.2 MB
    numpy-1.7.1                |           py27_0         5.7 MB
    ------------------------------------------------------------
                                           Total:         9.9 MB

"""

    actions = defaultdict(list, {'PREFIX':
    '/Users/aaronmeurer/anaconda/envs/test', 'SYMLINK_CONDA':
    ['/Users/aaronmeurer/anaconda'], 'LINK': ['defaults::python-3.3.2-0', 'defaults::readline-6.2-0 1', 'defaults::sqlite-3.7.13-0 1', 'defaults::tk-8.5.13-0 1', 'defaults::zlib-1.2.7-0 1']})

    with captured() as c:
        display_actions(actions, index)


    assert c.stdout == """Package plan for environment '/Users/aaronmeurer/anaconda/envs/test':

The following NEW packages will be INSTALLED:

    python:   3.3.2-0 \n\
    readline: 6.2-0   \n\
    sqlite:   3.7.13-0
    tk:       8.5.13-0
    zlib:     1.2.7-0 \n\

"""

    actions['UNLINK'] = actions['LINK']
    actions['LINK'] = []

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """Package plan for environment '/Users/aaronmeurer/anaconda/envs/test':

The following packages will be REMOVED:

    python:   3.3.2-0 \n\
    readline: 6.2-0   \n\
    sqlite:   3.7.13-0
    tk:       8.5.13-0
    zlib:     1.2.7-0 \n\

"""

    actions = defaultdict(list, {'LINK': ['defaults::cython-0.19.1-py33_0'], 'UNLINK':
    ['defaults::cython-0.19-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be UPDATED:

    cython: 0.19-py33_0 --> 0.19.1-py33_0

"""

    actions['LINK'], actions['UNLINK'] = actions['UNLINK'], actions['LINK']

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be DOWNGRADED:

    cython: 0.19.1-py33_0 --> 0.19-py33_0

"""

    actions = defaultdict(list, {'LINK': ['defaults::cython-0.19.1-py33_0',
        'defaults::dateutil-1.5-py33_0', 'defaults::numpy-1.7.1-py33_0'], 'UNLINK':
        ['defaults::cython-0.19-py33_0', 'defaults::dateutil-2.1-py33_1', 'defaults::pip-1.3.1-py33_1']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following NEW packages will be INSTALLED:

    numpy:    1.7.1-py33_0

The following packages will be REMOVED:

    pip:      1.3.1-py33_1

The following packages will be UPDATED:

    cython:   0.19-py33_0  --> 0.19.1-py33_0

The following packages will be DOWNGRADED:

    dateutil: 2.1-py33_1   --> 1.5-py33_0   \n\

"""

    actions = defaultdict(list, {'LINK': ['defaults::cython-0.19.1-py33_0',
        'defaults::dateutil-2.1-py33_1'], 'UNLINK':  ['defaults::cython-0.19-py33_0',
            'defaults::dateutil-1.5-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be UPDATED:

    cython:   0.19-py33_0 --> 0.19.1-py33_0
    dateutil: 1.5-py33_0  --> 2.1-py33_1   \n\

"""

    actions['LINK'], actions['UNLINK'] = actions['UNLINK'], actions['LINK']

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be DOWNGRADED:

    cython:   0.19.1-py33_0 --> 0.19-py33_0
    dateutil: 2.1-py33_1    --> 1.5-py33_0 \n\

"""


def test_display_actions_show_channel_urls():
    os.environ['CONDA_SHOW_CHANNEL_URLS'] = 'True'
    reset_context(())
    actions = defaultdict(list, {"FETCH": ['sympy-0.7.2-py27_0',
        "numpy-1.7.1-py27_0"]})
    # The older test index doesn't have the size metadata
    d = Dist('sympy-0.7.2-py27_0.tar.bz2')
    index[d] = DPkg(d, size=4374752)
    d = Dist('numpy-1.7.1-py27_0.tar.bz2')
    index[d] = DPkg(d, size=5994338)

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be downloaded:

    package                    |            build
    ---------------------------|-----------------
    sympy-0.7.2                |           py27_0         4.2 MB  <unknown>
    numpy-1.7.1                |           py27_0         5.7 MB  <unknown>
    ------------------------------------------------------------
                                           Total:         9.9 MB

"""

    actions = defaultdict(list, {'PREFIX':
    '/Users/aaronmeurer/anaconda/envs/test', 'SYMLINK_CONDA':
    ['/Users/aaronmeurer/anaconda'], 'LINK': ['defaults::python-3.3.2-0', 'defaults::readline-6.2-0', 'defaults::sqlite-3.7.13-0', 'defaults::tk-8.5.13-0', 'defaults::zlib-1.2.7-0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """Package plan for environment '/Users/aaronmeurer/anaconda/envs/test':

The following NEW packages will be INSTALLED:

    python:   3.3.2-0  defaults
    readline: 6.2-0    defaults
    sqlite:   3.7.13-0 defaults
    tk:       8.5.13-0 defaults
    zlib:     1.2.7-0  defaults

"""

    actions['UNLINK'] = actions['LINK']
    actions['LINK'] = []

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """Package plan for environment '/Users/aaronmeurer/anaconda/envs/test':

The following packages will be REMOVED:

    python:   3.3.2-0  defaults
    readline: 6.2-0    defaults
    sqlite:   3.7.13-0 defaults
    tk:       8.5.13-0 defaults
    zlib:     1.2.7-0  defaults

"""

    actions = defaultdict(list, {'LINK': ['defaults::cython-0.19.1-py33_0'], 'UNLINK':
    ['defaults::cython-0.19-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be UPDATED:

    cython: 0.19-py33_0 defaults --> 0.19.1-py33_0 defaults

"""

    actions['LINK'], actions['UNLINK'] = actions['UNLINK'], actions['LINK']

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be DOWNGRADED:

    cython: 0.19.1-py33_0 defaults --> 0.19-py33_0 defaults

"""

    actions = defaultdict(list, {'LINK': ['defaults::cython-0.19.1-py33_0',
        'defaults::dateutil-1.5-py33_0', 'defaults::numpy-1.7.1-py33_0'], 'UNLINK':
        ['defaults::cython-0.19-py33_0', 'defaults::dateutil-2.1-py33_1', 'defaults::pip-1.3.1-py33_1']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following NEW packages will be INSTALLED:

    numpy:    1.7.1-py33_0 defaults

The following packages will be REMOVED:

    pip:      1.3.1-py33_1 defaults

The following packages will be UPDATED:

    cython:   0.19-py33_0  defaults --> 0.19.1-py33_0 defaults

The following packages will be DOWNGRADED:

    dateutil: 2.1-py33_1   defaults --> 1.5-py33_0    defaults

"""

    actions = defaultdict(list, {'LINK': ['defaults::cython-0.19.1-py33_0',
        'defaults::dateutil-2.1-py33_1'], 'UNLINK':  ['defaults::cython-0.19-py33_0',
            'defaults::dateutil-1.5-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be UPDATED:

    cython:   0.19-py33_0 defaults --> 0.19.1-py33_0 defaults
    dateutil: 1.5-py33_0  defaults --> 2.1-py33_1    defaults

"""

    actions['LINK'], actions['UNLINK'] = actions['UNLINK'], actions['LINK']

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be DOWNGRADED:

    cython:   0.19.1-py33_0 defaults --> 0.19-py33_0 defaults
    dateutil: 2.1-py33_1    defaults --> 1.5-py33_0  defaults

"""

    actions['LINK'], actions['UNLINK'] = actions['UNLINK'], actions['LINK']

    d = Dist('defaults::cython-0.19.1-py33_0.tar.bz2')
    index[d] = DPkg(d, channel='my_channel')
    d = Dist('defaults::dateutil-1.5-py33_0.tar.bz2')
    index[d] = DPkg(d, channel='my_channel')

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be UPDATED:

    cython:   0.19-py33_0 defaults   --> 0.19.1-py33_0 my_channel
    dateutil: 1.5-py33_0  my_channel --> 2.1-py33_1    defaults  \n\

"""

    actions['LINK'], actions['UNLINK'] = actions['UNLINK'], actions['LINK']

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be DOWNGRADED:

    cython:   0.19.1-py33_0 my_channel --> 0.19-py33_0 defaults  \n\
    dateutil: 2.1-py33_1    defaults   --> 1.5-py33_0  my_channel

"""


@pytest.mark.xfail(strict=True, reason="Not reporting link type until refactoring display_actions "
                                       "after txn.verify()")
def test_display_actions_link_type():
    os.environ['CONDA_SHOW_CHANNEL_URLS'] = 'False'
    reset_context(())

    actions = defaultdict(list, {'LINK': ['cython-0.19.1-py33_0 2', 'dateutil-1.5-py33_0 2',
    'numpy-1.7.1-py33_0 2', 'python-3.3.2-0 2', 'readline-6.2-0 2', 'sqlite-3.7.13-0 2', 'tk-8.5.13-0 2', 'zlib-1.2.7-0 2']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following NEW packages will be INSTALLED:

    cython:   0.19.1-py33_0 (softlink)
    dateutil: 1.5-py33_0    (softlink)
    numpy:    1.7.1-py33_0  (softlink)
    python:   3.3.2-0       (softlink)
    readline: 6.2-0         (softlink)
    sqlite:   3.7.13-0      (softlink)
    tk:       8.5.13-0      (softlink)
    zlib:     1.2.7-0       (softlink)

"""

    actions = defaultdict(list, {'LINK': ['cython-0.19.1-py33_0 2',
        'dateutil-2.1-py33_1 2'], 'UNLINK':  ['cython-0.19-py33_0',
            'dateutil-1.5-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be UPDATED:

    cython:   0.19-py33_0 --> 0.19.1-py33_0 (softlink)
    dateutil: 1.5-py33_0  --> 2.1-py33_1    (softlink)

"""

    actions = defaultdict(list, {'LINK': ['cython-0.19-py33_0 2',
        'dateutil-1.5-py33_0 2'], 'UNLINK':  ['cython-0.19.1-py33_0',
            'dateutil-2.1-py33_1']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be DOWNGRADED:

    cython:   0.19.1-py33_0 --> 0.19-py33_0 (softlink)
    dateutil: 2.1-py33_1    --> 1.5-py33_0  (softlink)

"""

    actions = defaultdict(list, {'LINK': ['cython-0.19.1-py33_0 1', 'dateutil-1.5-py33_0 1',
    'numpy-1.7.1-py33_0 1', 'python-3.3.2-0 1', 'readline-6.2-0 1', 'sqlite-3.7.13-0 1', 'tk-8.5.13-0 1', 'zlib-1.2.7-0 1']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following NEW packages will be INSTALLED:

    cython:   0.19.1-py33_0
    dateutil: 1.5-py33_0   \n\
    numpy:    1.7.1-py33_0 \n\
    python:   3.3.2-0      \n\
    readline: 6.2-0        \n\
    sqlite:   3.7.13-0     \n\
    tk:       8.5.13-0     \n\
    zlib:     1.2.7-0      \n\

"""

    actions = defaultdict(list, {'LINK': ['cython-0.19.1-py33_0 1',
        'dateutil-2.1-py33_1 1'], 'UNLINK':  ['cython-0.19-py33_0',
            'dateutil-1.5-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be UPDATED:

    cython:   0.19-py33_0 --> 0.19.1-py33_0
    dateutil: 1.5-py33_0  --> 2.1-py33_1   \n\

"""

    actions = defaultdict(list, {'LINK': ['cython-0.19-py33_0 1',
        'dateutil-1.5-py33_0 1'], 'UNLINK':  ['cython-0.19.1-py33_0',
            'dateutil-2.1-py33_1']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be DOWNGRADED:

    cython:   0.19.1-py33_0 --> 0.19-py33_0
    dateutil: 2.1-py33_1    --> 1.5-py33_0 \n\

"""

    actions = defaultdict(list, {'LINK': ['cython-0.19.1-py33_0 3', 'dateutil-1.5-py33_0 3',
    'numpy-1.7.1-py33_0 3', 'python-3.3.2-0 3', 'readline-6.2-0 3', 'sqlite-3.7.13-0 3', 'tk-8.5.13-0 3', 'zlib-1.2.7-0 3']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following NEW packages will be INSTALLED:

    cython:   0.19.1-py33_0 (copy)
    dateutil: 1.5-py33_0    (copy)
    numpy:    1.7.1-py33_0  (copy)
    python:   3.3.2-0       (copy)
    readline: 6.2-0         (copy)
    sqlite:   3.7.13-0      (copy)
    tk:       8.5.13-0      (copy)
    zlib:     1.2.7-0       (copy)

"""

    actions = defaultdict(list, {'LINK': ['cython-0.19.1-py33_0 3',
        'dateutil-2.1-py33_1 3'], 'UNLINK':  ['cython-0.19-py33_0',
            'dateutil-1.5-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be UPDATED:

    cython:   0.19-py33_0 --> 0.19.1-py33_0 (copy)
    dateutil: 1.5-py33_0  --> 2.1-py33_1    (copy)

"""

    actions = defaultdict(list, {'LINK': ['cython-0.19-py33_0 3',
        'dateutil-1.5-py33_0 3'], 'UNLINK':  ['cython-0.19.1-py33_0',
            'dateutil-2.1-py33_1']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be DOWNGRADED:

    cython:   0.19.1-py33_0 --> 0.19-py33_0 (copy)
    dateutil: 2.1-py33_1    --> 1.5-py33_0  (copy)

"""
    os.environ['CONDA_SHOW_CHANNEL_URLS'] = 'True'
    reset_context(())

    d = Dist('cython-0.19.1-py33_0.tar.bz2')
    index[d] = IndexRecord.from_objects(index[d], channel='my_channel')

    d = Dist('dateutil-1.5-py33_0.tar.bz2')
    index[d] = IndexRecord.from_objects(index[d], channel='my_channel')

    actions = defaultdict(list, {'LINK': ['cython-0.19.1-py33_0 3', 'dateutil-1.5-py33_0 3',
    'numpy-1.7.1-py33_0 3', 'python-3.3.2-0 3', 'readline-6.2-0 3', 'sqlite-3.7.13-0 3', 'tk-8.5.13-0 3', 'zlib-1.2.7-0 3']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following NEW packages will be INSTALLED:

    cython:   0.19.1-py33_0 my_channel (copy)
    dateutil: 1.5-py33_0    my_channel (copy)
    numpy:    1.7.1-py33_0  <unknown>  (copy)
    python:   3.3.2-0       <unknown>  (copy)
    readline: 6.2-0         <unknown>  (copy)
    sqlite:   3.7.13-0      <unknown>  (copy)
    tk:       8.5.13-0      <unknown>  (copy)
    zlib:     1.2.7-0       <unknown>  (copy)

"""

    actions = defaultdict(list, {'LINK': ['cython-0.19.1-py33_0 3',
        'dateutil-2.1-py33_1 3'], 'UNLINK':  ['cython-0.19-py33_0',
            'dateutil-1.5-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be UPDATED:

    cython:   0.19-py33_0 <unknown>  --> 0.19.1-py33_0 my_channel (copy)
    dateutil: 1.5-py33_0  my_channel --> 2.1-py33_1    <unknown>  (copy)

"""

    actions = defaultdict(list, {'LINK': ['cython-0.19-py33_0 3',
        'dateutil-1.5-py33_0 3'], 'UNLINK':  ['cython-0.19.1-py33_0',
            'dateutil-2.1-py33_1']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be DOWNGRADED:

    cython:   0.19.1-py33_0 my_channel --> 0.19-py33_0 <unknown>  (copy)
    dateutil: 2.1-py33_1    <unknown>  --> 1.5-py33_0  my_channel (copy)

"""


def test_display_actions_features():
    os.environ['CONDA_SHOW_CHANNEL_URLS'] = 'False'
    reset_context(())

    actions = defaultdict(list, {'LINK': ['defaults::numpy-1.7.1-py33_p0', 'defaults::cython-0.19-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following NEW packages will be INSTALLED:

    cython: 0.19-py33_0  \n\
    numpy:  1.7.1-py33_p0 [mkl]

"""

    actions = defaultdict(list, {'UNLINK': ['defaults::numpy-1.7.1-py33_p0', 'defaults::cython-0.19-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be REMOVED:

    cython: 0.19-py33_0  \n\
    numpy:  1.7.1-py33_p0 [mkl]

"""

    actions = defaultdict(list, {'UNLINK': ['defaults::numpy-1.7.1-py33_p0'], 'LINK': ['defaults::numpy-1.7.0-py33_p0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be DOWNGRADED:

    numpy: 1.7.1-py33_p0 [mkl] --> 1.7.0-py33_p0 [mkl]

"""

    actions = defaultdict(list, {'LINK': ['defaults::numpy-1.7.1-py33_p0'], 'UNLINK': ['defaults::numpy-1.7.0-py33_p0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be UPDATED:

    numpy: 1.7.0-py33_p0 [mkl] --> 1.7.1-py33_p0 [mkl]

"""

    actions = defaultdict(list, {'LINK': ['defaults::numpy-1.7.1-py33_p0'], 'UNLINK': ['defaults::numpy-1.7.1-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    # NB: Packages whose version do not changed are put in UPDATED
    assert c.stdout == """
The following packages will be UPDATED:

    numpy: 1.7.1-py33_0 --> 1.7.1-py33_p0 [mkl]

"""

    actions = defaultdict(list, {'UNLINK': ['defaults::numpy-1.7.1-py33_p0'], 'LINK': ['defaults::numpy-1.7.1-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be UPDATED:

    numpy: 1.7.1-py33_p0 [mkl] --> 1.7.1-py33_0

"""
    os.environ['CONDA_SHOW_CHANNEL_URLS'] = 'True'
    reset_context(())

    actions = defaultdict(list, {'LINK': ['defaults::numpy-1.7.1-py33_p0', 'defaults::cython-0.19-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following NEW packages will be INSTALLED:

    cython: 0.19-py33_0   defaults
    numpy:  1.7.1-py33_p0 defaults [mkl]

"""

    actions = defaultdict(list, {'UNLINK': ['defaults::numpy-1.7.1-py33_p0', 'defaults::cython-0.19-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be REMOVED:

    cython: 0.19-py33_0   defaults
    numpy:  1.7.1-py33_p0 defaults [mkl]

"""

    actions = defaultdict(list, {'UNLINK': ['defaults::numpy-1.7.1-py33_p0'], 'LINK': ['defaults::numpy-1.7.0-py33_p0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be DOWNGRADED:

    numpy: 1.7.1-py33_p0 defaults [mkl] --> 1.7.0-py33_p0 defaults [mkl]

"""

    actions = defaultdict(list, {'LINK': ['defaults::numpy-1.7.1-py33_p0'], 'UNLINK': ['defaults::numpy-1.7.0-py33_p0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be UPDATED:

    numpy: 1.7.0-py33_p0 defaults [mkl] --> 1.7.1-py33_p0 defaults [mkl]

"""

    actions = defaultdict(list, {'LINK': ['defaults::numpy-1.7.1-py33_p0'], 'UNLINK': ['defaults::numpy-1.7.1-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    # NB: Packages whose version do not changed are put in UPDATED
    assert c.stdout == """
The following packages will be UPDATED:

    numpy: 1.7.1-py33_0 defaults --> 1.7.1-py33_p0 defaults [mkl]

"""

    actions = defaultdict(list, {'UNLINK': ['defaults::numpy-1.7.1-py33_p0'], 'LINK': ['defaults::numpy-1.7.1-py33_0']})

    with captured() as c:
        display_actions(actions, index)

    assert c.stdout == """
The following packages will be UPDATED:

    numpy: 1.7.1-py33_p0 defaults [mkl] --> 1.7.1-py33_0 defaults

"""


class TestDeprecatedExecutePlan(unittest.TestCase):

    def test_update_old_plan(self):
        old_plan = ['# plan', 'INSTRUCTION arg']
        new_plan = plan.update_old_plan(old_plan)

        expected = [('INSTRUCTION', 'arg')]
        self.assertEqual(new_plan, expected)

        with self.assertRaises(CondaError):
            plan.update_old_plan(['INVALID'])

    def test_execute_plan(self):
        initial_commands = inst.commands

        def set_commands(cmds):
            inst.commands = cmds
        self.addCleanup(lambda : set_commands(initial_commands))

        def INSTRUCTION_CMD(state, arg):
            INSTRUCTION_CMD.called = True
            INSTRUCTION_CMD.arg = arg

        set_commands({'INSTRUCTION': INSTRUCTION_CMD})

        old_plan = ['# plan', 'INSTRUCTION arg']

        plan.execute_plan(old_plan)

        self.assertTrue(INSTRUCTION_CMD.called)
        self.assertEqual(INSTRUCTION_CMD.arg, 'arg')


class PlanFromActionsTests(unittest.TestCase):
    py_ver = ''.join(str(x) for x in sys.version_info[:2])

    def test_plan_link_menuinst(self):
        menuinst = Dist('menuinst-1.4.2-py27_0')
        menuinst_record = DPkg(menuinst)
        ipython = Dist('ipython-5.1.0-py27_1')
        ipython_record = DPkg(ipython)
        actions = defaultdict(list)
        actions.update({
            'PREFIX': 'aprefix',
            'LINK': [ipython, menuinst],
        })

        conda_plan = plan.plan_from_actions(actions, {
            menuinst: menuinst_record,
            ipython: ipython_record,
        })

        expected_plan = [
            ('PREFIX', 'aprefix'),
            ('PRINT', 'Linking packages ...'),
            # ('PROGRESS', '2'),
            ('PROGRESSIVEFETCHEXTRACT', ProgressiveFetchExtract(index, (ipython, menuinst))),
            ('UNLINKLINKTRANSACTION', ((), (ipython), menuinst)),
        ]

        if on_win:
            # menuinst should be linked first
            expected_plan = [
                ('PREFIX', 'aprefix'),
                ('PRINT', 'Linking packages ...'),
                # ('PROGRESS', '1'),
                ('PROGRESSIVEFETCHEXTRACT', ProgressiveFetchExtract(index, (menuinst, ipython))),
                ('UNLINKLINKTRANSACTION', ((), (menuinst, ipython))),
            ]

            # last_two = expected_plan[-2:]
            # expected_plan[-2:] = last_two[::-1]
        assert expected_plan[0] == conda_plan[0]
        assert expected_plan[1] == conda_plan[1]
        # assert expected_plan[2] == conda_plan[2]  fails, but probably isn't relevant anymore


def generate_mocked_resolve(pkgs, install=None):
    mock_package = namedtuple("IndexRecord",
                              ["preferred_env", "name", "schannel", "version", "fn"])
    mock_resolve = namedtuple("Resolve", ["get_dists_for_spec", "index", "explicit", "install",
                                          "package_name", "dependency_sort"])

    index = {}
    groups = defaultdict(list)
    for preferred_env, name, schannel, version in pkgs:
        dist = Dist.from_string('%s-%s-0' % (name, version), channel_override=schannel)
        pkg = mock_package(preferred_env=preferred_env, name=name, schannel=schannel,
                           version=version, fn=name)
        groups[name].append(dist)
        index[dist] = pkg

    def get_dists_for_spec(spec, emptyok=False):
        # Here, spec should be a MatchSpec
        res = groups[spec.name]
        if not res and not emptyok:
            raise NoPackagesFoundError([(spec,)])
        return res

    def get_explicit(spec):
        return True

    def get_install(spec, installed, update_deps=None):
        return install

    def get_package_name(dist):
        return dist.name

    def get_dependency_sort(specs):
        return tuple(spec for spec in specs.values())

    return mock_resolve(get_dists_for_spec=get_dists_for_spec, index=index, explicit=get_explicit,
                        install=get_install, package_name=get_package_name,
                        dependency_sort=get_dependency_sort)


def generate_mocked_record(dist_name):
    mocked_record = namedtuple("Record", ["dist_name"])
    return mocked_record(dist_name=dist_name)


def generate_mocked_context(prefix, root_prefix, envs_dirs):
    mocked_context = namedtuple("Context", ["prefix", "root_prefix", "envs_dirs"])
    return mocked_context(prefix=prefix, root_prefix=root_prefix, envs_dirs=envs_dirs)


class TestDetermineAllEnvs(unittest.TestCase):
    def setUp(self):
        self.res = generate_mocked_resolve([
            ("ranenv", "test-spec", "rando_chnl", "1"),
            (None, "test-spec", "defaults", "5"),
            ("test1", "test-spec2", "defaults", "1")
        ])
        self.specs = [MatchSpec("test-spec"), MatchSpec("test-spec2")]

    def test_determine_all_envs(self):
        specs_for_envs = plan.determine_all_envs(self.res, self.specs)
        expected_output = (plan.SpecForEnv(env=None, spec="test-spec"),
                           plan.SpecForEnv(env="test1", spec="test-spec2"))
        self.assertEquals(specs_for_envs, expected_output)

    def test_determine_all_envs_with_channel_priority(self):
        self.res = generate_mocked_resolve([
            (None, "test-spec", "defaults", "5"),
            ("ranenv", "test-spec", "rando_chnl", "1"),
            ("test1", "test-spec2", "defaults", "1")
        ])
        prioritized_channel_map = prioritize_channels(tuple(["rando_chnl", "defaults"]))
        specs_for_envs_w_channel_priority = plan.determine_all_envs(
            self.res, self.specs, prioritized_channel_map)
        expected_output = (plan.SpecForEnv(env="ranenv", spec="test-spec"),
                           plan.SpecForEnv(env="test1", spec="test-spec2"))
        self.assertEquals(specs_for_envs_w_channel_priority, expected_output)

    def test_determine_all_envs_no_package(self):
        specs = [MatchSpec("no-exist")]
        with pytest.raises(NoPackagesFoundError) as err:
            plan.determine_all_envs(self.res, specs)
            assert "no-exist package not found" in str(err)


class TestEnsurePackageNotDuplicatedInPrivateEnvRoot(unittest.TestCase):
    def setUp(self):
        self.linked_in_root = {
            Dist("test1-1.2.3-bs_7"): generate_mocked_record("test1-1.2.3-bs_7")
        }

    def test_try_install_duplicate_package_in_root(self):
        dists_for_envs = [plan.SpecForEnv(env="_env_", spec="test1"),
                          plan.SpecForEnv(env=None, spec="something")]
        with pytest.raises(InstallError) as err:
            plan.ensure_packge_not_duplicated_in_private_env_root(
                dists_for_envs, self.linked_in_root)
            assert "Package test1 is already installed" in str(err)
            assert "Can't install in private environment _env_" in str(err)

    def test_try_install_duplicate_package_in_private_env(self):
        dists_for_envs = [plan.SpecForEnv(env="_env_", spec="test2"),
                          plan.SpecForEnv(env=None, spec="test3")]
        with patch.object(common, "prefix_if_in_private_env") as mock_prefix:
            mock_prefix.return_value = "some/prefix"
            with pytest.raises(InstallError) as err:
                plan.ensure_packge_not_duplicated_in_private_env_root(
                    dists_for_envs, self.linked_in_root)
                assert "Package test3 is already installed" in str(err)
                assert "private_env some/prefix" in str(err)

    def test_try_install_no_duplicate(self):
        dists_for_envs = [plan.SpecForEnv(env="_env_", spec="test2"),
                          plan.SpecForEnv(env=None, spec="test3")]
        plan.ensure_packge_not_duplicated_in_private_env_root(dists_for_envs, self.linked_in_root)


# Includes testing for determine_dists_per_prefix and match_to_original_specs
class TestGroupDistsForPrefix(unittest.TestCase):
    def setUp(self):
        pkgs = [
            (None, "test-spec", "default", "1"),
            ("ranenv", "test-spec", "default", "5"),
            ("test1", "test-spec2", "default", "1")]
        self.res = generate_mocked_resolve(pkgs)
        self.specs = [MatchSpec("test-spec"), MatchSpec("test-spec2")]
        self.context = generate_mocked_context(
            "some/prefix", "some/prefix", ["some/prefix/envs", "some/prefix/envs/_pre_"])

    def test_not_requires_private_env(self):
        with patch.object(plan, "not_requires_private_env") as not_requires:
            not_requires.return_value = True
            dists_for_envs = [plan.SpecForEnv(env=None, spec="test-spec"),
                              plan.SpecForEnv(env=None, spec="test-spec2")]
            specs_for_prefix = plan.determine_dists_per_prefix(
                self.res, "some/envs/prefix", self.res.index, "prefix", dists_for_envs, self.context)
        expected_output = [plan.SpecsForPrefix(
            prefix="some/envs/prefix", r=self.res, specs={"test-spec", "test-spec2"})]
        self.assertEquals(specs_for_prefix, expected_output)

    @patch.object(plan, "not_requires_private_env", return_value=False)
    def test_determine_dists_per_prefix(self, not_requires):
        with patch.object(plan, "get_resolve_object") as gen_resolve_object_mock:
            gen_resolve_object_mock.return_value = self.res
            dists_for_envs = [plan.SpecForEnv(env=None, spec="test-spec"),
                              plan.SpecForEnv(env=None, spec="test-spec2"),
                              plan.SpecForEnv(env="ranenv", spec="test")]
            specs_for_prefix = plan.determine_dists_per_prefix(
                self.res, "some/prefix", self.res.index, ["ranenv", None], dists_for_envs, self.context)
            expected_output = [
                plan.SpecsForPrefix(prefix="some/prefix/envs/_ranenv_",
                                    r=gen_resolve_object_mock(),
                                    specs={"test"}),
                plan.SpecsForPrefix(prefix="some/prefix", r=self.res,
                                    specs=IndexedSet(("test-spec", "test-spec2")))
            ]
        self.assertEquals(expected_output, specs_for_prefix)

    def test_match_to_original_specs(self):
        str_specs = ["test 1.2.0", "test-spec 1.1*", "test-spec2 <4.3"]
        test_r = self.res
        grouped_specs = [
            plan.SpecsForPrefix(prefix="some/prefix/envs/_ranenv_",
                                r=test_r,
                                specs=IndexedSet(("test",))),
            plan.SpecsForPrefix(prefix="some/prefix", r=self.res,
                                specs=IndexedSet(("test-spec", "test-spec2")))]
        matched = plan.match_to_original_specs(tuple(MatchSpec(s) for s in str_specs),
                                               grouped_specs)
        expected_output = [
            plan.SpecsForPrefix(prefix="some/prefix/envs/_ranenv_",
                                r=test_r,
                                specs=[MatchSpec("test 1.2.0")]),
            plan.SpecsForPrefix(prefix="some/prefix", r=self.res,
                                specs=[MatchSpec("test-spec 1.1*"), MatchSpec("test-spec2 <4.3")])]

        assert len(matched) == len(expected_output)
        assert matched == expected_output


class TestGetActionsForDist(unittest.TestCase):
    def setUp(self):
        self.pkgs = [
            (None, "test-spec", "defaults", "1"),
            ("ranenv", "test-spec", "defaults", "5"),
            (None, "test-spec2", "defaults", "1"),
            ("ranenv", "test", "defaults", "1.2.0")]
        self.res = generate_mocked_resolve(self.pkgs)

    # TODO: ensure_linked_actions is going away; only used in plan._remove_actions
    # @patch("conda.core.linked_data.is_linked", return_value=True)
    # def test_ensure_linked_actions_all_linked(self, load_meta):
    #     dists = [Dist("test-88"), Dist("test-spec-42"), Dist("test-spec2-8.0.0.0.1-9")]
    #     prefix = "some/prefix"
    #
    #     link_actions = plan.ensure_linked_actions(dists, prefix)
    #
    #     expected_output = defaultdict(list)
    #     expected_output["PREFIX"] = prefix
    #     expected_output["op_order"] = ('CHECK_FETCH', 'RM_FETCHED', 'FETCH', 'CHECK_EXTRACT',
    #                                    'RM_EXTRACTED', 'EXTRACT', 'UNLINK', 'LINK',
    #                                    'SYMLINK_CONDA')
    #     self.assertEquals(link_actions, expected_output)
    #
    # @patch("conda.core.linked_data.is_linked", return_value=False)
    # def test_ensure_linked_actions_no_linked(self, load_meta):
    #     dists = [Dist("test-88"), Dist("test-spec-42"), Dist("test-spec2-8.0.0.0.1-9")]
    #     prefix = "some/prefix"
    #
    #     link_actions = plan.ensure_linked_actions(dists, prefix)
    #
    #     expected_output = defaultdict(list)
    #     expected_output["PREFIX"] = prefix
    #     expected_output["op_order"] = ('CHECK_FETCH', 'RM_FETCHED', 'FETCH', 'CHECK_EXTRACT',
    #                                    'RM_EXTRACTED', 'EXTRACT', 'UNLINK', 'LINK',
    #                                    'SYMLINK_CONDA')
    #     expected_output["LINK"] = [Dist("test-88"), Dist("test-spec-42"), Dist("test-spec2-8.0.0.0.1-9")]
    #     self.assertEquals(link_actions, expected_output)

    # def test_get_actions_for_dist(self):
    #     install = [Dist("test-1.2.0-py36_7")]
    #     r = generate_mocked_resolve(self.pkgs, install)
    #     dists_for_prefix = plan.SpecsForPrefix(prefix="some/prefix/envs/_ranenv_", r=r,
    #                                            specs=["test 1.2.0"])
    #     actions = plan.get_actions_for_dists(dists_for_prefix, None, self.res.index, None, False,
    #                                          False, True, True)
    #
    #     expected_output = defaultdict(list)
    #     expected_output["PREFIX"] = "some/prefix/envs/_ranenv_"
    #     expected_output["op_order"] = ('CHECK_FETCH', 'RM_FETCHED', 'FETCH', 'CHECK_EXTRACT',
    #                                    'RM_EXTRACTED', 'EXTRACT', 'UNLINK', 'LINK',
    #                                    'SYMLINK_CONDA')
    #     expected_output["LINK"] = [Dist("test-1.2.0-py36_7")]
    #     expected_output["SYMLINK_CONDA"] = [context.root_dir]
    #
    #     self.assertEquals(actions, expected_output)
    #
    # def test_get_actions_multiple_dists(self):
    #     install = [Dist("testspec2-4.3.0-1"), Dist("testspecs-1.1.1-4")]
    #     r = generate_mocked_resolve(self.pkgs, install)
    #     dists_for_prefix = plan.SpecsForPrefix(prefix="root/prefix", r=r,
    #                                            specs=["testspec2 <4.3", "testspecs 1.1*"])
    #     actions = plan.get_actions_for_dists(dists_for_prefix, None, self.res.index, None, False,
    #                                          False, True, True)
    #
    #     expected_output = defaultdict(list)
    #     expected_output["PREFIX"] = "root/prefix"
    #     expected_output["op_order"] = ('CHECK_FETCH', 'RM_FETCHED', 'FETCH', 'CHECK_EXTRACT',
    #                                    'RM_EXTRACTED', 'EXTRACT', 'UNLINK', 'LINK',
    #                                    'SYMLINK_CONDA')
    #     expected_output["LINK"] = [Dist("testspec2-4.3.0-1"), Dist("testspecs-1.1.1-4")]
    #     expected_output["SYMLINK_CONDA"] = [context.root_dir]
    #
    #     assert actions == expected_output
    #
    # @patch("conda.core.linked_data.load_linked_data", return_value=[Dist("testspec1-0.9.1-py27_2")])
    # def test_get_actions_multiple_dists_and_unlink(self, load_linked_data):
    #     install = [Dist("testspec2-4.3.0-2"), Dist("testspec1-1.1.1-py27_0")]
    #     r = generate_mocked_resolve(self.pkgs, install)
    #     dists_for_prefix = plan.SpecsForPrefix(prefix="root/prefix", r=r,
    #                                            specs=["testspec2 <4.3", "testspec1 1.1*"])
    #
    #     test_link_data = {"root/prefix": {Dist("testspec1-0.9.1-py27_2"): True}}
    #     with patch("conda.core.linked_data.linked_data_", test_link_data):
    #         actions = plan.get_actions_for_dists(dists_for_prefix, None, self.res.index, None, False,
    #                                          False, True, True)
    #
    #     expected_output = defaultdict(list)
    #     expected_output["PREFIX"] = "root/prefix"
    #     expected_output["op_order"] = ('CHECK_FETCH', 'RM_FETCHED', 'FETCH', 'CHECK_EXTRACT',
    #                                    'RM_EXTRACTED', 'EXTRACT', 'UNLINK', 'LINK',
    #                                    'SYMLINK_CONDA')
    #     expected_output["LINK"] = [Dist("testspec2-4.3.0-2"), Dist("testspec1-1.1.1-py27_0")]
    #     expected_output["UNLINK"] = [Dist("testspec1-0.9.1-py27_2")]
    #
    #     expected_output["SYMLINK_CONDA"] = [context.root_dir]
    #     assert expected_output["LINK"] == actions["LINK"]
    #     assert actions == expected_output


def generate_remove_action(prefix, unlink):
    action = defaultdict(list)
    action["op_order"] = ('CHECK_FETCH', 'RM_FETCHED', 'FETCH', 'CHECK_EXTRACT', 'RM_EXTRACTED',
                          'EXTRACT', 'UNLINK', 'LINK', 'SYMLINK_CONDA')
    action["PREFIX"] = prefix
    action["UNLINK"] = unlink
    return action


class TestAddUnlinkOptionsForUpdate(unittest.TestCase):
    def setUp(self):
        pkgs = [
            (None, "test1", "default", "1.0.1"),
            ("env", "test1", "rando_chnl", "2.1.4"),
            ("env", "test2", "default", "1.1.1"),
            (None, "test3", "default", "1.2.0"),
            (None, "test4", "default", "1.2.1")]
        self.res = generate_mocked_resolve(pkgs)

    @patch("conda.plan.remove_actions", return_value=generate_remove_action(
        "root/prefix", [Dist("rando_chnl::test1-2.1.4-0")]))
    def test_update_in_private_env_add_remove_action(self, remove_actions):
        required_solves = [plan.SpecsForPrefix(prefix="root/prefix/envs/_env_",
                                               specs=["test1", "test2"], r=self.res),
                           plan.SpecsForPrefix(prefix=context.root_dir, specs=["test3"],
                                               r=self.res)]

        action = defaultdict(list)
        action["PREFIX"] = "root/prefix/envs/_env_"
        action["LINK"] = [Dist("rando_chnl::test1-2.1.4-0"), Dist("test2-1.1.1-0")]
        actions = [action]

        test_link_data = {context.root_prefix: {Dist("rando_chnl::test1-2.1.4-0"): True}}
        with patch("conda.core.linked_data.linked_data_", test_link_data):
            plan.add_unlink_options_for_update(actions, required_solves, self.res.index)

        expected_output = [action, generate_remove_action("root/prefix", [Dist("rando_chnl::test1-2.1.4-0")])]
        self.assertEquals(actions, expected_output)

    @patch("conda.plan.remove_actions", return_value=generate_remove_action(
        "root/prefix", [Dist("rando_chnl::test1-2.1.4-0")]))
    def test_update_in_private_env_append_unlink(self, remove_actions):
        required_solves = [plan.SpecsForPrefix(prefix="root/prefix/envs/_env_",
                                               specs=["test1", "test2"], r=self.res),
                           plan.SpecsForPrefix(prefix=context.root_prefix, specs=["whatevs"],
                                               r=self.res)]

        action = defaultdict(list)
        action["PREFIX"] = "root/prefix/envs/_env_"
        action["LINK"] = [Dist("rando_chnl::test1-2.1.4-0"), Dist("test2-1.1.1-8")]
        action_root = defaultdict(list)
        action_root["PREFIX"] = context.root_prefix
        action_root["LINK"] = [Dist("whatevs-54-54")]
        actions = [action, action_root]

        test_link_data = {context.root_prefix: {Dist("rando_chnl::test1-2.1.4-0"): True}}
        with patch("conda.core.linked_data.linked_data_", test_link_data):
            plan.add_unlink_options_for_update(actions, required_solves, self.res.index)

        aug_action_root = defaultdict(list)
        aug_action_root["PREFIX"] = context.root_prefix
        aug_action_root["LINK"] = [Dist("whatevs-54-54")]
        aug_action_root["UNLINK"] = [Dist("rando_chnl::test1-2.1.4-0")]
        expected_output = [action, aug_action_root]
        self.assertEquals(actions, expected_output)

    @patch("conda.cli.common.get_private_envs_json", return_value=
        {"test3-1.2.0": "some/prefix/envs/_env_", "test4-2.1.0-22": "some/prefix/envs/_env_"})
    def test_update_in_root_env(self, prefix_if_in_private_env):
        required_solves = [plan.SpecsForPrefix(prefix=context.root_dir, specs=["test3", "test4"],
                                               r=self.res)]

        action = defaultdict(list)
        action["PREFIX"] = "root/prefix"
        action["LINK"] = [Dist("test3-1.2.0"), Dist("test4-1.2.1")]
        actions = [action]
        plan.add_unlink_options_for_update(actions, required_solves, self.res.index)
        expected_output = [action, generate_remove_action(
            "some/prefix/envs/_env_", [Dist("test3-1.2.0"), Dist("test4-2.1.0-22")])]
        self.assertEquals(actions, expected_output)


def test_pinned_specs():
    # Test pinned specs environment variable
    specs_str_1 = ("numpy 1.11", "python >3")
    specs_1 = tuple(MatchSpec(spec_str, optional=True) for spec_str in specs_str_1)
    with env_var('CONDA_PINNED_PACKAGES', '/'.join(specs_str_1), reset_context):
        pinned_specs = plan.get_pinned_specs("/none")
        assert pinned_specs == specs_1
        assert pinned_specs != specs_str_1

    # Test pinned specs conda environment file
    specs_str_2 = ("scipy ==0.14.2", "openjdk >=8")
    specs_2 = tuple(MatchSpec(spec_str, optional=True) for spec_str in specs_str_2)
    with tempdir() as td:
        mkdir_p(join(td, 'conda-meta'))
        with open(join(td, 'conda-meta', 'pinned'), 'w') as fh:
            fh.write("\n".join(specs_str_2))
            fh.write("\n")
        pinned_specs = plan.get_pinned_specs(td)
        assert pinned_specs == specs_2
        assert pinned_specs != specs_str_2

    # Test pinned specs conda configuration and pinned specs conda environment file
    with tempdir() as td:
        mkdir_p(join(td, 'conda-meta'))
        with open(join(td, 'conda-meta', 'pinned'), 'w') as fh:
            fh.write("\n".join(specs_str_1))
            fh.write("\n")

        with env_var('CONDA_PREFIX', td, reset_context):
            run_command(Commands.CONFIG, "--env --add pinned_packages requests=2.13")
            with env_var('CONDA_PINNED_PACKAGES', '/'.join(specs_str_2), reset_context):
                pinned_specs = get_pinned_specs(td)
                expected = specs_2 + (MatchSpec("requests 2.13.*", optional=True),) + specs_1
                assert pinned_specs == expected
                assert pinned_specs != specs_str_1 + ("requests 2.13",) + specs_str_2



if __name__ == '__main__':
    unittest.main()
