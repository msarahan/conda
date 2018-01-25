# -*- coding: utf-8 -*-
gfrom __future__ import absolute_import, division, print_function, unicode_literals

from collections import defaultdict

from .install import calculate_channel_urls
from ..base.context import context
from ..cli.common import stdout_json
from ..common.io import Spinner
from ..compat import iteritems, text_type
from ..core.envs_manager import search_all_prefixes
from ..core.repodata import SubdirData
from ..models.index_record import PackageRef
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

    if args.envs:
        with Spinner("Searching environments for %s" % spec,
                     not context.verbosity and not context.quiet,
                     context.json):
            prefix_matches = search_all_prefixes(spec)
        formatted_result = tuple({
            'location': prefix,
            'package_refs': tuple(PackageRef.from_objects(prefix_rec)
                                  for prefix_rec in prefix_recs),
        } for prefix, prefix_recs in iteritems(prefix_matches))
        if context.json:
            stdout_json(formatted_result)
        else:
            builder = []
            for pkg_group in formatted_result:
                builder.append("location: %s" % pkg_group['location'])
                builder.append("package matches:%s" % dashlist(
                    pref.dist_str() for pref in pkg_group['package_refs']
                ))
                builder.append('')
            print('\n'.join(builder))
        return 0

    with Spinner("Loading channels", not context.verbosity and not context.quiet, context.json):
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
        builder = ['%-25s  %-15s %15s  %-15s' % (
            "Name",
            "Version",
            "Build",
            "Channel",
        )]
        for record in matches:
            builder.append('%-25s  %-15s %15s  %-15s' % (
                record.name,
                record.version,
                record.build,
                record.schannel,
            ))
        print('\n'.join(builder))


def pretty_record(record):
    def push_line(display_name, attr_name):
        value = getattr(record, attr_name, None)
        if value is not None:
            builder.append("%-12s: %s" % (display_name, value))

    builder = []
    builder.append(record.name + " " + record.version + " " + record.build)
    builder.append('-'*len(builder[0]))

    push_line("file name", "fn")
    push_line("name", "name")
    push_line("version", "version")
    push_line("build string", "build")
    push_line("build number", "build_number")
    builder.append("%-12s: %s" % ("size", human_bytes(record.size)))
    push_line("arch", "arch")
    push_line("constrains", "constrains")
    push_line("platform", "platform")
    push_line("license", "license")
    push_line("subdir", "subdir")
    push_line("url", "url")
    push_line("md5", "md5")
    builder.append("%-12s: %s" % ("dependencies", dashlist(record.depends)))
    builder.append('\n')
    print('\n'.join(builder))
