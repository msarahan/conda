# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from functools import reduce
from logging import getLogger
from os.path import dirname, basename

log = getLogger(__name__)


def tokenized_startswith(test_iterable, startswith_iterable):
    return all(t == sw for t, sw in zip(test_iterable, startswith_iterable))


def get_leaf_directories(files):
    # type: (List[str]) -> List[str]
    # give this function a list of files, and it will hand back a list of leaf directories to
    # pass to os.makedirs()
    directories = sorted(set(tuple(f.split('/')[:-1]) for f in files))
    if not directories:
        return ()

    leaves = []

    def _process(x, y):
        if not tokenized_startswith(y, x):
            leaves.append(x)
        return y
    last = reduce(_process, directories)

    if not leaves:
        leaves.append(directories[-1])
    elif not tokenized_startswith(last, leaves[-1]):
        leaves.append(last)

    return tuple('/'.join(leaf) for leaf in leaves)


def missing_pyc_files(python_major_minor_version, files):
    pyver_string = python_major_minor_version.replace('.', '')
    def pyc_path(file):
        if python_major_minor_version.startswith('2'):
            return file + "c"
        else:
            base_fn, base_extention = basename(file).rsplit('.', 1)
            return "%s/__pycache__/%s.cpython-%s.%sc" % (dirname(file), base_fn, pyver_string,
                                                         base_extention)

    py_files = (f for f in files if f.endswith('.py'))
    pyc_matches = ((py_file, pyc_path(py_file)) for py_file in py_files)
    return tuple(match for match in pyc_matches if match[1] in files)


def parse_entry_point_def(ep_definition):
    cmd_mod, func = ep_definition.rsplit(':', 1)
    command, module = cmd_mod.rsplit("=", 1)
    command, module, func = command.strip(), module.strip(), func.strip()
    return command, module, func


