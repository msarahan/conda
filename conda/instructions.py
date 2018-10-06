from __future__ import absolute_import, division, print_function

from logging import getLogger

from .base.context import context
from .core.install import PackageUninstaller, get_package_installer
from .core.package_cache import extract, fetch_pkg, is_extracted, rm_extracted, rm_fetched
from .models.dist import Dist
from .models.enums import LinkType


log = getLogger(__name__)

# op codes
FETCH = 'FETCH'
EXTRACT = 'EXTRACT'
UNLINK = 'UNLINK'
LINK = 'LINK'
RM_EXTRACTED = 'RM_EXTRACTED'
RM_FETCHED = 'RM_FETCHED'
PREFIX = 'PREFIX'
PRINT = 'PRINT'
PROGRESS = 'PROGRESS'
SYMLINK_CONDA = 'SYMLINK_CONDA'


progress_cmds = set([EXTRACT, RM_EXTRACTED, LINK, UNLINK])
action_codes = (
    FETCH,
    EXTRACT,
    UNLINK,
    LINK,
    SYMLINK_CONDA,
    RM_EXTRACTED,
    RM_FETCHED,
)


def PREFIX_CMD(state, arg):
    state['prefix'] = arg


def PRINT_CMD(state, arg):
    getLogger('print').info(arg)


def FETCH_CMD(state, arg):
    dist = Dist(arg)
    fetch_pkg(state['index'][dist])


def PROGRESS_CMD(state, arg):
    state['i'] = 0
    state['maxval'] = int(arg)
    getLogger('progress.start').info(state['maxval'])


def EXTRACT_CMD(state, arg):
    dist = Dist(arg)
    if not is_extracted(dist):
        extract(dist)


def RM_EXTRACTED_CMD(state, arg):
    dist = Dist(arg)
    rm_extracted(dist)


def RM_FETCHED_CMD(state, arg):
    dist = Dist(arg)
    rm_fetched(dist)


def split_linkarg(arg):
    """Return tuple(dist, linktype)"""
    parts = arg.split()
    return (parts[0], int(LinkType.hardlink if len(parts) < 2 else parts[1]))


def LINK_CMD(state, arg):
    dist, lt = split_linkarg(arg)
    dist, lt = Dist(dist), LinkType(lt)
    log.debug("=======> LINKING %s <=======", dist)
    installer = get_package_installer(state['prefix'], state['index'], dist)
    installer.link(lt)


def UNLINK_CMD(state, arg):
    log.debug("=======> UNLINKING %s <=======", arg)
    dist = Dist(arg)
    PackageUninstaller(state['prefix'], dist).unlink()


def SYMLINK_CONDA_CMD(state, arg):
    log.debug("No longer symlinking conda. Passing for prefix %s", state['prefix'])
    # symlink_conda(state['prefix'], arg)


# Map instruction to command (a python function)
commands = {
    PREFIX: PREFIX_CMD,
    PRINT: PRINT_CMD,
    FETCH: FETCH_CMD,
    PROGRESS: PROGRESS_CMD,
    EXTRACT: EXTRACT_CMD,
    RM_EXTRACTED: RM_EXTRACTED_CMD,
    RM_FETCHED: RM_FETCHED_CMD,
    LINK: LINK_CMD,
    UNLINK: UNLINK_CMD,
    SYMLINK_CONDA: SYMLINK_CONDA_CMD,
}


def execute_instructions(plan, index=None, verbose=False, _commands=None):
    """Execute the instructions in the plan

    :param plan: A list of (instruction, arg) tuples
    :param index: The meta-data index
    :param verbose: verbose output
    :param _commands: (For testing only) dict mapping an instruction to executable if None
    then the default commands will be used
    """
    if _commands is None:
        _commands = commands

    if verbose:
        from .console import setup_verbose_handlers
        setup_verbose_handlers()

    log.debug("executing plan %s", plan)

    state = {'i': None, 'prefix': context.root_dir, 'index': index}

    for instruction, arg in plan:

        log.debug(' %s(%r)', instruction, arg)

        if state['i'] is not None and instruction in progress_cmds:
            state['i'] += 1
            getLogger('progress.update').info((Dist(arg).dist_name,
                                               state['i'] - 1))
        cmd = _commands[instruction]

        cmd(state, arg)

        if (state['i'] is not None and instruction in progress_cmds and
                state['maxval'] == state['i']):
            state['i'] = None
            getLogger('progress.stop').info(None)
