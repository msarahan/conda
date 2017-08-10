# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from argparse import SUPPRESS
from collections import defaultdict
import sys

from .conda_argparse import (add_parser_channels, add_parser_insecure, add_parser_json,
                             add_parser_known, add_parser_offline, add_parser_prefix,
                             add_parser_use_index_cache, add_parser_use_local)
from ..compat import text_type

descr = """Search for packages and display associated information.
The input is a MatchSpec, a query language for conda packages.
See examples below.
"""

example = """
Examples:

Search for a specific package named 'scikit-learn':

    conda search scikit-learn

Search for packages containing 'scikit' in the package name:

    conda search *scikit*

Note that your shell may expand '*' before handing the command over to conda.
Therefore it is sometimes necessary to use single or double quotes around the query.

    conda search '*scikit'
    conda search "*scikit*"

Search for packages for 64-bit Linux (by default, packages for your current
platform are shown):

    conda search numpy[subdir=linux-64]

Search for a specific version of a package:

    conda search 'numpy>=1.12'

Search for a package on a specific channel

    conda search conda-forge::numpy
    conda search 'numpy[channel=conda-forge, subdir=osx-64]'
"""


def configure_parser(sub_parsers):
    p = sub_parsers.add_parser(
        'search',
        description=descr,
        help=descr,
        epilog=example,
    )
    add_parser_prefix(p)
    p.add_argument(
        "--canonical",
        action="store_true",
        help=SUPPRESS,
    )
    p.add_argument(
        '-f', "--full-name",
        action="store_true",
        help=SUPPRESS,
    )
    p.add_argument(
        '-i', "--info",
        action="store_true",
        help="Provide detailed information about each package. "
             "Similar to output of 'conda info package-name'."
    )
    p.add_argument(
        "--names-only",
        action="store_true",
        help=SUPPRESS,
    )
    add_parser_known(p)
    add_parser_use_index_cache(p)
    p.add_argument(
        '-o', "--outdated",
        action="store_true",
        help=SUPPRESS,
    )
    p.add_argument(
        '--platform',
        action='store',
        dest='platform',
        help="""Search the given platform. Should be formatted like 'osx-64', 'linux-32',
        'win-64', and so on. The default is to search the current platform.""",
        default=None,
    )
    p.add_argument(
        'match_spec',
        default='*',
        nargs='?',
        help=SUPPRESS,
    )
    p.add_argument(
        "--spec",
        action="store_true",
        help=SUPPRESS,
    )
    p.add_argument(
        "--reverse-dependency",
        action="store_true",
        help="""Perform a reverse dependency search. When using this flag, the --full-name
flag is recommended. Use 'conda info package' to see the dependencies of a
package.""",
    )
    add_parser_offline(p)
    add_parser_channels(p)
    add_parser_json(p)
    add_parser_use_local(p)
    add_parser_insecure(p)
    p.set_defaults(func=execute)


def execute(args, parser):
    from ..models.match_spec import MatchSpec
    from ..models.version import VersionOrder
    from ..base.context import context
    from ..cli.common import stdout_json
    from ..common.io import spinner

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

        from ..core.repodata import query_all
        matches = sorted(query_all(channel_urls, subdirs, spec),
                         key=lambda rec: (rec.name, VersionOrder(rec.version), rec.build))

    if not matches:
        from .install import calculate_channel_urls
        channels_urls = tuple(calculate_channel_urls(
            channel_urls=context.channels,
            prepend=not args.override_channels,
            platform=None,
            use_local=args.use_local,
        ))
        from ..models.match_spec import MatchSpec
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
        sys.stdout.write('\n'.join(builder))
        sys.stdout.write('\n')


def pretty_record(record):
    from ..utils import human_bytes
    from ..resolve import dashlist

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
    builder.append("%-12s: %s" % ("dependencies", dashlist(record.depends)))
    builder.append('\n')
    sys.stdout.write('\n'.join(builder))
    sys.stdout.write('\n')
