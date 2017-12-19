# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from collections import defaultdict
import sys

from .install import calculate_channel_urls
from ..base.context import context
from ..cli.common import stdout_json
from ..common.io import spinner
from ..compat import text_type
from ..core.repodata import SubdirData
from ..models.match_spec import MatchSpec
from ..models.version import VersionOrder
from ..resolve import dashlist
from ..utils import human_bytes


def execute(args, parser):
    spec = MatchSpec(args.match_spec)
    if spec.get_exact_value('subdir'):
        subdirs = spec.get_exact_value('subdir'),
    elif args.platform:
        subdirs = args.platform,
    else:
        subdirs = context.subdirs

    with spinner("Loading channels", not context.verbosity and not context.quiet, context.json):
        spec_channel = spec.get_exact_value('channel')
        channel_urls = (spec_channel,) if spec_channel else context.channels

        matches = sorted(SubdirData.query_all(channel_urls, subdirs, spec),
                         key=lambda rec: (rec.name, VersionOrder(rec.version), rec.build))

    if not matches:
        channels_urls = tuple(calculate_channel_urls(
            channel_urls=context.channels,
            prepend=not args.override_channels,
            platform=subdirs[0],
            use_local=args.use_local,
        ))
        from ..exceptions import PackagesNotFoundError
        raise PackagesNotFoundError((text_type(spec),), channels_urls)

    if context.json:
        json_obj = defaultdict(list)
        for match in matches:
            json_obj[match.name].append(match)
        stdout_json(json_obj)

    elif args.info:
        for record in matches:
            pretty_record(record)

    if args.reverse_dependency:
        if not args.regex:
            parser.error("--reverse-dependency requires at least one package name")
        if args.spec:
            parser.error("--reverse-dependency does not work with --spec")

    pat = None
    ms = None
    if args.regex:
        if args.spec:
            ms = MatchSpec(arg2spec(args.regex))
        else:
            regex = args.regex
            if args.full_name:
                regex = r'^%s$' % regex
            try:
                pat = re.compile(regex, re.I)
            except re.error as e:
                from ..exceptions import CommandArgumentError
                raise CommandArgumentError("Failed to compile regex pattern for "
                                           "search: %(regex)s\n"
                                           "regex error: %(regex_error)s",
                                           regex=regex, regex_error=repr(e))

    prefix = context.target_prefix

    linked = linked_data(prefix)
    extracted = set(pc_entry.name for pc_entry in PackageCache.get_all_extracted_entries())

    # XXX: Make this work with more than one platform
    platform = args.platform or ''
    if platform and platform != context.subdir:
        args.unknown = False
    ensure_use_local(args)
    ensure_override_channels_requires_channel(args, dashc=False)
    index = get_index(channel_urls=context.channels, prepend=not args.override_channels,
                      platform=args.platform, use_local=args.use_local,
                      use_cache=args.use_index_cache, prefix=None,
                      unknown=args.unknown)

    r = Resolve(index)
    if args.canonical:
        json = []
    else:
        json = {}

    names = []
    for name in sorted(r.groups):
        if '@' in name:
            continue
        res = []
        if args.reverse_dependency:
            res = [dist for dist in r.get_dists_for_spec(name)
                   if any(pat.search(dep.name) for dep in r.ms_depends(dist))]
        elif ms is not None:
            if ms.name == name:
                res = r.get_dists_for_spec(ms)
        elif pat is None or pat.search(name):
            res = r.get_dists_for_spec(name)
        if res:
            names.append((name, res))

    if not names:
        raise ResolvePackageNotFound([(args.regex,)])

    for name, pkgs in names:
        disp_name = name

        if args.names_only and not args.outdated:
            print(name)
            continue

        if not args.canonical:
            json[name] = []

        if args.outdated:
            vers_inst = [dist.quad[1] for dist in linked if dist.quad[0] == name]
            if not vers_inst:
                continue
            assert len(vers_inst) == 1, name
            if not pkgs:
                continue
            latest = pkgs[-1]
            if latest.version == vers_inst[0]:
                continue
            if args.names_only:
                print(name)
                continue

        for dist in pkgs:
            index_record = r.index[dist]
            if args.canonical:
                if not context.json:
                    print(dist.dist_name)
                else:
                    json.append(dist.dist_name)
                continue
            if platform and platform != context.subdir:
                inst = ' '
            elif dist in linked:
                inst = '*'
            elif dist in extracted:
                inst = '.'
            else:
                inst = ' '

            features = r.features(dist)

            if not context.json:
                print('%-25s %s  %-15s %15s  %-15s %s' % (
                    disp_name, inst,
                    index_record.version,
                    index_record.build,
                    index_record.schannel,
                    disp_features(features),
                ))
                disp_name = ''
            else:
                data = {}
                data.update(index_record.dump())
                data.update({
                    'fn': index_record.fn,
                    'installed': inst == '*',
                    'extracted': inst in '*.',
                    'version': index_record.version,
                    'build': index_record.build,
                    'build_number': index_record.build_number,
                    'channel': index_record.schannel,
                    'full_channel': index_record.channel,
                    'features': list(features),
                    'license': index_record.get('license'),
                    'size': index_record.get('size'),
                    'depends': index_record.get('depends'),
                    'type': index_record.get('type')
                })

                if data['type'] == 'app':
                    data['icon'] = make_icon_url(index_record.info)
                json[name].append(data)

    if context.json:
        stdout_json(json)
