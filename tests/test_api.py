# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

import inspect

import pytest

from conda.api import Solver, PackageCacheData, SubdirData, PrefixData, DepsModifier
from conda.common.compat import odict, isiterable
from conda.common.constants import NULL
from conda.core.link import UnlinkLinkTransaction
from conda.models.channel import Channel
from conda.models.index_record import PackageRef


class PositionalArgument:
    pass


def inspect_arguments(f, arguments):
    result = inspect.getargspec(f)
    arg_names = result[0]
    defaults = result.defaults or ()
    default_val_first_idx = len(arg_names) - len(defaults)
    arg_values = [PositionalArgument] * default_val_first_idx + list(defaults)
    for (recorded_name, recorded_value), (arg_name, arg_value) in zip(arguments.items(), zip(arg_names, arg_values)):
        print(recorded_name, arg_name)
        assert recorded_name == arg_name
        assert recorded_value == arg_value


def test_DepsModifier_contract():
    assert DepsModifier.NO_DEPS
    assert DepsModifier.ONLY_DEPS
    assert DepsModifier.UPDATE_DEPS
    assert DepsModifier.UPDATE_DEPS_ONLY_DEPS
    assert DepsModifier.UPDATE_ALL
    assert DepsModifier.FREEZE_INSTALLED


def test_Solver_inputs_contract():
    init_args = odict((
        ('self', PositionalArgument),
        ('prefix', PositionalArgument),
        ('channels', PositionalArgument),
        ('subdirs', ()),
        ('specs_to_add', ()),
        ('specs_to_remove', ()),
    ))
    inspect_arguments(Solver.__init__, init_args)

    solve_final_state_args = odict((
        ('self', PositionalArgument),
        ('deps_modifier', NULL),
        ('prune', NULL),
        ('ignore_pinned', NULL),
        ('force_remove', NULL),
    ))
    inspect_arguments(Solver.solve_final_state, solve_final_state_args)

    solve_for_diff_args = odict((
        ('self', PositionalArgument),
        ('deps_modifier', NULL),
        ('prune', NULL),
        ('ignore_pinned', NULL),
        ('force_remove', NULL),
        ('force_reinstall', False),
    ))
    inspect_arguments(Solver.solve_for_diff, solve_for_diff_args)

    solve_for_transaction_args = odict((
        ('self', PositionalArgument),
        ('deps_modifier', NULL),
        ('prune', NULL),
        ('ignore_pinned', NULL),
        ('force_remove', NULL),
        ('force_reinstall', False),
    ))
    inspect_arguments(Solver.solve_for_transaction, solve_for_transaction_args)


@pytest.mark.integration
def test_Solver_return_value_contract():
    solver = Solver('/', (Channel('pkgs/main'),), specs_to_add=('openssl',))
    solve_final_state_rv = solver.solve_final_state()
    assert isiterable(solve_final_state_rv)
    assert all(isinstance(pref, PackageRef) for pref in solve_final_state_rv)

    solve_for_diff_rv = solver.solve_for_diff()
    assert len(solve_for_diff_rv) == 2
    unlink_precs, link_precs = solve_for_diff_rv
    assert isiterable(unlink_precs)
    assert all(isinstance(pref, PackageRef) for pref in unlink_precs)
    assert isiterable(link_precs)
    assert all(isinstance(pref, PackageRef) for pref in link_precs)

    solve_for_transaction_rv = solver.solve_for_transaction()
    assert isinstance(solve_for_transaction_rv, UnlinkLinkTransaction)


def test_SubdirData_contract():
    init_args = odict((
        ('self', PositionalArgument),
        ('channel', PositionalArgument),
    ))
    inspect_arguments(SubdirData.__init__, init_args)

    query_args = odict((
        ('self', PositionalArgument),
        ('package_ref_or_match_spec', PositionalArgument),
    ))
    inspect_arguments(SubdirData.query, query_args)

    query_all_args = odict((
        ('channels', PositionalArgument),
        ('subdirs', PositionalArgument),
        ('package_ref_or_match_spec', PositionalArgument),
    ))
    inspect_arguments(SubdirData.query_all, query_all_args)

    iter_records_args = odict((
        ('self', PositionalArgument),
    ))
    inspect_arguments(SubdirData.iter_records, iter_records_args)

    reload_args = odict((
        ('self', PositionalArgument),
    ))
    inspect_arguments(SubdirData.reload, reload_args)


def test_PackageCacheData_contract():
    init_args = odict((
        ('self', PositionalArgument),
        ('pkgs_dir', PositionalArgument),
    ))
    inspect_arguments(PackageCacheData.__init__, init_args)

    get_args = odict((
        ('self', PositionalArgument),
        ('package_ref', PositionalArgument),
        ('default', NULL),
    ))
    inspect_arguments(PackageCacheData.get, get_args)

    query_args = odict((
        ('self', PositionalArgument),
        ('package_ref_or_match_spec', PositionalArgument),
    ))
    inspect_arguments(PackageCacheData.query, query_args)

    query_all_args = odict((
        ('package_ref_or_match_spec', PositionalArgument),
        ('pkgs_dirs', None),
    ))
    inspect_arguments(PackageCacheData.query_all, query_all_args)

    iter_records_args = odict((
        ('self', PositionalArgument),
    ))
    inspect_arguments(PackageCacheData.iter_records, iter_records_args)

    isinstance(PackageCacheData.is_writable, property)

    first_writable_args = odict((
        ('pkgs_dirs', None),
    ))
    inspect_arguments(PackageCacheData.first_writable, first_writable_args)

    reload_args = odict((
        ('self', PositionalArgument),
    ))
    inspect_arguments(PackageCacheData.reload, reload_args)


def test_PrefixData_contract():
    init_args = odict((
        ('self', PositionalArgument),
        ('prefix_path', PositionalArgument),
    ))
    inspect_arguments(PrefixData.__init__, init_args)

    get_args = odict((
        ('self', PositionalArgument),
        ('package_ref', PositionalArgument),
        ('default', NULL),
    ))
    inspect_arguments(PrefixData.get, get_args)

    query_args = odict((
        ('self', PositionalArgument),
        ('package_ref_or_match_spec', PositionalArgument),
    ))
    inspect_arguments(PrefixData.query, query_args)

    iter_records_args = odict((
        ('self', PositionalArgument),
    ))
    inspect_arguments(PrefixData.iter_records, iter_records_args)

    isinstance(PrefixData.is_writable, property)

    reload_args = odict((
        ('self', PositionalArgument),
    ))
    inspect_arguments(PrefixData.reload, reload_args)
