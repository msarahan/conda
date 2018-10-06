# (c) 2012-2013 Continuum Analytics, Inc. / http://continuum.io
# All Rights Reserved
#
# conda is distributed under the terms of the BSD 3-clause license.
# Consult LICENSE.txt or http://opensource.org/licenses/BSD-3-Clause.

from __future__ import absolute_import, division, print_function, unicode_literals

import errno
import json
import logging
import sys
from argparse import RawDescriptionHelpFormatter
from collections import namedtuple
from os.path import join

from conda.common.path import is_private_env
from conda.models.dist import Dist
from conda.resolve import MatchSpec
from .common import (InstalledPackages, add_parser_channels, add_parser_help, add_parser_json,
                     add_parser_no_pin, add_parser_no_use_index_cache, add_parser_offline,
                     add_parser_prefix, add_parser_pscheck, add_parser_quiet,
                     add_parser_use_index_cache, add_parser_use_local, add_parser_yes,
                     confirm_yn, ensure_override_channels_requires_channel, ensure_use_local,
                     names_in_specs, specs_from_args, stdout_json)
from conda.base.constants import ROOT_NO_RM
from conda.core.index import get_index
from ..base.context import check_write, context
from ..gateways.disk.delete import delete_trash
from ..compat import iteritems, iterkeys
from ..console import json_progress_bars
from ..exceptions import CondaEnvironmentError, CondaValueError, PackageNotFoundError

help = "%s a list of packages from a specified conda environment."
descr = help + """

This command will also remove any package that depends on any of the
specified packages as well---unless a replacement can be found without
that dependency. If you wish to skip this dependency checking and remove
just the requested packages, add the '--force' option. Note however that
this may result in a broken environment, so use this with caution.
"""
example = """
Examples:

    conda %s -n myenv scipy

"""

uninstall_help = "Alias for conda remove.  See conda remove --help."
log = logging.getLogger(__name__)


def configure_parser(sub_parsers, name='remove'):
    if name == 'remove':
        p = sub_parsers.add_parser(
            name,
            formatter_class=RawDescriptionHelpFormatter,
            description=descr % name.capitalize(),
            help=help % name.capitalize(),
            epilog=example % name,
            add_help=False,
        )
    else:
        p = sub_parsers.add_parser(
            name,
            formatter_class=RawDescriptionHelpFormatter,
            description=uninstall_help,
            help=uninstall_help,
            epilog=example % name,
            add_help=False,
        )
    add_parser_help(p)
    add_parser_yes(p)
    add_parser_json(p)
    p.add_argument(
        "--all",
        action="store_true",
        help="%s all packages, i.e., the entire environment." % name.capitalize(),
    )
    p.add_argument(
        "--features",
        action="store_true",
        help="%s features (instead of packages)." % name.capitalize(),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Forces removal of a package without removing packages that depend on it. "
             "Using this option will usually leave your environment in a broken and "
             "inconsistent state.",
    )
    add_parser_no_pin(p)
    add_parser_channels(p)
    add_parser_prefix(p)
    add_parser_quiet(p)
    # Putting this one first makes it the default
    add_parser_no_use_index_cache(p)
    add_parser_use_index_cache(p)
    add_parser_use_local(p)
    add_parser_offline(p)
    add_parser_pscheck(p)
    p.add_argument(
        'package_names',
        metavar='package_name',
        action="store",
        nargs='*',
        help="Package names to %s from the environment." % name,
    ).completer = InstalledPackages
    p.set_defaults(func=execute)


def prefix_if_in_private_env(spec):
    path_to_private_envs = join(context.root_dir, "conda-meta", "private_envs")
    with open(path_to_private_envs, "r") as f:
        private_envs_json = json.load(f)

    # specs_match = lambda pkg: any(m for m in specs if m.match(Dist(pkg)))
    prefixes = tuple(prefix for pkg, prefix in iteritems(private_envs_json) if pkg.startswith(spec))
    prefix = prefixes[0] if len(prefixes) > 0 else None
    return prefix


def execute(args, parser):
    import conda.plan as plan
    import conda.instructions as inst
    from conda.gateways.disk.delete import rm_rf
    from conda.core.linked_data import linked_data

    if not (args.all or args.package_names):
        raise CondaValueError('no package names supplied,\n'
                              '       try "conda remove -h" for more details')

    prefix = context.prefix_w_legacy_search
    if args.all and prefix == context.default_prefix:
        msg = "cannot remove current environment. deactivate and run conda remove again"
        raise CondaEnvironmentError(msg)
    check_write('remove', prefix, json=context.json)
    ensure_use_local(args)
    ensure_override_channels_requires_channel(args)
    channel_urls = args.channel or ()
    if not args.features and args.all:
        index = linked_data(prefix)
        index = {dist: info for dist, info in iteritems(index)}
    else:
        index = get_index(channel_priority_map=channel_urls,
                          prepend=not args.override_channels,
                          use_local=args.use_local,
                          use_cache=args.use_index_cache,
                          prefix=prefix)
    specs = None
    if args.features:
        features = set(args.package_names)
        actions = plan.remove_features_actions(prefix, index, features)
        action_set = actions,
    elif args.all:
        if plan.is_root_prefix(prefix):
            raise CondaEnvironmentError('cannot remove root environment,\n'
                                        '       add -n NAME or -p PREFIX option')
        actions = {inst.PREFIX: prefix}
        for dist in sorted(iterkeys(index)):
            plan.add_unlink(actions, dist)
        action_set = actions,
    else:
        specs = specs_from_args(args.package_names)

        prefix_spec_map = {}
        for spec in specs:
            spec_prefix = prefix_if_in_private_env(spec)
            spec_prefix = spec_prefix if spec_prefix is not None else prefix
            if spec_prefix in prefix_spec_map.keys():
                prefix_spec_map[spec_prefix].append(spec)
            else:
                prefix_spec_map[spec_prefix] = [spec]

        if (context.conda_in_root
                and plan.is_root_prefix(prefix)
                and names_in_specs(ROOT_NO_RM, specs)
                and not args.force):
            raise CondaEnvironmentError('cannot remove %s from root environment' %
                                        ', '.join(ROOT_NO_RM))
        actions = []
        for prfx, spcs in iteritems(prefix_spec_map):
            index = linked_data(prfx)
            index = {dist: info for dist, info in iteritems(index)}
            actions.append(plan.remove_actions(prfx, spcs, index=index, force=args.force,
                                               pinned=args.pinned))
        action_set = tuple(actions)

    delete_trash()
    if any(plan.nothing_to_do(actions) for actions in action_set):
        if args.all:
            print("\nRemove all packages in environment %s:\n" % prefix, file=sys.stderr)
            if not args.json:
                confirm_yn(args)
            rm_rf(prefix)

            if context.json:
                stdout_json({
                    'success': True,
                    'actions': action_set
                })
            return
        raise PackageNotFoundError('', 'no packages found to remove from '
                                   'environment: %s' % prefix)
    for action in action_set:
        if not context.json:
            print()
            print("Package plan for package removal in environment %s:" % action["PREFIX"])
            plan.display_actions(action, index)

        if context.json and args.dry_run:
            stdout_json({
                'success': True,
                'dry_run': True,
                'actions': action_set
            })
            return

    if not context.json:
        confirm_yn(args)

    for actions in action_set:
        if context.json and not context.quiet:
            with json_progress_bars():
                plan.execute_actions(actions, index, verbose=not context.quiet)
        else:
            plan.execute_actions(actions, index, verbose=not context.quiet)
            if specs:
                try:
                    with open(join(prefix, 'conda-meta', 'history'), 'a') as f:
                        f.write('# remove specs: %s\n' % ','.join(specs))
                except IOError as e:
                    if e.errno == errno.EACCES:
                        log.debug("Can't write the history file")
                    else:
                        raise

        if is_private_env(action["PREFIX"]):
            # TODO: find out if we remove private env if other stuff is also installed in it
            rm_rf(prefix)

    if args.all:
        rm_rf(prefix)

    if context.json:
        stdout_json({
            'success': True,
            'actions': actions
        })
