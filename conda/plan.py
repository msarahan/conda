"""
Handle the planning of installs and their execution.

NOTE:
    conda.install uses canonical package names in its interface functions,
    whereas conda.resolve uses package filenames, as those are used as index
    keys.  We try to keep fixes to this "impedance mismatch" local to this
    module.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

from collections import defaultdict
from copy import copy
from logging import getLogger
from os.path import abspath, basename, exists, join
import sys

from ._vendor.boltons.setutils import IndexedSet
from .base.constants import DEFAULTS_CHANNEL_NAME, UNKNOWN_CHANNEL
from .base.context import context
from .cli import common
from .cli.common import pkg_if_in_private_env, prefix_if_in_private_env
from .common.compat import iterkeys, odict, on_win
from .common.path import (is_private_env, preferred_env_matches_prefix,
                          preferred_env_to_prefix, prefix_to_env_name)
from .core.index import _supplement_index_with_prefix
from .core.link import UnlinkLinkTransaction, UnlinkLinkTransactionSetup
from .core.linked_data import is_linked, linked_data
from .core.package_cache import ProgressiveFetchExtract
from .exceptions import (ArgumentError, CondaIndexError, CondaRuntimeError, InstallError,
                         RemoveError)
from .gateways.disk.test import prefix_is_writable
from .history import History
from .instructions import (ACTION_CODES, CHECK_EXTRACT, CHECK_FETCH, EXTRACT, FETCH, LINK, PREFIX,
                           PRINT, PROGRESS, PROGRESSIVEFETCHEXTRACT, PROGRESS_COMMANDS,
                           RM_EXTRACTED, RM_FETCHED, SYMLINK_CONDA, UNLINK,
                           UNLINKLINKTRANSACTION, execute_instructions)
from .models.channel import Channel
from .models.dist import Dist
from .models.enums import LinkType
from .models.version import normalized_version
from .resolve import MatchSpec, Resolve
from .utils import human_bytes

try:
    from cytoolz.itertoolz import concat, concatv, groupby, remove
except ImportError:
    from ._vendor.toolz.itertoolz import concat, concatv, groupby, remove  # NOQA

log = getLogger(__name__)


def print_dists(dists_extras):
    fmt = "    %-27s|%17s"
    print(fmt % ('package', 'build'))
    print(fmt % ('-' * 27, '-' * 17))
    for dist, extra in dists_extras:
        name, version, build, _ = dist.quad
        line = fmt % (name + '-' + version, build)
        if extra:
            line += extra
        print(line)


def display_actions(actions, index, show_channel_urls=None):
    prefix = actions.get("PREFIX")
    if prefix:
        print("Package plan for environment '%s':" % prefix)

    if show_channel_urls is None:
        show_channel_urls = context.show_channel_urls

    def channel_str(rec):
        if rec.get('schannel'):
            return rec['schannel']
        if rec.get('url'):
            return Channel(rec['url']).canonical_name
        if rec.get('channel'):
            return Channel(rec['channel']).canonical_name
        return UNKNOWN_CHANNEL

    def channel_filt(s):
        if show_channel_urls is False:
            return ''
        if show_channel_urls is None and s == DEFAULTS_CHANNEL_NAME:
            return ''
        return s

    if actions.get(FETCH):
        print("\nThe following packages will be downloaded:\n")

        disp_lst = []
        for dist in actions[FETCH]:
            dist = Dist(dist)
            info = index[dist]
            extra = '%15s' % human_bytes(info['size'])
            schannel = channel_filt(channel_str(info))
            if schannel:
                extra += '  ' + schannel
            disp_lst.append((dist, extra))
        print_dists(disp_lst)

        if index and len(actions[FETCH]) > 1:
            num_bytes = sum(index[Dist(dist)]['size'] for dist in actions[FETCH])
            print(' ' * 4 + '-' * 60)
            print(" " * 43 + "Total: %14s" % human_bytes(num_bytes))

    # package -> [oldver-oldbuild, newver-newbuild]
    packages = defaultdict(lambda: list(('', '')))
    features = defaultdict(lambda: list(('', '')))
    channels = defaultdict(lambda: list(('', '')))
    records = defaultdict(lambda: list((None, None)))
    linktypes = {}

    for arg in actions.get(LINK, []):
        dist = Dist(arg)
        rec = index[dist]
        pkg = rec['name']
        channels[pkg][1] = channel_str(rec)
        packages[pkg][1] = rec['version'] + '-' + rec['build']
        records[pkg][1] = rec
        linktypes[pkg] = LinkType.hardlink  # TODO: this is a lie; may have to give this report after UnlinkLinkTransaction.verify()  # NOQA
        features[pkg][1] = rec.get('features', '')
    for arg in actions.get(UNLINK, []):
        dist = Dist(arg)
        rec = index[dist]
        pkg = rec['name']
        channels[pkg][0] = channel_str(rec)
        packages[pkg][0] = rec['version'] + '-' + rec['build']
        records[pkg][0] = rec
        features[pkg][0] = rec.get('features', '')

    new = {p for p in packages if not packages[p][0]}
    removed = {p for p in packages if not packages[p][1]}
    # New packages are actually listed in the left-hand column,
    # so let's move them over there
    for pkg in new:
        for var in (packages, features, channels, records):
            var[pkg] = var[pkg][::-1]

    empty = False
    if packages:
        maxpkg = max(len(p) for p in packages) + 1
        maxoldver = max(len(p[0]) for p in packages.values())
        maxnewver = max(len(p[1]) for p in packages.values())
        maxoldfeatures = max(len(p[0]) for p in features.values())
        maxnewfeatures = max(len(p[1]) for p in features.values())
        maxoldchannels = max(len(channel_filt(p[0])) for p in channels.values())
        maxnewchannels = max(len(channel_filt(p[1])) for p in channels.values())
    else:
        empty = True

    updated = set()
    downgraded = set()
    channeled = set()
    oldfmt = {}
    newfmt = {}
    for pkg in packages:
        # That's right. I'm using old-style string formatting to generate a
        # string with new-style string formatting.
        oldfmt[pkg] = '{pkg:<%s} {vers[0]:<%s}' % (maxpkg, maxoldver)
        if maxoldchannels:
            oldfmt[pkg] += ' {channels[0]:<%s}' % maxoldchannels
        if features[pkg][0]:
            oldfmt[pkg] += ' [{features[0]:<%s}]' % maxoldfeatures

        lt = LinkType(linktypes.get(pkg, LinkType.hardlink))
        lt = '' if lt == LinkType.hardlink else (' (%s)' % lt)
        if pkg in removed or pkg in new:
            oldfmt[pkg] += lt
            continue

        newfmt[pkg] = '{vers[1]:<%s}' % maxnewver
        if maxnewchannels:
            newfmt[pkg] += ' {channels[1]:<%s}' % maxnewchannels
        if features[pkg][1]:
            newfmt[pkg] += ' [{features[1]:<%s}]' % maxnewfeatures
        newfmt[pkg] += lt

        P0 = records[pkg][0]
        P1 = records[pkg][1]
        pri0 = P0.get('priority')
        pri1 = P1.get('priority')
        if pri0 is None or pri1 is None:
            pri0 = pri1 = 1
        try:
            if str(P1.version) == 'custom':
                newver = str(P0.version) != 'custom'
                oldver = not newver
            else:
                # <= here means that unchanged packages will be put in updated
                N0 = normalized_version(P0.version)
                N1 = normalized_version(P1.version)
                newver = N0 < N1
                oldver = N0 > N1
        except TypeError:
            newver = P0.version < P1.version
            oldver = P0.version > P1.version
        oldbld = P0.build_number > P1.build_number
        newbld = P0.build_number < P1.build_number
        if context.channel_priority and pri1 < pri0 and (oldver or not newver and not newbld):
            channeled.add(pkg)
        elif newver:
            updated.add(pkg)
        elif pri1 < pri0 and (oldver or not newver and oldbld):
            channeled.add(pkg)
        elif oldver:
            downgraded.add(pkg)
        elif not oldbld:
            updated.add(pkg)
        else:
            downgraded.add(pkg)

    arrow = ' --> '
    lead = ' ' * 4

    def format(s, pkg):
        chans = [channel_filt(c) for c in channels[pkg]]
        return lead + s.format(pkg=pkg + ':', vers=packages[pkg],
                               channels=chans, features=features[pkg])

    if new:
        print("\nThe following NEW packages will be INSTALLED:\n")
        for pkg in sorted(new):
            # New packages have been moved to the "old" column for display
            print(format(oldfmt[pkg], pkg))

    if removed:
        print("\nThe following packages will be REMOVED:\n")
        for pkg in sorted(removed):
            print(format(oldfmt[pkg], pkg))

    if updated:
        print("\nThe following packages will be UPDATED:\n")
        for pkg in sorted(updated):
            print(format(oldfmt[pkg] + arrow + newfmt[pkg], pkg))

    if channeled:
        print("\nThe following packages will be SUPERSEDED by a higher-priority channel:\n")
        for pkg in sorted(channeled):
            print(format(oldfmt[pkg] + arrow + newfmt[pkg], pkg))

    if downgraded:
        print("\nThe following packages will be DOWNGRADED due to dependency conflicts:\n")
        for pkg in sorted(downgraded):
            print(format(oldfmt[pkg] + arrow + newfmt[pkg], pkg))

    if empty and actions.get(SYMLINK_CONDA):
        print("\nThe following empty environments will be CREATED:\n")
        print(actions['PREFIX'])

    print()


def nothing_to_do(actions):
    for op in ACTION_CODES:
        if actions.get(op):
            return False
    return True


def add_unlink(actions, dist):
    assert isinstance(dist, Dist)
    if UNLINK not in actions:
        actions[UNLINK] = []
    actions[UNLINK].append(dist)


def handle_menuinst(unlink_dists, link_dists):
    if not on_win:
        return unlink_dists, link_dists

    # Always link/unlink menuinst first/last on windows in case a subsequent
    # package tries to import it to create/remove a shortcut

    # unlink
    menuinst_idx = next((q for q, d in enumerate(unlink_dists) if d.name == 'menuinst'), None)
    if menuinst_idx is not None:
        unlink_dists = tuple(concatv(
            unlink_dists[:menuinst_idx],
            unlink_dists[menuinst_idx+1:],
            unlink_dists[menuinst_idx:menuinst_idx+1],
        ))

    # link
    menuinst_idx = next((q for q, d in enumerate(link_dists) if d.name == 'menuinst'), None)
    if menuinst_idx is not None:
        link_dists = tuple(concatv(
            link_dists[menuinst_idx:menuinst_idx+1],
            link_dists[:menuinst_idx],
            link_dists[menuinst_idx+1:],
        ))

    return unlink_dists, link_dists


def inject_UNLINKLINKTRANSACTION(plan, index, prefix, axn, specs):
    # TODO: we really shouldn't be mutating the plan list here; turn plan into a tuple
    first_unlink_link_idx = next((q for q, p in enumerate(plan) if p[0] in (UNLINK, LINK)), -1)
    if first_unlink_link_idx >= 0:
        grouped_instructions = groupby(lambda x: x[0], plan)
        unlink_dists = tuple(Dist(d[1]) for d in grouped_instructions.get(UNLINK, ()))
        link_dists = tuple(Dist(d[1]) for d in grouped_instructions.get(LINK, ()))
        unlink_dists, link_dists = handle_menuinst(unlink_dists, link_dists)

        # TODO: ideally we'd move these two lines before both the y/n confirmation and the --dry-run exit  # NOQA
        pfe = ProgressiveFetchExtract(index, link_dists)
        pfe.prepare()

        stp = UnlinkLinkTransactionSetup(index, prefix, unlink_dists, link_dists, axn, specs)
        plan.insert(first_unlink_link_idx, (UNLINKLINKTRANSACTION, UnlinkLinkTransaction(stp)))
        plan.insert(first_unlink_link_idx, (PROGRESSIVEFETCHEXTRACT, pfe))
    elif axn in ('INSTALL', 'CREATE'):
        plan.insert(0, (UNLINKLINKTRANSACTION, (prefix, (), (), axn, specs)))

    return plan


def plan_from_actions(actions, index):
    if 'op_order' in actions and actions['op_order']:
        op_order = actions['op_order']
    else:
        op_order = ACTION_CODES

    assert PREFIX in actions and actions[PREFIX]
    prefix = actions[PREFIX]
    plan = [('PREFIX', '%s' % prefix)]

    unlink_link_transaction = actions.get('UNLINKLINKTRANSACTION')
    if unlink_link_transaction:
        progressive_fetch_extract = actions.get('PROGRESSIVEFETCHEXTRACT')
        if progressive_fetch_extract:
            plan.append((PROGRESSIVEFETCHEXTRACT, progressive_fetch_extract))
        plan.append((UNLINKLINKTRANSACTION, unlink_link_transaction))
        return plan

    axn = actions.get('ACTION') or None
    specs = actions.get('SPECS', [])

    log.debug("Adding plans for operations: {0}".format(op_order))
    for op in op_order:
        if op not in actions:
            log.trace("action {0} not in actions".format(op))
            continue
        if not actions[op]:
            log.trace("action {0} has None value".format(op))
            continue
        if '_' not in op:
            plan.append((PRINT, '%sing packages ...' % op.capitalize()))
        elif op.startswith('RM_'):
            plan.append((PRINT, 'Pruning %s packages from the cache ...' % op[3:].lower()))
        if op in PROGRESS_COMMANDS:
            plan.append((PROGRESS, '%d' % len(actions[op])))
        for arg in actions[op]:
            log.debug("appending value {0} for action {1}".format(arg, op))
            plan.append((op, arg))

    plan = inject_UNLINKLINKTRANSACTION(plan, index, prefix, axn, specs)

    return plan


# force_linked_actions has now been folded into this function, and is enabled by
# supplying an index and setting force=True
def ensure_linked_actions(dists, prefix, index=None, force=False,
                          always_copy=False):
    assert all(isinstance(d, Dist) for d in dists)
    actions = defaultdict(list)
    actions[PREFIX] = prefix
    actions['op_order'] = (CHECK_FETCH, RM_FETCHED, FETCH, CHECK_EXTRACT,
                           RM_EXTRACTED, EXTRACT,
                           UNLINK, LINK, SYMLINK_CONDA)

    for dist in dists:
        if not force and is_linked(prefix, dist):
            continue
        actions[LINK].append(dist)
    return actions


def get_blank_actions(prefix):
    actions = defaultdict(list)
    actions[PREFIX] = prefix
    actions['op_order'] = (CHECK_FETCH, RM_FETCHED, FETCH, CHECK_EXTRACT,
                           RM_EXTRACTED, EXTRACT,
                           UNLINK, LINK, SYMLINK_CONDA)
    return actions


# -------------------------------------------------------------------


def is_root_prefix(prefix):
    return abspath(prefix) == abspath(context.root_prefix)


def add_defaults_to_specs(r, linked, specs, update=False, prefix=None):
    # TODO: This should use the pinning mechanism. But don't change the API because cas uses it
    if r.explicit(specs) or is_private_env_path(prefix):
        return
    log.debug('H0 specs=%r' % specs)
    names_linked = {r.package_name(d): d for d in linked if d in r.index}
    mspecs = list(map(MatchSpec, specs))

    for name, def_ver in [('python', context.default_python or None),
                          # Default version required, but only used for Python
                          ('lua', None)]:
        if any(s.name == name and not s.is_simple() for s in mspecs):
            # if any of the specifications mention the Python/Numpy version,
            # we don't need to add the default spec
            log.debug('H1 %s' % name)
            continue

        depends_on = {s for s in mspecs if r.depends_on(s, name)}
        any_depends_on = bool(depends_on)
        log.debug('H2 %s %s' % (name, any_depends_on))

        if not any_depends_on:
            # if nothing depends on Python/Numpy AND the Python/Numpy is not
            # specified, we don't need to add the default spec
            log.debug('H2A %s' % name)
            continue

        if any(s.exact_field('build') for s in depends_on):
            # If something depends on Python/Numpy, but the spec is very
            # explicit, we also don't need to add the default spec
            log.debug('H2B %s' % name)
            continue

        if name in names_linked:
            # if Python/Numpy is already linked, we add that instead of the default
            log.debug('H3 %s' % name)
            dist = Dist(names_linked[name])
            info = r.index[dist]
            ver = '.'.join(info['version'].split('.', 2)[:2])
            spec = '%s %s* (target=%s)' % (info['name'], ver, dist)
            specs.append(spec)
            continue

        if name == 'python' and def_ver and def_ver.startswith('3.'):
            # Don't include Python 3 in the specs if this is the Python 3
            # version of conda.
            continue

        if def_ver is not None:
            specs.append('%s %s*' % (name, def_ver))
    log.debug('HF specs=%r' % specs)


def get_pinned_specs(prefix):
    pinfile = join(prefix, 'conda-meta', 'pinned')
    if exists(pinfile):
        with open(pinfile) as f:
            from_file = (i for i in f.read().strip().splitlines()
                         if i and not i.strip().startswith('#'))
    else:
        from_file = ()

    from .cli.common import spec_from_line

    def munge_spec(s):
        return s if ' ' in s else spec_from_line(s)

    return tuple(munge_spec(s) for s in concatv(context.pinned_packages, from_file))


# def install_actions(prefix, index, specs, force=False, only_names=None, always_copy=False,
#                     pinned=True, minimal_hint=False, update_deps=True, prune=False,
#                     channel_priority_map=None, is_update=False):  # pragma: no cover
#     """
#     This function ignores all preferred_env preference.
#     """
#     # type: (str, Dict[Dist, Record], List[str], bool, Option[List[str]], bool, bool, bool,
#     #        bool, bool, bool, Dict[str, Sequence[str, int]]) -> Dict[weird]
#
#     r = get_resolve_object(index.copy(), prefix)
#     str_specs = specs
#
#     specs_for_prefix = SpecsForPrefix(prefix=prefix, specs=tuple(str_specs), r=r)
#
#     # TODO: Don't we need add_defaults_to_sepcs here?
#
#     actions = get_actions_for_dists(specs_for_prefix, only_names, index, force, always_copy,
#                                     prune, update_deps, pinned)
#     actions['SPECS'].extend(str_specs)
#     actions['ACTION'] = 'INSTALL'
#     return actions


def install_actions(prefix, index, specs, force=False, only_names=None, always_copy=False,
                    pinned=True, minimal_hint=False, update_deps=True, prune=False,
                    channel_priority_map=None, is_update=False):  # pragma: no cover
    # this is for conda-build
    txn = install_transaction(prefix, index, specs, force, only_names, always_copy,
                              pinned, minimal_hint, update_deps, prune,
                              channel_priority_map, is_update)

    pfe = txn.get_pfe()
    return {
        'PREFIX': prefix,
        'PROGRESSIVEFETCHEXTRACT': pfe,
        'UNLINKLINKTRANSACTION': txn,
    }


def install_transaction(prefix, index, specs, force=False, only_names=None, always_copy=False,
                        pinned=True, minimal_hint=False, update_deps=True, prune=False,
                        channel_priority_map=None, is_update=False):
    specs = set(MatchSpec(s) for s in specs)
    r = get_resolve_object(index.copy(), prefix)
    unlink_dists, link_dists = solve_for_actions(prefix, r, specs_to_add=specs, prune=prune)

    stp = UnlinkLinkTransactionSetup(r.index, prefix, unlink_dists, link_dists, 'INSTALL',
                                     tuple(s.spec for s in specs))
    txn = UnlinkLinkTransaction(stp)
    return txn


def install_actions_list(prefix, index, spec_strs, force=False, only_names=None, always_copy=False,
                         pinned=True, minimal_hint=False, update_deps=True, prune=False,
                         channel_priority_map=None, is_update=False):
    # type: (str, Dict[Dist, Record], List[str], bool, Option[List[str]], bool, bool, bool,
    #        bool, bool, bool, Dict[str, Sequence[str, int]]) -> List[Dict[weird]]

    # split out specs into potentially multiple preferred envs if:
    #  1. the user default env (root_prefix) is the prefix being considered here
    #  2. the user has not specified the --name or --prefix command-line flags
    if (prefix == context.root_prefix
            and not context.prefix_specified
            and prefix_is_writable(prefix)
            and context.enable_private_envs):

        # a registered package CANNOT be installed in the root env
        # if ANY package requesting a private env is required in the root env, all packages for
        #   that requested env must instead be installed in the root env

        root_r = get_resolve_object(index.copy(), context.root_prefix)

        def get_env_for_spec(spec):
            # use resolve's get_dists_for_spec() to find the "best" matching record
            record_for_spec = root_r.index[root_r.get_dists_for_spec(spec, emptyok=False)[-1]]
            return ensure_pad(record_for_spec.preferred_env)

        # specs grouped by target env, the 'None' key holds the specs for the root env
        env_add_map = groupby(get_env_for_spec, (MatchSpec(s) for s in spec_strs))
        requested_root_specs_to_add = {s for s in env_add_map.pop(None, ())}

        ed = EnvsDirectory(join(context.root_prefix, 'envs'))
        registered_packages = ed.get_registered_packages_keyed_on_env_name()

        if len(env_add_map) == len(registered_packages) == 0:
            # short-circuit the rest of this logic
            return install_transaction(prefix, index, spec_strs, force, only_names, always_copy,
                                       pinned, minimal_hint, update_deps, prune,
                                       channel_priority_map, is_update)

        root_specs_to_remove = set(MatchSpec(s.name) for s in concat(itervalues(env_add_map)))
        required_root_dists, _ = solve_prefix(context.root_prefix, root_r,
                                              specs_to_remove=root_specs_to_remove,
                                              specs_to_add=requested_root_specs_to_add,
                                              prune=True)

        required_root_package_names = tuple(d.name for d in required_root_dists)

        # first handle pulling back requested specs to root
        forced_root_specs_to_add = set()
        pruned_env_add_map = defaultdict(list)
        for env_name, specs in iteritems(env_add_map):
            for spec in specs:
                spec_name = MatchSpec(spec).name
                if spec_name in required_root_package_names:
                    forced_root_specs_to_add.add(spec)
                else:
                    pruned_env_add_map[env_name].append(spec)
        env_add_map = pruned_env_add_map

        # second handle pulling back registered specs to root
        env_remove_map = defaultdict(list)
        for env_name, registered_package_entries in iteritems(registered_packages):
            for rpe in registered_package_entries:
                if rpe['package_name'] in required_root_package_names:
                    # ANY registered packages in this environment need to be pulled back
                    for pe in registered_package_entries:
                        # add an entry in env_remove_map
                        # add an entry in forced_root_specs_to_add
                        pname = pe['package_name']
                        env_remove_map[env_name].append(MatchSpec(pname))
                        forced_root_specs_to_add.add(MatchSpec(pe['requested_spec']))
                break

        unlink_link_map = odict()

        # solve all neede preferred_env prefixes
        for env_name in set(concatv(env_add_map, env_remove_map)):
            specs_to_add = env_add_map[env_name]
            spec_to_remove = env_remove_map[env_name]
            pfx = ed.preferred_env_to_prefix(env_name)
            unlink, link = solve_for_actions(pfx, get_resolve_object(index.copy(), pfx),
                                             specs_to_remove=spec_to_remove,
                                             specs_to_add=specs_to_add,
                                             prune=True)
            unlink_link_map[env_name] = unlink, link, specs_to_add

        # now solve root prefix
        # we have to solve root a second time in all cases, because this time we don't prune
        root_specs_to_add = set(concatv(requested_root_specs_to_add, forced_root_specs_to_add))
        root_unlink, root_link = solve_for_actions(context.root_prefix, root_r,
                                                   specs_to_remove=root_specs_to_remove,
                                                   specs_to_add=root_specs_to_add)
        if root_unlink or root_link:
            # this needs to be added to odict last; the private envs need to be updated first
            unlink_link_map[None] = root_unlink, root_link, root_specs_to_add

        # def make_actions(pfx, unlink, link, specs):
        #     actions = get_blank_actions(pfx)
        #     actions['UNLINK'].extend(unlink)
        #     actions['LINK'].extend(link)
        #     actions['SPECS'].extend(s.spec for s in specs)
        #     actions['ACTION'] = 'INSTALL'
        #     return actions
        #
        # action_groups = [make_actions(ed.to_prefix(ensure_pad(env_name)), *oink)
        #                  for env_name, oink in iteritems(unlink_link_map)]
        # return action_groups

        def make_txn_setup(pfx, unlink, link, specs):
            # TODO: this index here is probably wrong; needs to be per-prefix
            return UnlinkLinkTransactionSetup(index, pfx, unlink, link, 'INSTALL',
                                              tuple(s.spec for s in specs))

        txn_args = tuple(make_txn_setup(ed.to_prefix(ensure_pad(env_name)), *oink)
                         for env_name, oink in iteritems(unlink_link_map))
        txn = UnlinkLinkTransaction(*txn_args)
        return txn

    # Need to add unlink actions if updating a private env from root
    if is_update and prefix == context.root_prefix:
        add_unlink_options_for_update(actions, required_solves, index)

    return actions


def add_unlink_options_for_update(actions, required_solves, index):
    # type: (Dict[weird], List[SpecsForPrefix], List[weird]) -> ()
    get_action_for_prefix = lambda prfx: tuple(actn for actn in actions if actn["PREFIX"] == prfx)
    linked_in_prefix = linked_data(context.root_prefix)
    spec_in_root = lambda spc: tuple(
        mtch for mtch in iterkeys(linked_in_prefix) if MatchSpec(spc).match(index[mtch]))
    for solved in required_solves:
        # If the solved prefix is private
        if is_private_env(prefix_to_env_name(solved.prefix, context.root_prefix)):
            for spec in solved.specs:
                matched_in_root = spec_in_root(spec)
                if matched_in_root:
                    aug_action = get_action_for_prefix(context.root_prefix)
                    if len(aug_action) > 0:
                        add_unlink(aug_action[0], matched_in_root[0])
                    else:
                        actions.append(remove_actions(context.root_prefix, matched_in_root, index))
        # If the solved prefix is root
        elif preferred_env_matches_prefix(None, solved.prefix, context.root_prefix):
            for spec in solved.specs:
                spec_in_private_env = prefix_if_in_private_env(spec)
                if spec_in_private_env:
                    # remove pkg from private env and install in root
                    aug_action = get_action_for_prefix(spec_in_private_env)
                    if len(aug_action) > 0:
                        add_unlink(aug_action[0], Dist(pkg_if_in_private_env(spec)))
                    else:
                        actions.append(remove_spec_action_from_prefix(
                            spec_in_private_env, Dist(pkg_if_in_private_env(spec))))


def get_resolve_object(index, prefix):
    # instantiate resolve object
    _supplement_index_with_prefix(index, prefix, {})
    r = Resolve(index)
    return r


def solve_prefix(prefix, r, specs_to_remove=(), specs_to_add=(), prune=False):
    # this function gives a "final state" for an existing prefix given just these simple inputs
    prune = context.prune or prune
    log.debug("solving prefix %s\n"
              "  specs_to_remove: %s\n"
              "  specs_to_add: %s\n"
              "  prune: %s", prefix, specs_to_remove, specs_to_add, prune)

    # declare starting point
    solved_linked_dists = () if prune else tuple(iterkeys(linked_data(prefix)))
    # TODO: to change this whole function from working with dists to working with records, just
    #       change iterkeys to itervalues

    if solved_linked_dists and specs_to_remove:
        solved_linked_dists = r.remove(tuple(text_type(s) for s in specs_to_remove),
                                       solved_linked_dists)

    # add in specs from requested history,
    #   but not if we're requesting removal in this operation
    spec_names_to_remove = set(s.name for s in specs_to_remove)
    user_requested_specs = History(prefix).get_requested_specs()
    log.debug("user requested specs: %s", user_requested_specs)
    specs_map = {s.name: s for s in user_requested_specs if s.name not in spec_names_to_remove}

    # replace specs matching same name with new specs_to_add
    specs_map.update({s.name: s for s in specs_to_add})
    specs_to_add = itervalues(specs_map)

    specs_to_add = augment_specs(prefix, specs_to_add)
    solved_linked_dists = r.install(specs_to_add,
                                    solved_linked_dists,
                                    update_deps=context.update_dependencies)

    if context.respect_pinned:
        # TODO: assert all pinned specs are compatible with what's in solved_linked_dists
        pass

    # TODO: don't uninstall conda or its dependencies, probably need to check elsewhere

    solved_linked_dists = IndexedSet(r.dependency_sort({d.name: d for d in solved_linked_dists}))

    log.debug("solved prefix %s\n"
              "  solved_linked_dists:\n"
              "    %s\n",
              prefix, "\n    ".join(text_type(d) for d in solved_linked_dists))

    return solved_linked_dists, specs_to_add


def sort_unlink_link_from_solve(prefix, solved_dists, remove_satisfied_specs):
    # solved_dists should be the return value of solve_prefix()
    old_linked_dists = IndexedSet(iterkeys(linked_data(prefix)))

    dists_for_unlinking = old_linked_dists - solved_dists
    dists_for_linking = solved_dists - old_linked_dists

    # TODO: back 'noarch: python' to unlink and link if python version changes

    # r_linked = Resolve(linked_data(prefix))
    # for spec in remove_satisfied_specs:
    #     if r_linked.find_matches(spec):
    #         spec_name = spec.name
    #         unlink_dist = next((d for d in dists_for_unlinking if d.name == spec_name), None)
    #         link_dist = next((d for d in dists_for_linking if d.name == spec_name), None)
    #         if unlink_dist:
    #             dists_for_unlinking.discard(unlink_dist)
    #         if link_dist:
    #             dists_for_linking.discard(link_dist)

    return dists_for_unlinking, dists_for_linking


def forced_reinstall_specs(prefix, solved_dists, dists_for_unlinking, dists_for_linking,
                           specs_to_add):
    _dists_for_unlinking, _dists_for_linking = copy(dists_for_unlinking), copy(dists_for_linking)
    old_linked_dists = IndexedSet(iterkeys(linked_data(prefix)))

    # re-install any specs_to_add
    def find_first(dists, package_name):
        return next((d for d in dists if d.name == package_name), None)

    for spec in specs_to_add:
        spec_name = MatchSpec(spec).name
        old_dist_with_same_name = find_first(old_linked_dists, spec_name)
        if old_dist_with_same_name:
            _dists_for_unlinking.add(old_dist_with_same_name)

        new_dist_with_same_name = find_first(solved_dists, spec_name)
        if new_dist_with_same_name:
            _dists_for_linking.add(new_dist_with_same_name)

    return _dists_for_unlinking, _dists_for_linking


def solve_for_actions(prefix, r, specs_to_remove=(), specs_to_add=(), prune=False):
    # this is not for force-removing packages, which doesn't invoke the solver

    solved_dists, _specs_to_add = solve_prefix(prefix, r, specs_to_remove, specs_to_add, prune)
    dists_for_unlinking, dists_for_linking = sort_unlink_link_from_solve(prefix, solved_dists,
                                                                         _specs_to_add)
    # TODO: this _specs_to_add part should be refactored when we can better pin package channel origin  # NOQA

    if context.force:
        dists_for_unlinking, dists_for_linking = forced_reinstall_specs(prefix, solved_dists,
                                                                        dists_for_unlinking,
                                                                        dists_for_linking,
                                                                        specs_to_add)

    # actions = get_blank_actions(prefix)
    # actions['UNLINK'].extend(reversed(dists_for_unlinking))
    # actions['LINK'].extend(dists_for_linking)
    # return actions
    dists_for_unlinking = IndexedSet(reversed(dists_for_unlinking))
    return dists_for_unlinking, dists_for_linking


# def get_actions_for_dists(dists_for_prefix, only_names, index, force, always_copy, prune,
#                           update_deps, pinned):
#     root_only = ('conda', 'conda-env')
#     prefix = dists_for_prefix.prefix
#     dists = dists_for_prefix.specs
#     r = dists_for_prefix.r
#     specs = [MatchSpec(dist) for dist in dists]
#     specs = augment_specs(prefix, specs, pinned)
#
#     linked = linked_data(prefix)
#     must_have = odict()
#
#     installed = linked
#     if prune:
#         installed = []
#     pkgs = r.install(specs, installed, update_deps=update_deps)
#
#     for fn in pkgs:
#         dist = Dist(fn)
#         name = r.package_name(dist)
#         if not name or only_names and name not in only_names:
#             continue
#         must_have[name] = dist
#
#     if is_root_prefix(prefix):
#         # for name in foreign:
#         #     if name in must_have:
#         #         del must_have[name]
#         pass
#     elif basename(prefix).startswith('_'):
#         # anything (including conda) can be installed into environments
#         # starting with '_', mainly to allow conda-build to build conda
#         pass
#
#     elif any(s in must_have for s in root_only):
#         # the solver scheduled an install of conda, but it wasn't in the
#         # specs, so it must have been a dependency.
#         specs = [s for s in specs if r.depends_on(s, root_only)]
#         if specs:
#             raise InstallError("""\
# Error: the following specs depend on 'conda' and can only be installed
# into the root environment: %s""" % (' '.join(spec.name for spec in specs),))
#         linked = [r.package_name(s) for s in linked]
#         linked = [s for s in linked if r.depends_on(s, root_only)]
#         if linked:
#             raise InstallError("""\
# Error: one or more of the packages already installed depend on 'conda'
# and should only be installed in the root environment: %s
# These packages need to be removed before conda can proceed.""" % (' '.join(linked),))
#         raise InstallError("Error: 'conda' can only be installed into the "
#                            "root environment")
#
#     smh = r.dependency_sort(must_have)
#     actions = ensure_linked_actions(
#         smh, prefix,
#         index=r.index,
#         force=force)
#
#     if actions[LINK]:
#         actions[SYMLINK_CONDA] = [context.root_prefix]
#
#     for dist in sorted(linked):
#         dist = Dist(dist)
#         name = r.package_name(dist)
#         replace_existing = name in must_have and dist != must_have[name]
#         prune_it = prune and dist not in smh
#         if replace_existing or prune_it:
#             add_unlink(actions, dist)
#
#     return actions


def augment_specs(prefix, specs, pinned=True):
    _specs = list(specs)

    # get conda-meta/pinned
    if context.respect_pinned:
        pinned_specs = get_pinned_specs(prefix)
        log.debug("Pinned specs=%s", pinned_specs)
        _specs += [MatchSpec(spec) for spec in pinned_specs]

    # support aggressive auto-update conda
    #   Only add a conda spec if conda and conda-env are not in the specs.
    #   Also skip this step if we're offline.
    root_only = ('conda', 'conda-env')
    mss = [MatchSpec(s) for s in _specs if s.name.startswith(root_only)]
    mss = [ms for ms in mss if ms.name in root_only]
    if is_root_prefix(prefix):
        if context.auto_update_conda and not context.offline and not mss:
            _specs.append(MatchSpec('conda'))
            _specs.append(MatchSpec('conda-env'))
    elif basename(prefix).startswith('_'):
        # anything (including conda) can be installed into environments
        # starting with '_', mainly to allow conda-build to build conda
        pass
    elif mss:
        raise InstallError("Error: 'conda' can only be installed into the root environment")

    # support track_features config parameter
    if context.track_features:
        _specs.extend(x + '@' for x in context.track_features)
    return _specs


def _remove_actions(prefix, specs, index, force=False, pinned=True):
    r = Resolve(index)
    linked = linked_data(prefix)
    linked_dists = [d for d in linked.keys()]

    if force:
        mss = list(map(MatchSpec, specs))
        nlinked = {r.package_name(dist): dist
                   for dist in linked_dists
                   if not any(r.match(ms, dist) for ms in mss)}
    else:
        add_defaults_to_specs(r, linked_dists, specs, update=True)
        nlinked = {r.package_name(dist): dist
                   for dist in (Dist(fn) for fn in r.remove(specs, r.installed))}

    if pinned:
        pinned_specs = get_pinned_specs(prefix)
        log.debug("Pinned specs=%s", pinned_specs)

    linked = {r.package_name(dist): dist for dist in linked_dists}

    actions = ensure_linked_actions(r.dependency_sort(nlinked), prefix)
    for old_dist in reversed(r.dependency_sort(linked)):
        # dist = old_fn + '.tar.bz2'
        name = r.package_name(old_dist)
        if old_dist == nlinked.get(name):
            continue
        if pinned and any(r.match(ms, old_dist) for ms in pinned_specs):
            msg = "Cannot remove %s because it is pinned. Use --no-pin to override."
            raise CondaRuntimeError(msg % old_dist.to_filename())
        if (abspath(prefix) == sys.prefix and name == 'conda' and name not in nlinked
                and not context.force):
            if any(s.split(' ', 1)[0] == 'conda' for s in specs):
                raise RemoveError("'conda' cannot be removed from the root environment")
            else:
                raise RemoveError("Error: this 'remove' command cannot be executed because it\n"
                                  "would require removing 'conda' dependencies")
        add_unlink(actions, old_dist)
    actions['SPECS'].extend(specs)
    actions['ACTION'] = 'REMOVE'
    return actions


def remove_actions(prefix, specs, index, force=False, pinned=True):
    return _remove_actions(prefix, specs, index, force, pinned)
    # TODO: can't do this yet because   py.test tests/test_create.py -k test_remove_features
    # if force:
    #     return _remove_actions(prefix, specs, index, force, pinned)
    # else:
    #     specs = set(MatchSpec(s) for s in specs)
    #     unlink_dists, link_dists = solve_for_actions(prefix, get_resolve_object(index.copy(), prefix),  # NOQA
    #                                                  specs_to_remove=specs)
    #
    #     actions = get_blank_actions(prefix)
    #     actions['UNLINK'].extend(unlink_dists)
    #     actions['LINK'].extend(link_dists)
    #     actions['SPECS'].extend(specs)
    #     actions['ACTION'] = 'REMOVE'
    #     return actions


def revert_actions(prefix, revision=-1, index=None):
    # TODO: If revision raise a revision error, should always go back to a safe revision
    # change
    h = History(prefix)
    h.update()
    user_requested_specs = h.get_requested_specs()
    try:
        state = h.get_state(revision)
    except IndexError:
        raise CondaIndexError("no such revision: %d" % revision)

    curr = h.get_state()
    if state == curr:
        return {}  # TODO: return txn with nothing_to_do

    r = get_resolve_object(index, prefix)
    state = r.dependency_sort({d.name: d for d in (Dist(s) for s in state)})
    curr = set(Dist(s) for s in curr)

    link_dists = tuple(d for d in state if not is_linked(prefix, d))
    unlink_dists = set(curr) - set(state)

    # dists = (Dist(s) for s in state)
    # actions = ensure_linked_actions(dists, prefix)
    # for dist in curr - state:
    #     add_unlink(actions, Dist(dist))

    # check whether it is a safe revision
    for dist in concatv(link_dists, unlink_dists):
        if dist not in index:
            from .exceptions import CondaRevisionError
            msg = "Cannot revert to {}, since {} is not in repodata".format(revision, dist)
            raise CondaRevisionError(msg)

    stp = UnlinkLinkTransactionSetup(index, prefix, unlink_dists, link_dists, 'INSTALL',
                                     user_requested_specs)
    txn = UnlinkLinkTransaction(stp)
    return txn


# ---------------------------- EXECUTION --------------------------

def execute_actions(actions, index, verbose=False):
    plan = plan_from_actions(actions, index)
    execute_instructions(plan, index, verbose)


def update_old_plan(old_plan):
    """
    Update an old plan object to work with
    `conda.instructions.execute_instructions`
    """
    plan = []
    for line in old_plan:
        if line.startswith('#'):
            continue
        if ' ' not in line:
            raise ArgumentError("The instruction '%s' takes at least"
                                " one argument" % line)

        instruction, arg = line.split(' ', 1)
        plan.append((instruction, arg))
    return plan


def execute_plan(old_plan, index=None, verbose=False):
    """
    Deprecated: This should `conda.instructions.execute_instructions` instead
    """
    plan = update_old_plan(old_plan)
    execute_instructions(plan, index, verbose)


if __name__ == '__main__':
    # for testing new revert_actions() only
    from pprint import pprint
    pprint(dict(revert_actions(sys.prefix, int(sys.argv[1]))))
