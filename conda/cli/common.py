# -*- coding: utf-8 -*-
# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import absolute_import, division, print_function, unicode_literals

from os.path import basename, dirname
import re
import sys

from .._vendor.auxlib.ish import dals
from ..base.constants import ROOT_ENV_NAME
from ..base.context import context
from ..common.constants import NULL
from ..common.io import swallow_broken_pipe
from ..common.path import paths_equal
from ..common.serialize import json_dump
from ..models.match_spec import MatchSpec


def confirm(message="Proceed", choices=('yes', 'no'), default='yes'):
    assert default in choices, default
    if context.dry_run:
        from ..exceptions import DryRunExit
        raise DryRunExit()

    options = []
    for option in choices:
        if option == default:
            options.append('[%s]' % option[0])
        else:
            options.append(option[0])
    message = "%s (%s)? " % (message, '/'.join(options))
    choices = {alt: choice
               for choice in choices
               for alt in [choice, choice[0]]}
    choices[''] = default
    while True:
        # raw_input has a bug and prints to stderr, not desirable
        sys.stdout.write(message)
        sys.stdout.flush()
        user_choice = sys.stdin.readline().strip().lower()
        if user_choice not in choices:
            print("Invalid choice: %s" % user_choice)
        else:
            sys.stdout.write("\n")
            sys.stdout.flush()
            return choices[user_choice]


def confirm_yn(message="Proceed", default='yes', dry_run=NULL):
    dry_run = context.dry_run if dry_run is NULL else dry_run
    if dry_run:
        from ..exceptions import DryRunExit
        raise DryRunExit()
    if context.always_yes:
        return True
    try:
        choice = confirm(message=message, choices=('yes', 'no'))
    except KeyboardInterrupt as e:  # pragma: no cover
        from ..exceptions import CondaSystemExit
        raise CondaSystemExit("\nOperation aborted.  Exiting.")
    if choice == 'no':
        from ..exceptions import CondaSystemExit
        raise CondaSystemExit("Exiting.")
    return True


def ensure_name_or_prefix(args, command):
    if not (args.name or args.prefix):
        from ..exceptions import CondaValueError
        raise CondaValueError('either -n NAME or -p PREFIX option required,\n'
                              'try "conda %s -h" for more details' % command)


def arg2spec(arg, json=False, update=False):
    try:
        # spec_from_line can return None, especially for the case of a .tar.bz2 extension and
        #   a space in the path
        _arg = spec_from_line(arg)
        if _arg is None and arg.endswith(CONDA_TARBALL_EXTENSION):
            _arg = arg
        from ..resolve import MatchSpec
        spec = MatchSpec(_arg, normalize=True)
    except:
        from ..exceptions import CondaValueError
        raise CondaValueError('invalid package specification: %s' % arg)

    name = spec.name
    if name in context.disallow:
        from ..exceptions import CondaValueError
        raise CondaValueError("specification '%s' is disallowed" % name)

    if not spec.is_simple() and update:
        from ..exceptions import CondaValueError
        raise CondaValueError("""version specifications not allowed with 'update'; use
    conda update  %s%s  or
    conda install %s""" % (name, ' ' * (len(arg) - len(name)), arg))

    return str(spec)


def specs_from_args(args, json=False):
    return [arg2spec(arg, json=json) for arg in args]


spec_pat = re.compile(r'''
(?P<name>[^=<>!\s]+)               # package name
\s*                                # ignore spaces
(
  (?P<cc>=[^=]+(=[^=]+)?)          # conda constraint
  |
  (?P<pc>(?:[=!]=|[><]=?).+)       # new (pip-style) constraint(s)
)?
$                                  # end-of-line
''', re.VERBOSE)


def strip_comment(line):
    return line.split('#')[0].rstrip()


def spec_from_line(line):
    m = spec_pat.match(strip_comment(line))
    if m is None:
        return None
    name, cc, pc = (m.group('name').lower(), m.group('cc'), m.group('pc'))
    if cc:
        return name + cc.replace('=', ' ')
    elif pc:
        return name + ' ' + pc.replace(' ', '')
    else:
        return name


def specs_from_url(url, json=False):
    from ..gateways.connection.download import TmpDownload

    explicit = False
    with TmpDownload(url, verbose=False) as path:
        specs = []
        try:
            for line in open(path):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if line == '@EXPLICIT':
                    explicit = True
                if explicit:
                    specs.append(line)
                    continue
                spec = spec_from_line(line)
                if spec is None:
                    from ..exceptions import CondaValueError
                    raise CondaValueError("could not parse '%s' in: %s" %
                                          (line, url))
                specs.append(spec)
        except IOError as e:
            from ..exceptions import CondaFileIOError
            raise CondaFileIOError(path, e)
    return specs


def names_in_specs(names, specs):
    return any(spec.split()[0] in names for spec in specs)


def disp_features(features):
    if features:
        return '[%s]' % ' '.join(features)
    else:
        return ''


@swallow_broken_pipe
def stdout_json(d):
    print(json_dump(d))


def stdout_json_success(success=True, **kwargs):
    from ..models.dist import Dist
    result = {'success': success}
    actions = kwargs.pop('actions', None)
    if actions:
        if 'LINK' in actions:
            actions['LINK'] = [Dist(prec) for prec in actions['LINK']]
        if 'UNLINK' in actions:
            actions['UNLINK'] = [Dist(prec) for prec in actions['UNLINK']]
        result['actions'] = actions
    result.update(kwargs)
    stdout_json(result)


def print_envs_list(known_conda_prefixes, output=True):

    if output:
        print("# conda environments:")
        print("#")

    def disp_env(prefix):
        fmt = '%-20s  %s  %s'
        default = '*' if prefix == context.default_prefix else ' '
        if prefix == context.root_prefix:
            name = ROOT_ENV_NAME
        elif any(paths_equal(envs_dir, dirname(prefix)) for envs_dir in context.envs_dirs):
            name = basename(prefix)
        else:
            name = ''
        if output:
            print(fmt % (name, default, prefix))

    for prefix in known_conda_prefixes:
        disp_env(prefix)

    if output:
        print()


def get_private_envs_json():
    path_to_private_envs = join(context.root_prefix, "conda-meta", "private_envs")
    if not isfile(path_to_private_envs):
        return None
    try:
        with open(path_to_private_envs, "r") as f:
            private_envs_json = json.load(f)
    except json.decoder.JSONDecodeError:
        private_envs_json = {}
    return private_envs_json


def prefix_if_in_private_env(spec):
    private_envs_json = get_private_envs_json()
    if not private_envs_json:
        return None
    prefixes = tuple(prefix for pkg, prefix in iteritems(private_envs_json) if
                     pkg.startswith(spec))
    prefix = prefixes[0] if len(prefixes) > 0 else None
    return prefix


def pkg_if_in_private_env(spec):
    private_envs_json = get_private_envs_json()
    pkgs = tuple(pkg for pkg, prefix in iteritems(private_envs_json) if pkg.startswith(spec))
    pkg = pkgs[0] if len(pkgs) > 0 else None
    return pkg


def create_prefix_spec_map_with_deps(r, specs, default_prefix):
    from ..common.path import is_private_env, prefix_to_env_name
    from ..core.linked_data import linked_data
    prefix_spec_map = {}
    for spec in specs:
        spec_prefix = prefix_if_in_private_env(spec)
        spec_prefix = spec_prefix if spec_prefix is not None else default_prefix
        if spec_prefix in prefix_spec_map.keys():
            prefix_spec_map[spec_prefix].add(spec)
        else:
            prefix_spec_map[spec_prefix] = {spec}

        if is_private_env(prefix_to_env_name(spec_prefix, context.root_prefix)):
            linked = linked_data(spec_prefix)
            for linked_spec in linked:
                if not linked_spec.name.startswith(spec) and r.depends_on(spec, linked_spec):
                    prefix_spec_map[spec_prefix].add(linked_spec.name)
    return prefix_spec_map


def check_non_admin():
    from ..common.platform import is_admin
    if not context.non_admin_enabled and not is_admin():
        from ..exceptions import OperationNotAllowed
        raise OperationNotAllowed(dals("""
            The create, install, update, and remove operations have been disabled
            on your system for non-privileged users.
        """))
