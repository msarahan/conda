from __future__ import absolute_import

from os.path import basename

from conda._vendor.boltons.setutils import IndexedSet
from conda.cli import common
from conda.core.solve import Solver
from conda.models.channel import Channel, prioritize_channels


def install(prefix, specs, args, env, prune=False):
    # TODO: support all various ways this happens
    # Including 'nodefaults' in the channels list disables the defaults
    new_specs = []
    channel_urls = set()
    for elem in specs:
        if "::" in elem:
            channel_urls.add(elem.split("::")[0])
            new_specs.append(elem.split("::")[-1])
        else:
            new_specs.append(elem)
    specs = new_specs
    channel_urls = list(channel_urls)
    # TODO: support all various ways this happens
    # Including 'nodefaults' in the channels list disables the defaults
    channel_urls = channel_urls + [chan for chan in env.channels if chan != 'nodefaults']
    _channel_priority_map = prioritize_channels(channel_urls)

    channel_names = IndexedSet(Channel(url).canonical_name for url in _channel_priority_map)
    channels = IndexedSet(Channel(cn) for cn in channel_names)
    subdirs = IndexedSet(basename(url) for url in _channel_priority_map)

    solver = Solver(prefix, channels, subdirs, specs_to_add=specs)
    unlink_link_transaction = solver.solve_for_transaction(prune)

    with common.json_progress_bars(json=args.json and not args.quiet):
        for actions in action_set:
            try:
                plan.execute_actions(actions, index, verbose=not args.quiet)
            except RuntimeError as e:
                if len(e.args) > 0 and "LOCKERROR" in e.args[0]:
                    raise LockError('Already locked: %s' % text_type(e))
                else:
                    raise CondaHTTPError('CondaHTTPError: %s' % e)
            except SystemExit as e:
                raise CondaSystemExit('Exiting', e)
