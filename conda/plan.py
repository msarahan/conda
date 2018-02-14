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
from logging import getLogger
from os.path import abspath
import sys

from .base.constants import DEFAULTS_CHANNEL_NAME, UNKNOWN_CHANNEL
from .base.context import context
from .cli import common
from .cli.common import pkg_if_in_private_env, prefix_if_in_private_env
from .common.compat import odict, on_win
from .common.io import time_recorder
from .core.index import _supplement_index_with_prefix, LAST_CHANNEL_URLS
from .core.link import PrefixSetup, UnlinkLinkTransaction
from .core.linked_data import is_linked, linked_data
from .core.solve import get_pinned_specs
from .exceptions import CondaIndexError, RemoveError
from .history import History
from .instructions import (CHECK_EXTRACT, CHECK_FETCH, EXTRACT, FETCH, LINK, PREFIX,
                           RM_EXTRACTED, RM_FETCHED, SYMLINK_CONDA, UNLINK)
from .models.channel import Channel, prioritize_channels
from .models.dist import Dist
from .models.enums import LinkType
from .models.version import normalized_version
from .resolve import MatchSpec, Resolve, dashlist
from .utils import human_bytes

try:
    from cytoolz.itertoolz import concat, concatv, remove
except ImportError:  # pragma: no cover
    from ._vendor.toolz.itertoolz import concat, concatv, remove  # NOQA

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


def display_actions(actions, index, show_channel_urls=None, specs_to_remove=(), specs_to_add=()):
    prefix = actions.get("PREFIX")
    builder = ['', '## Package Plan ##\n']
    if prefix:
        builder.append('  environment location: %s' % prefix)
        builder.append('')
    if specs_to_remove:
        builder.append('  removed specs: %s'
                       % dashlist(sorted(text_type(s) for s in specs_to_remove), indent=4))
        builder.append('')
    if specs_to_add:
        builder.append('  added / updated specs: %s'
                       % dashlist(sorted(text_type(s) for s in specs_to_add), indent=4))
        builder.append('')
    print('\n'.join(builder))

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
        features[pkg][1] = ','.join(rec.get('features') or ())
    for arg in actions.get(UNLINK, []):
        dist = Dist(arg)
        rec = index[dist]
        pkg = rec['name']
        channels[pkg][0] = channel_str(rec)
        packages[pkg][0] = rec['version'] + '-' + rec['build']
        records[pkg][0] = rec
        features[pkg][0] = ','.join(rec.get('features') or ())

    new = {p for p in packages if not packages[p][0]}
    removed = {p for p in packages if not packages[p][1]}
    # New packages are actually listed in the left-hand column,
    # so let's move them over there
    for pkg in new:
        for var in (packages, features, channels, records):
            var[pkg] = var[pkg][::-1]

    updated = set()
    downgraded = set()
    channeled = set()
    oldfmt = {}
    newfmt = {}
    empty = True
    if packages:
        empty = False
        maxpkg = max(len(p) for p in packages) + 1
        maxoldver = max(len(p[0]) for p in packages.values())
        maxnewver = max(len(p[1]) for p in packages.values())
        maxoldfeatures = max(len(p[0]) for p in features.values())
        maxnewfeatures = max(len(p[1]) for p in features.values())
        maxoldchannels = max(len(channel_filt(p[0])) for p in channels.values())
        maxnewchannels = max(len(channel_filt(p[1])) for p in channels.values())
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
        print("\nThe following packages will be DOWNGRADED:\n")
        for pkg in sorted(downgraded):
            print(format(oldfmt[pkg] + arrow + newfmt[pkg], pkg))

    if empty and actions.get(SYMLINK_CONDA):
        print("\nThe following empty environments will be CREATED:\n")
        print(actions['PREFIX'])

    print()


def add_unlink(actions, dist):
    assert isinstance(dist, Dist)
    if UNLINK not in actions:
        actions[UNLINK] = []
    actions[UNLINK].append(dist)


# force_linked_actions has now been folded into this function, and is enabled by
# supplying an index and setting force=True
def ensure_linked_actions(dists, prefix, index=None, force=False,
                          always_copy=False):  # pragma: no cover
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


# -------------------------------------------------------------------


        if def_ver is not None:
            specs.append('%s %s*' % (name, def_ver))
    log.debug('HF specs=%r' % (specs,))


def get_pinned_specs(prefix):
    """Find pinned specs from file and return a tuple of MatchSpec."""
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

    return tuple(MatchSpec(munge_spec(s), optional=True) for s in
                 concatv(context.pinned_packages, from_file))


# Has one spec (string) for each env
SpecForEnv = namedtuple('DistForEnv', ['env', 'spec'])
# Has several spec (strings) for each prefix and the related r value
SpecsForPrefix = namedtuple('DistsForPrefix', ['prefix', 'specs', 'r'])


@time_recorder("install_actions")
def install_actions(prefix, index, specs, force=False, only_names=None, always_copy=False,
                    pinned=True, minimal_hint=False, update_deps=True, prune=False,
                    channel_priority_map=None, is_update=False):  # pragma: no cover
    """
    This function ignores preferred_env.  It's currently used extensively by conda-build, but
    it is no longer used within the conda code.  Instead, we now use `install_actions_list()`.
    """
    # type: (str, Dict[Dist, Record], List[str], bool, Option[List[str]], bool, bool, bool,
    #        bool, bool, bool, Dict[str, Sequence[str, int]]) -> Dict[weird]
    r = get_resolve_object(index.copy(), prefix)
    str_specs = specs

    specs_for_prefix = SpecsForPrefix(
        prefix=prefix, specs=tuple(str_specs), r=r
    )
    actions = get_actions_for_dists(specs_for_prefix, only_names, index, force, always_copy, prune,
                                    update_deps, pinned)
    return actions


@time_recorder("install_actions_list")
def install_actions_list(prefix, index, specs, force=False, only_names=None, always_copy=False,
                         pinned=True, minimal_hint=False, update_deps=True, prune=False,
                         channel_priority_map=None, is_update=False):
    # type: (str, Dict[Dist, Record], List[str], bool, Option[List[str]], bool, bool, bool,
    #        bool, bool, bool, Dict[str, Sequence[str, int]]) -> List[Dict[weird]]
    specs = [MatchSpec(spec) for spec in specs]
    r = get_resolve_object(index.copy(), prefix)

    linked_in_root = linked_data(context.root_prefix)

    dists_for_envs = determine_all_envs(r, specs, channel_priority_map=channel_priority_map)
    ensure_packge_not_duplicated_in_private_env_root(dists_for_envs, linked_in_root)
    preferred_envs = set(d.env for d in dists_for_envs)

    # Group specs by prefix
    grouped_specs = determine_dists_per_prefix(r, prefix, index, preferred_envs,
                                               dists_for_envs, context)

    # Replace SpecsForPrefix specs with specs that were passed in in order to retain
    #   version information
    required_solves = match_to_original_specs(specs, grouped_specs)

    actions = [get_actions_for_dists(specs_by_prefix, only_names, index, force,
                                     always_copy, prune, update_deps, pinned)
               for specs_by_prefix in required_solves]

    # Need to add unlink actions if updating a private env from root
    if is_update and prefix == context.root_prefix:
        add_unlink_options_for_update(actions, required_solves, index)

    return actions


def add_unlink_options_for_update(actions, required_solves, index):
    # type: (Dict[weird], List[SpecsForPrefix], List[weird]) -> ()
    get_action_for_prefix = lambda prfx: tuple(actn for actn in actions if actn["PREFIX"] == prfx)
    linked_in_prefix = linked_data(context.root_prefix)
    spec_in_root = lambda spc: tuple(
        mtch for mtch in linked_in_prefix.keys() if MatchSpec(spc).match(mtch))
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


def determine_all_envs(r, specs, channel_priority_map=None):
    # type: (Record, List[MatchSpec], Option[List[Tuple]] -> List[SpecForEnv]
    assert all(isinstance(spec, MatchSpec) for spec in specs)
    best_pkgs = (r.index[r.get_dists_for_spec(s, emptyok=False)[-1]] for s in specs)
    spec_for_envs = tuple(SpecForEnv(env=p.preferred_env, spec=p.name) for p in best_pkgs)
    return spec_for_envs


def ensure_packge_not_duplicated_in_private_env_root(dists_for_envs, linked_in_root):
    # type: List[DistForEnv], List[(Dist, Record)] -> ()
    for dist_env in dists_for_envs:
        # If trying to install a package in root that is already in a private env
        if dist_env.env is None and common.prefix_if_in_private_env(dist_env.spec) is not None:
            raise InstallError("Package %s is already installed in a private env %s" %
                               (dist_env.spec, dist_env.env))
        # If trying to install a package in a private env that is already in root
        if (is_private_env(dist_env.env) and
                any(dist for dist in linked_in_root if dist.dist_name.startswith(dist_env.spec))):
            raise InstallError("Package %s is already installed in root. Can't install in private"
                               " environment %s" % (dist_env.spec, dist_env.env))


def not_requires_private_env(prefix, preferred_envs):
    if (context.prefix_specified is True or not context.prefix == context.root_prefix or
            all(preferred_env_matches_prefix(preferred_env, prefix, context.root_prefix) for
                preferred_env in preferred_envs)):
        return True
    return False


def determine_dists_per_prefix(r, prefix, index, preferred_envs, dists_for_envs, context):
    # type: (Resolve, string, List[(Dist, Record)], Set[String], List[SpecForEnv]) ->
    #   (List[pecsForPrefix])

    # if len(preferred_envs) == 1 and preferred_env matches prefix
    #    solution is good
    # if len(preferred_envs) == 1 and preferred_env is None
    #    solution is good
    # if len(preferred_envs) == 2 and set([None, preferred_env]) preferred_env matches prefix
    #    solution is good
    if not_requires_private_env(prefix, preferred_envs):
        dists = set(d.spec for d in dists_for_envs)
        prefix_with_dists_no_deps_has_resolve = [SpecsForPrefix(prefix=prefix, r=r, specs=dists)]
    else:
        channels = subdirs = None

        def get_r(preferred_env):
            # don't make r for the prefix where we already have it created
            if preferred_env_matches_prefix(preferred_env, prefix, context.root_prefix):
                return r
            else:
                return get_resolve_object(index.copy(), preferred_env_to_prefix(
                    preferred_env, context.root_prefix, context.envs_dirs))

        prefix_with_dists_no_deps_has_resolve = []
        for env in preferred_envs:
            dists = IndexedSet(d.spec for d in dists_for_envs if d.env == env)
            prefix_with_dists_no_deps_has_resolve.append(
                SpecsForPrefix(
                    prefix=preferred_env_to_prefix(env, context.root_prefix, context.envs_dirs),
                    r=get_r(env),
                    specs=dists)
            )
    return prefix_with_dists_no_deps_has_resolve


def match_to_original_specs(specs, specs_for_prefix):
    matches_any_spec = lambda dst: next(spc for spc in specs if spc.name == dst)
    matched_specs_for_prefix = []
    for prefix_with_dists in specs_for_prefix:
        new_matches = []
        for spec in prefix_with_dists.specs:
            matched = matches_any_spec(spec)
            if matched:
                new_matches.append(matched)
        matched_specs_for_prefix.append(SpecsForPrefix(
            prefix=prefix_with_dists.prefix, r=prefix_with_dists.r, specs=new_matches))
    return matched_specs_for_prefix


def get_actions_for_dists(specs_by_prefix, only_names, index, force, always_copy, prune,
                          update_deps, pinned):
    root_only = ('conda', 'conda-env')
    prefix = specs_by_prefix.prefix
    r = specs_by_prefix.r
    specs = [MatchSpec(s) for s in specs_by_prefix.specs]
    specs = augment_specs(prefix, specs, pinned)

    linked = linked_data(prefix)
    add_defaults_to_specs(r, linked, specs, prefix)

    installed = linked
    if prune:
        installed = []
    pkgs = r.install(specs, installed, update_deps=update_deps)

    must_have = odict()
    for fn in pkgs:
        dist = Dist(fn)
        name = r.package_name(dist)
        if not name or only_names and name not in only_names:
            continue
        must_have[name] = dist

    if is_root_prefix(prefix):
        # for name in foreign:
        #     if name in must_have:
        #         del must_have[name]
        pass
    elif basename(prefix).startswith('_'):
        # anything (including conda) can be installed into environments
        # starting with '_', mainly to allow conda-build to build conda
        pass

    elif any(s in must_have for s in root_only):
        # the solver scheduled an install of conda, but it wasn't in the
        # specs, so it must have been a dependency.
        specs = [s for s in specs if r.depends_on(s, root_only)]
        if specs:
            raise InstallError("""\
Error: the following specs depend on 'conda' and can only be installed
into the root environment: %s""" % (' '.join(spec.name for spec in specs),))
        linked = [r.package_name(s) for s in linked]
        linked = [s for s in linked if r.depends_on(s, root_only)]
        if linked:
            raise InstallError("""\
Error: one or more of the packages already installed depend on 'conda'
and should only be installed in the root environment: %s
These packages need to be removed before conda can proceed.""" % (' '.join(linked),))
        raise InstallError("Error: 'conda' can only be installed into the "
                           "root environment")

    smh = r.dependency_sort(must_have)
    actions = ensure_linked_actions(
        smh, prefix,
        index=r.index,
        force=force, always_copy=always_copy)

    if actions[LINK]:
        actions[SYMLINK_CONDA] = [context.root_prefix]

    for dist in sorted(linked):
        dist = Dist(dist)
        name = r.package_name(dist)
        replace_existing = name in must_have and dist != must_have[name]
        prune_it = prune and dist not in smh
        if replace_existing or prune_it:
            add_unlink(actions, dist)

    solver = Solver(prefix, channels, subdirs, specs_to_add=specs)
    if index:
        solver._index = index
    txn = solver.solve_for_transaction(prune=prune, ignore_pinned=not pinned)
    prefix_setup = txn.prefix_setups[prefix]
    actions = get_blank_actions(prefix)
    actions['UNLINK'].extend(Dist(prec) for prec in prefix_setup.unlink_precs)
    actions['LINK'].extend(Dist(prec) for prec in prefix_setup.link_precs)
    return actions


def augment_specs(prefix, specs, pinned=True):
    """
    Include additional specs for conda and (optionally) pinned packages.

    Parameters
    ----------
    prefix : str
        Environment prefix.
    specs : list of MatchSpec
        List of package specifications to augment.
    pinned : bool, optional
        Optionally include pinned specs for the current environment.

    Returns
    -------
    augmented_specs : list of MatchSpec
       List of augmented package specifications.
    """
    specs = list(specs)

    # Get conda-meta/pinned
    if pinned:
        pinned_specs = get_pinned_specs(prefix)
        log.debug("Pinned specs=%s", pinned_specs)
        specs.extend(pinned_specs)

    # Support aggressive auto-update conda
    #   Only add a conda spec if conda and conda-env are not in the specs.
    #   Also skip this step if we're offline.
    root_only_specs_str = ('conda', 'conda-env')
    conda_in_specs_str = any(spec for spec in specs if spec.name in root_only_specs_str)

    if is_root_prefix(prefix):
        if context.auto_update_conda and not context.offline and not conda_in_specs_str:
            specs.append(MatchSpec('conda'))
            specs.append(MatchSpec('conda-env'))
    elif basename(prefix).startswith('_'):
        # Anything (including conda) can be installed into environments
        # starting with '_', mainly to allow conda-build to build conda
        pass
    elif conda_in_specs_str:
        raise InstallError("Error: 'conda' can only be installed into the "
                           "root environment")

    # Support track_features config parameter
    if context.track_features:
        specs.extend(x + '@' for x in context.track_features)

    return list(specs)


def _remove_actions(prefix, specs, index, force=False, pinned=True):  # pragma: no cover
    r = Resolve(index)
    linked = linked_data(prefix)
    linked_dists = [d for d in linked]

    if force:
        mss = list(map(MatchSpec, specs))
        nlinked = {r.package_name(dist): dist
                   for dist in linked_dists
                   if not any(r.match(ms, dist) for ms in mss)}
    else:
        add_defaults_to_specs(r, linked_dists, specs, update=True)
        nlinked = {r.package_name(dist): dist
                   for dist in (Dist(fn) for fn in r.remove(specs, set(linked_dists)))}

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
            raise RemoveError(msg % old_dist.to_filename())
        if context.conda_in_root and name == 'conda' and name not in nlinked and not context.force:
            if any(s.split(' ', 1)[0] == 'conda' for s in specs):
                raise RemoveError("'conda' cannot be removed from the base environment")
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
    user_requested_specs = itervalues(h.get_requested_specs_map())
    try:
        state = h.get_state(revision)
    except IndexError:
        raise CondaIndexError("no such revision: %d" % revision)

    curr = h.get_state()
    if state == curr:
        return UnlinkLinkTransaction()

    _supplement_index_with_prefix(index, prefix)
    r = Resolve(index)

    state = r.dependency_sort({d.name: d for d in (Dist(s) for s in state)})
    curr = set(Dist(s) for s in curr)

    link_dists = tuple(d for d in state if not is_linked(prefix, d))
    unlink_dists = set(curr) - set(state)

    # check whether it is a safe revision
    for dist in concatv(link_dists, unlink_dists):
        if dist not in index:
            from .exceptions import CondaRevisionError
            msg = "Cannot revert to {}, since {} is not in repodata".format(revision, dist)
            raise CondaRevisionError(msg)
    return actions


# ---------------------------- Backwards compat for conda-build --------------------------

@time_recorder("execute_actions")
def execute_actions(actions, index, verbose=False):
    plan = plan_from_actions(actions, index)
    execute_instructions(plan, index, verbose)


def _plan_from_actions(actions, index):  # pragma: no cover
    from .instructions import ACTION_CODES, PREFIX, PRINT, PROGRESS, PROGRESS_COMMANDS

    if 'op_order' in actions and actions['op_order']:
        op_order = actions['op_order']
    else:
        op_order = ACTION_CODES

    assert PREFIX in actions and actions[PREFIX]
    prefix = actions[PREFIX]
    plan = [('PREFIX', '%s' % prefix)]

    unlink_link_transaction = actions.get('UNLINKLINKTRANSACTION')
    if unlink_link_transaction:
        raise RuntimeError()
        # progressive_fetch_extract = actions.get('PROGRESSIVEFETCHEXTRACT')
        # if progressive_fetch_extract:
        #     plan.append((PROGRESSIVEFETCHEXTRACT, progressive_fetch_extract))
        # plan.append((UNLINKLINKTRANSACTION, unlink_link_transaction))
        # return plan

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

    plan = _inject_UNLINKLINKTRANSACTION(plan, index, prefix, axn, specs)

    return plan


def _inject_UNLINKLINKTRANSACTION(plan, index, prefix, axn, specs):  # pragma: no cover
    from os.path import isdir
    from .models.dist import Dist
    from ._vendor.toolz.itertoolz import groupby
    from .instructions import LINK, PROGRESSIVEFETCHEXTRACT, UNLINK, UNLINKLINKTRANSACTION
    from .core.package_cache import ProgressiveFetchExtract
    from .core.link import PrefixSetup, UnlinkLinkTransaction
    # this is only used for conda-build at this point
    first_unlink_link_idx = next((q for q, p in enumerate(plan) if p[0] in (UNLINK, LINK)), -1)
    if first_unlink_link_idx >= 0:
        grouped_instructions = groupby(lambda x: x[0], plan)
        unlink_dists = tuple(Dist(d[1]) for d in grouped_instructions.get(UNLINK, ()))
        link_dists = tuple(Dist(d[1]) for d in grouped_instructions.get(LINK, ()))
        unlink_dists, link_dists = _handle_menuinst(unlink_dists, link_dists)

        if isdir(prefix):
            unlink_precs = tuple(index[d] for d in unlink_dists)
        else:
            # there's nothing to unlink in an environment that doesn't exist
            # this is a hack for what appears to be a logic error in conda-build
            # caught in tests/test_subpackages.py::test_subpackage_recipes[python_test_dep]
            unlink_precs = ()
        link_precs = tuple(index[d] for d in link_dists)

        pfe = ProgressiveFetchExtract(link_precs)
        pfe.prepare()

        stp = PrefixSetup(prefix, unlink_precs, link_precs, (), specs)
        plan.insert(first_unlink_link_idx, (UNLINKLINKTRANSACTION, UnlinkLinkTransaction(stp)))
        plan.insert(first_unlink_link_idx, (PROGRESSIVEFETCHEXTRACT, pfe))
    elif axn in ('INSTALL', 'CREATE'):
        plan.insert(0, (UNLINKLINKTRANSACTION, (prefix, (), (), (), specs)))

    return plan


def _handle_menuinst(unlink_dists, link_dists):  # pragma: no cover
    from ._vendor.toolz.itertoolz import concatv
    from .common.compat import on_win
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


def install_actions(prefix, index, specs, force=False, only_names=None, always_copy=False,
                    pinned=True, update_deps=True, prune=False,
                    channel_priority_map=None, is_update=False,
                    minimal_hint=False):  # pragma: no cover
    # this is for conda-build
    from os.path import basename
    from ._vendor.boltons.setutils import IndexedSet
    from .core.solve import Solver
    from .models.channel import Channel
    from .models.dist import Dist
    if channel_priority_map:
        channel_names = IndexedSet(Channel(url).canonical_name for url in channel_priority_map)
        channels = IndexedSet(Channel(cn) for cn in channel_names)
        subdirs = IndexedSet(basename(url) for url in channel_priority_map)
    else:
        # a hack for when conda-build calls this function without giving channel_priority_map
        if LAST_CHANNEL_URLS:
            channel_priority_map = prioritize_channels(LAST_CHANNEL_URLS)
            channels = IndexedSet(Channel(url) for url in channel_priority_map)
            subdirs = IndexedSet(
                subdir for subdir in (c.subdir for c in channels) if subdir
            ) or context.subdirs
        else:
            channels = subdirs = None

    specs = tuple(MatchSpec(spec) for spec in specs)

    from .core.linked_data import PrefixData
    PrefixData._cache_.clear()

    solver = Solver(prefix, channels, subdirs, specs_to_add=specs)
    if index:
        solver._index = index
    txn = solver.solve_for_transaction(prune=prune, ignore_pinned=not pinned)
    prefix_setup = txn.prefix_setups[prefix]
    actions = get_blank_actions(prefix)
    actions['UNLINK'].extend(Dist(prec) for prec in prefix_setup.unlink_precs)
    actions['LINK'].extend(Dist(prec) for prec in prefix_setup.link_precs)
    return actions


def get_blank_actions(prefix):  # pragma: no cover
    from collections import defaultdict
    from .instructions import (CHECK_EXTRACT, CHECK_FETCH, EXTRACT, FETCH, LINK, PREFIX,
                               RM_EXTRACTED, RM_FETCHED, SYMLINK_CONDA, UNLINK)
    actions = defaultdict(list)
    actions[PREFIX] = prefix
    actions['op_order'] = (CHECK_FETCH, RM_FETCHED, FETCH, CHECK_EXTRACT,
                           RM_EXTRACTED, EXTRACT,
                           UNLINK, LINK, SYMLINK_CONDA)
    return actions


def execute_plan(old_plan, index=None, verbose=False):  # pragma: no cover
    """
    Deprecated: This should `conda.instructions.execute_instructions` instead
    """
    plan = _update_old_plan(old_plan)
    execute_instructions(plan, index, verbose)


def execute_instructions(plan, index=None, verbose=False, _commands=None):  # pragma: no cover
    """Execute the instructions in the plan

    :param plan: A list of (instruction, arg) tuples
    :param index: The meta-data index
    :param verbose: verbose output
    :param _commands: (For testing only) dict mapping an instruction to executable if None
    then the default commands will be used
    """
    from .instructions import commands, PROGRESS_COMMANDS
    from .base.context import context
    from .models.dist import Dist
    if _commands is None:
        _commands = commands

    log.debug("executing plan %s", plan)

    state = {'i': None, 'prefix': context.root_prefix, 'index': index}

    for instruction, arg in plan:

        log.debug(' %s(%r)', instruction, arg)

        if state['i'] is not None and instruction in PROGRESS_COMMANDS:
            state['i'] += 1
            getLogger('progress.update').info((Dist(arg).dist_name,
                                               state['i'] - 1))
        cmd = _commands[instruction]

        if callable(cmd):
            cmd(state, arg)

        if (state['i'] is not None and instruction in PROGRESS_COMMANDS and
                state['maxval'] == state['i']):

            state['i'] = None
            getLogger('progress.stop').info(None)


def _update_old_plan(old_plan):  # pragma: no cover
    """
    Update an old plan object to work with
    `conda.instructions.execute_instructions`
    """
    plan = []
    for line in old_plan:
        if line.startswith('#'):
            continue
        if ' ' not in line:
            from .exceptions import ArgumentError
            raise ArgumentError("The instruction '%s' takes at least"
                                " one argument" % line)

        instruction, arg = line.split(' ', 1)
        plan.append((instruction, arg))
    return plan


@time_recorder("execute_plan")
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
