"""
Handle the planning of installs and their execution.

NOTE:
    conda.install uses canonical package names in its interface functions,
    whereas conda.resolve uses package filenames, as those are used as index
    keys.  We try to keep fixes to this "impedance mismatch" local to this
    module.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import json
import os
import sys
from collections import defaultdict, namedtuple, OrderedDict
from logging import getLogger
from os.path import abspath, basename, dirname, exists, join

from conda.common.path import preferred_env_to_prefix, preferred_env_matches_prefix
from conda.compat import itervalues
from . import instructions as inst
from .base.constants import DEFAULTS, LinkType
from .base.context import context
from .common.compat import text_type
from .core.index import supplement_index_with_prefix
from .core.linked_data import is_linked, linked_data
from .core.package_cache import find_new_location, is_extracted, is_fetched
from .exceptions import (ArgumentError, CondaIndexError, CondaRuntimeError, InstallError,
                         RemoveError, PackageNotFoundError)
from .gateways.disk.create import try_hard_link
from .gateways.disk.delete import rm_rf
from .history import History
from .models.channel import Channel, prioritize_channels
from .models.dist import Dist
from .resolve import MatchSpec, Package, Resolve
from .utils import human_bytes, md5_file, on_win

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
    if show_channel_urls is None:
        show_channel_urls = context.show_channel_urls

    def channel_str(rec):
        if rec.get('schannel'):
            return rec['schannel']
        if rec.get('url'):
            return Channel(rec['url']).canonical_name
        if rec.get('channel'):
            return Channel(rec['channel']).canonical_name
        return '<unknown>'

    def channel_filt(s):
        if show_channel_urls is False:
            return ''
        if show_channel_urls is None and s == DEFAULTS:
            return ''
        return s

    if actions.get(inst.FETCH):
        print("\nThe following packages will be downloaded:\n")

        disp_lst = []
        for dist in actions[inst.FETCH]:
            dist = Dist(dist)
            info = index[dist]
            extra = '%15s' % human_bytes(info['size'])
            schannel = channel_filt(channel_str(info))
            if schannel:
                extra += '  ' + schannel
            disp_lst.append((dist, extra))
        print_dists(disp_lst)

        if index and len(actions[inst.FETCH]) > 1:
            num_bytes = sum(index[Dist(dist)]['size'] for dist in actions[inst.FETCH])
            print(' ' * 4 + '-' * 60)
            print(" " * 43 + "Total: %14s" % human_bytes(num_bytes))

    # package -> [oldver-oldbuild, newver-newbuild]
    packages = defaultdict(lambda: list(('', '')))
    features = defaultdict(lambda: list(('', '')))
    channels = defaultdict(lambda: list(('', '')))
    records = defaultdict(lambda: list((None, None)))
    linktypes = {}

    for arg in actions.get(inst.LINK, []):
        d, lt = inst.split_linkarg(arg)
        dist = Dist(d)
        rec = index[dist]
        pkg = rec['name']
        channels[pkg][1] = channel_str(rec)
        packages[pkg][1] = rec['version'] + '-' + rec['build']
        records[pkg][1] = Package(dist.to_filename(), rec)
        linktypes[pkg] = lt
        features[pkg][1] = rec.get('features', '')
    for arg in actions.get(inst.UNLINK, []):
        dist = Dist(arg)
        rec = index.get(dist)
        if rec is None:
            package_name, version, build, schannel = dist.quad
            rec = dict(name=package_name,
                       version=version,
                       build=build,
                       channel=None,
                       schannel='<unknown>',
                       build_number=int(build) if build.isdigit() else 0)
        pkg = rec['name']
        channels[pkg][0] = channel_str(rec)
        packages[pkg][0] = rec['version'] + '-' + rec['build']
        records[pkg][0] = Package(dist.to_filename(), rec)
        features[pkg][0] = rec.get('features', '')

    #                     Put a minimum length here---.    .--For the :
    #                                                 v    v

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
        pri0 = P0.priority
        pri1 = P1.priority
        if pri0 is None or pri1 is None:
            pri0 = pri1 = 1
        try:
            if str(P1.version) == 'custom':
                newver = str(P0.version) != 'custom'
                oldver = not newver
            else:
                # <= here means that unchanged packages will be put in updated
                newver = P0.norm_version < P1.norm_version
                oldver = P0.norm_version > P1.norm_version
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
        print("\nThe following packages will be SUPERCEDED by a higher-priority channel:\n")
        for pkg in sorted(channeled):
            print(format(oldfmt[pkg] + arrow + newfmt[pkg], pkg))

    if downgraded:
        print("\nThe following packages will be DOWNGRADED due to dependency conflicts:\n")
        for pkg in sorted(downgraded):
            print(format(oldfmt[pkg] + arrow + newfmt[pkg], pkg))

    if empty and actions.get(inst.SYMLINK_CONDA):
        print("\nThe following empty environments will be CREATED:\n")
        print(actions['PREFIX'])

    print()


def nothing_to_do(action):
    for op in inst.action_codes:
        if action.get(op):
            return False
    return True


def add_unlink(actions, dist):
    assert isinstance(dist, Dist)
    if inst.UNLINK not in actions:
        actions[inst.UNLINK] = []
    actions[inst.UNLINK].append(dist)


def plan_from_actions(actions):
    if 'op_order' in actions and actions['op_order']:
        op_order = actions['op_order']
    else:
        op_order = inst.action_codes

    assert inst.PREFIX in actions and actions[inst.PREFIX]
    res = [('PREFIX', '%s' % actions[inst.PREFIX])]

    if on_win:
        # Always link/unlink menuinst first on windows in case a subsequent
        # package tries to import it to create/remove a shortcut

        for op in (inst.UNLINK, inst.FETCH, inst.EXTRACT, inst.LINK):
            if op in actions:
                pkgs = []
                for pkg in actions[op]:
                    if 'menuinst' in text_type(pkg):
                        res.append((op, pkg))
                    else:
                        pkgs.append(pkg)
                actions[op] = pkgs

    log.debug("Adding plans for operations: {0}".format(op_order))
    for op in op_order:
        if op not in actions:
            log.debug("action {0} not in actions".format(op))
            continue
        if not actions[op]:
            log.debug("action {0} has None value".format(op))
            continue
        if '_' not in op:
            res.append((inst.PRINT, '%sing packages ...' % op.capitalize()))
        elif op.startswith('RM_'):
            res.append((inst.PRINT, 'Pruning %s packages from the cache ...' % op[3:].lower()))
        if op in inst.progress_cmds:
            res.append((inst.PROGRESS, '%d' % len(actions[op])))
        for arg in actions[op]:
            log.debug("appending value {0} for action {1}".format(arg, op))
            res.append((op, arg))

    return res


# force_linked_actions has now been folded into this function, and is enabled by
# supplying an index and setting force=True
def ensure_linked_actions(dists, prefix, index=None, force=False,
                          always_copy=False):
    assert all(isinstance(d, Dist) for d in dists)
    actions = defaultdict(list)
    actions[inst.PREFIX] = prefix
    actions['op_order'] = (inst.RM_FETCHED, inst.FETCH, inst.RM_EXTRACTED,
                           inst.EXTRACT, inst.UNLINK, inst.LINK, inst.SYMLINK_CONDA)
    for dist in dists:
        fetched_in = is_fetched(dist)
        extracted_in = is_extracted(dist)

        if fetched_in and force:
            # Test the MD5, and possibly re-fetch
            fn = dist.to_filename()
            try:
                if md5_file(fetched_in) != index[dist]['md5']:
                    # RM_FETCHED now removes the extracted data too
                    actions[inst.RM_FETCHED].append(dist)
                    # Re-fetch, re-extract, re-link
                    fetched_in = extracted_in = None
                    force = True
            except KeyError:
                sys.stderr.write('Warning: cannot lookup MD5 of: %s' % fn)

        if not force and is_linked(prefix, dist):
            continue

        if extracted_in and force:
            # Always re-extract in the force case
            actions[inst.RM_EXTRACTED].append(dist)
            extracted_in = None

        # Otherwise we need to extract, and possibly fetch
        if not extracted_in and not fetched_in:
            # If there is a cache conflict, clean it up
            fetched_in, conflict = find_new_location(dist)
            fetched_in = join(fetched_in, dist.to_filename())
            if conflict is not None:
                actions[inst.RM_FETCHED].append(Dist(conflict))
            actions[inst.FETCH].append(dist)

        if not extracted_in:
            actions[inst.EXTRACT].append(dist)

        fetched_dist = extracted_in or fetched_in[:-8]
        fetched_dir = dirname(fetched_dist)

        try:
            ppath = join(fetched_dist, 'info')
            index_json = join(ppath, 'index.json')
            with open(index_json, 'r') as f:
                index_data = json.load(f)
            if context.prefix_specified or index_data.get("preferred_env") is None:
                prefix = prefix
            else:
                prefix = join(context.envs_dirs[0], index_data.get("preferred_env"))

            # Determine what kind of linking is necessary
            if not extracted_in:
                # If not already extracted, create some dummy
                # data to test with
                rm_rf(fetched_dist)
                os.makedirs(ppath)
                with open(index_json, 'w') as f:
                    pass
            if context.always_copy or always_copy:
                lt = LinkType.copy
            elif context.always_softlink:
                lt = LinkType.softlink
            elif try_hard_link(fetched_dir, prefix, dist):
                lt = LinkType.hardlink
            elif context.allow_softlinks and not on_win:
                lt = LinkType.softlink
            else:
                lt = LinkType.copy

            actions[inst.LINK].append('%s %d %s' % (dist, lt, prefix))

        except (OSError, IOError):
            actions[inst.LINK].append('%s %d %s' % (dist, LinkType.copy, prefix))
        finally:
            if not extracted_in:
                # Remove the dummy data
                try:
                    rm_rf(fetched_dist)
                except (OSError, IOError):
                    pass

    return actions

# -------------------------------------------------------------------


def is_root_prefix(prefix):
    return abspath(prefix) == abspath(context.root_dir)


def add_defaults_to_specs(r, linked, specs, update=False):
    # TODO: This should use the pinning mechanism. But don't change the API:
    # cas uses it.
    if r.explicit(specs):
        return
    log.debug('H0 specs=%r' % specs)
    names_linked = {r.package_name(d): d for d in linked if d in r.index}
    mspecs = list(map(MatchSpec, specs))

    for name, def_ver in [('python', context.default_python),
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

        if any(s.is_exact() for s in depends_on):
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

        if name == 'python' and def_ver.startswith('3.'):
            # Don't include Python 3 in the specs if this is the Python 3
            # version of conda.
            continue

        if def_ver is not None:
            specs.append('%s %s*' % (name, def_ver))
    log.debug('HF specs=%r' % specs)


def get_pinned_specs(prefix):
    pinfile = join(prefix, 'conda-meta', 'pinned')
    if not exists(pinfile):
        return []
    with open(pinfile) as f:
        return [i for i in f.read().strip().splitlines() if i and not i.strip().startswith('#')]


# Has one spec (string) for each env
SpecForEnv = namedtuple('DistForEnv', ['env', 'spec'])
# Has several spec (strings) for each prefix and the related r value
SpecsForPrefix = namedtuple('DistsForPrefix', ['prefix', 'specs', 'r'])


def install_actions(prefix, index, specs, force=False, only_names=None, always_copy=False,
                    pinned=True, minimal_hint=False,update_deps=True, prune=False,
                    channel_priority_map=None):
    # type: (str, Dict[Dist, Record], List[str], bool, Option[List[str]], bool, bool, bool,
    #        bool, bool, bool, Dict[str, Sequence[str, int]]) -> List[Dict[weird]]
    str_specs = specs
    specs = [MatchSpec(spec) for spec in specs]
    r = get_resolve_object(index.copy(), prefix)

    # Determine how many envs need to be solved for
    dists_for_envs = determine_all_envs(r, specs, channel_priority_map=channel_priority_map)
    preferred_envs = set(d.env for d in dists_for_envs)

    # Group specs by prefix
    grouped_specs = determine_dists_per_prefix(r, prefix, index, preferred_envs, dists_for_envs)

    # Replace SpecsForPrefix specs with specs that were passed in in order to retain
    #   version information
    required_solves = match_to_original_specs(str_specs, grouped_specs)

    actions = tuple(
        get_actions_for_dists(dists_by_prefix, only_names, index, force, always_copy, prune,
                              update_deps, pinned)
        for dists_by_prefix in required_solves)
    return actions


def get_resolve_object(index, prefix):
    # instantiate resolve object
    supplement_index_with_prefix(index, prefix, {})
    r = Resolve(index)
    return r


def determine_all_envs(r, specs, channel_priority_map=None):
    # type: (str, Dict[Dist, Record], List[MatchSpec], bool, Option[List[str]], bool, bool, bool,
    #        bool, bool, bool) -> Dict[weird]
    assert all(isinstance(spec, MatchSpec) for spec in specs)

    # Make sure there is a channel prioritu
    if channel_priority_map is None or len(channel_priority_map) == 0:
        channel_priority_map = prioritize_channels(context.channels)

    # remove duplicates e.g. for channel names with multiple urls
    prioritized_channel_list = set((chnl, prrty) for chnl, prrty in
                                   itervalues(channel_priority_map))
    nth_channel_priority = lambda n: [chnl[0] for chnl in prioritized_channel_list if
                                      chnl[1] == n][0]

    def get_highest_priority_match(matches):
        # This loop: match to the highest priority channel;
        #   if no packages match priority 0, try the next channel
        for i in range(0, len(prioritized_channel_list)):
            target_channel = nth_channel_priority(i)
            highest_match = [m for m in matches if m.schannel == target_channel]
            if len(highest_match) > 0:
                newest_pkg = sorted(highest_match, key=lambda pk: pk.version)[0]
                return newest_pkg
        raise PackageNotFoundError(matches[0].name, "package not found")

    spec_for_envs = []
    for spec in specs:
        matched_dists = r.get_pkgs(spec)
        best_match = get_highest_priority_match(matched_dists)
        spec_for_envs.append(SpecForEnv(env=r.index[Dist(best_match)].preferred_env, spec=best_match.name))
    return spec_for_envs


def not_requires_private_env(prefix, preferred_envs):
    if (context.prefix_specified is True or not context.prefix == context.root_dir or
            all(preferred_env_matches_prefix(preferred_env, prefix, context.root_dir) for
                preferred_env in preferred_envs)):
        return True
    return False


def determine_dists_per_prefix(r, prefix, index, preferred_envs, dists_for_envs):
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
        # Ensure that conda is working in the root dir
        assert(context.prefix == context.root_dir)

        def get_r(preferred_env):
            # don't make r for the prefix where we already have it created
            if preferred_env_matches_prefix(preferred_env, prefix, context.root_dir):
                return r
            else:
                return get_resolve_object(index.copy(), preferred_env_to_prefix(
                    preferred_env, context.root_dir, context.envs_dirs))

        prefix_with_dists_no_deps_has_resolve = []
        for env in preferred_envs:
            dists = set(d.spec for d in dists_for_envs if d.env == env)
            prefix_with_dists_no_deps_has_resolve.append(
                SpecsForPrefix(prefix=preferred_env_to_prefix(
                    env, context.root_dir, context.envs_dirs), r=get_r(env), specs=dists)
            )
    return prefix_with_dists_no_deps_has_resolve


def match_to_original_specs(str_specs, specs_for_prefix):
    matches_any_spec = lambda dst: next(spc for spc in str_specs if spc.startswith(dst))
    matched_specs_for_prefix = []
    for prefix_with_dists in specs_for_prefix:
        linked = linked_data(prefix_with_dists.prefix)
        r = prefix_with_dists.r
        new_matches = []
        for d in prefix_with_dists.specs:
            matched = matches_any_spec(d)
            if matched:
                new_matches.append(matched)
        add_defaults_to_specs(r, linked, new_matches)
        matched_specs_for_prefix.append(SpecsForPrefix(
            prefix=prefix_with_dists.prefix, r=r, specs=new_matches))
    return matched_specs_for_prefix


def get_actions_for_dists(dists_for_prefix, only_names, index, force, always_copy, prune,
                          update_deps, pinned):
    root_only = ('conda', 'conda-env')
    prefix = dists_for_prefix.prefix
    dists = dists_for_prefix.specs
    r = dists_for_prefix.r
    specs = [MatchSpec(dist) for dist in dists]
    specs = augment_specs(prefix, specs, pinned)

    linked = linked_data(prefix)
    must_have = {}

    installed = linked
    if prune:
        installed = []
    pkgs = r.install(specs, installed, update_deps=update_deps)

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
        index=index,
        force=force, always_copy=always_copy)

    if actions[inst.LINK]:
        actions[inst.SYMLINK_CONDA] = [context.root_dir]

    for dist in sorted(linked):
        dist = Dist(dist)
        name = r.package_name(dist)
        replace_existing = name in must_have and dist != must_have[name]
        prune_it = prune and dist not in smh
        if replace_existing or prune_it:
            add_unlink(actions, dist)

    return actions


def augment_specs(prefix, specs, pinned=True):
    # get conda-meta/pinned
    if pinned:
        pinned_specs = get_pinned_specs(prefix)
        log.debug("Pinned specs=%s" % pinned_specs)
        specs += [MatchSpec(spec) for spec in pinned_specs]

    # support aggressive auto-update conda
    #   Only add a conda spec if conda and conda-env are not in the specs.
    #   Also skip this step if we're offline.
    root_only = ('conda', 'conda-env')
    mss = [MatchSpec(s) for s in specs if s.name.startswith(root_only)]
    mss = [ms for ms in mss if ms.name in root_only]
    if is_root_prefix(prefix):
        if context.auto_update_conda and not context.offline and not mss:
            specs.append(MatchSpec('conda'))
            specs.append(MatchSpec('conda-env'))
    elif basename(prefix).startswith('_'):
        # anything (including conda) can be installed into environments
        # starting with '_', mainly to allow conda-build to build conda
        pass
    elif mss:
        raise InstallError("Error: 'conda' can only be installed into the root environment")

    # support track_features config parameter
    if context.track_features:
        specs.extend(x + '@' for x in context.track_features)
    return specs


def remove_actions(prefix, specs, index, force=False, pinned=True):
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
        log.debug("Pinned specs=%s" % pinned_specs)

    linked = {r.package_name(dist): dist for dist in linked_dists}

    actions = ensure_linked_actions(r.dependency_sort(nlinked), prefix)
    for old_dist in reversed(r.dependency_sort(linked)):
        # dist = old_fn + '.tar.bz2'
        name = r.package_name(old_dist)
        if old_dist == nlinked.get(name):
            continue
        if pinned and any(r.match(ms, old_dist.to_filename()) for ms in pinned_specs):
            msg = "Cannot remove %s because it is pinned. Use --no-pin to override."
            raise CondaRuntimeError(msg % old_dist.to_filename())
        if context.conda_in_root and name == 'conda' and name not in nlinked and not context.force:
            if any(s.split(' ', 1)[0] == 'conda' for s in specs):
                raise RemoveError("'conda' cannot be removed from the root environment")
            else:
                raise RemoveError("Error: this 'remove' command cannot be executed because it\n"
                                  "would require removing 'conda' dependencies")
        add_unlink(actions, old_dist)

    return actions


def remove_features_actions(prefix, index, features):
    r = Resolve(index)
    linked = r.installed

    actions = defaultdict(list)
    actions[inst.PREFIX] = prefix
    _linked = [d + '.tar.bz2' for d in linked]
    to_link = []
    for dist in sorted(linked):
        fn = dist + '.tar.bz2'
        if fn not in index:
            continue
        if r.track_features(fn).intersection(features):
            add_unlink(actions, dist)
        if r.features(fn).intersection(features):
            add_unlink(actions, dist)
            subst = r.find_substitute(_linked, features, fn)
            if subst:
                to_link.append(subst[:-8])

    if to_link:
        dists = (Dist(d) for d in to_link)
        actions.update(ensure_linked_actions(dists, prefix))
    return actions


def revert_actions(prefix, revision=-1, index=None):
    # TODO: If revision raise a revision error, should always go back to a safe revision
    # change
    h = History(prefix)
    h.update()
    try:
        state = h.get_state(revision)
    except IndexError:
        raise CondaIndexError("no such revision: %d" % revision)

    curr = h.get_state()
    if state == curr:
        return {}

    dists = (Dist(s) for s in state)
    actions = ensure_linked_actions(dists, prefix)
    for dist in curr - state:
        add_unlink(actions, Dist(dist))

    # check whether it is a safe revision
    from .instructions import split_linkarg, LINK, UNLINK, FETCH
    from .exceptions import CondaRevisionError
    for arg in set(actions.get(LINK, []) + actions.get(UNLINK, []) + actions.get(FETCH, [])):
        if isinstance(arg, Dist):
            dist = arg
        else:
            dist, lt, prefix = split_linkarg(arg)
            dist = Dist(dist)
        if dist not in index:
            msg = "Cannot revert to {}, since {} is not in repodata".format(revision, dist)
            raise CondaRevisionError(msg)

    return actions


# ---------------------------- EXECUTION --------------------------

def execute_actions(actions, index=None, verbose=False):
    plan = plan_from_actions(actions)
    with History(actions[inst.PREFIX]):
        inst.execute_instructions(plan, index, verbose)


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
    inst.execute_instructions(plan, index, verbose)


if __name__ == '__main__':
    # for testing new revert_actions() only
    from pprint import pprint
    pprint(dict(revert_actions(sys.prefix, int(sys.argv[1]))))
