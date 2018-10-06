# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

import json

import os
import re
import sys
import warnings
from collections import namedtuple
from conda import CONDA_PACKAGE_ROOT
from conda.compat import string_types
from logging import getLogger
from os.path import dirname, join
from subprocess import CalledProcessError, check_call

from .package_cache import is_extracted, read_url
from .._vendor.auxlib.ish import dals
from ..base.constants import LinkType
from ..base.context import context
from ..common.path import (explode_directories, get_all_directories, get_bin_directory_short_path,
                           get_leaf_directories, win_path_ok, pyc_path, get_python_path,
                           parse_entry_point_def)
from ..core.linked_data import (delete_linked_data, get_python_version_for_prefix,
                                get_site_packages_short_path, linked_data, load_meta, set_linked_data)
from ..exceptions import CondaOSError, LinkError, PaddingError
from ..gateways.disk.create import (compile_missing_pyc, create_link,
                                    make_menu, mkdir_p, write_conda_meta_record,
                                    hardlink_supported, softlink_supported, compile_pyc)
from ..gateways.disk.delete import maybe_rmdir_if_empty, rm_rf
from ..gateways.disk.read import collect_all_info_for_package, exists, isdir, isfile, yield_lines
from ..gateways.disk.update import _PaddingError, rename, update_prefix
from ..models.package_info import PathType
from ..models.record import Link, Record
from ..utils import on_win

try:
    from cytoolz.itertoolz import concat, concatv, groupby
except ImportError:
    from .._vendor.toolz.itertoolz import concat, concatv, groupby  # NOQA

log = getLogger(__name__)

MENU_RE = re.compile(r'^menu/.*\.json$', re.IGNORECASE)
LinkOperation = namedtuple('LinkOperation',
                           ('source_short_path', 'dest_short_path', 'link_type',
                            'prefix_placeholder', 'file_mode', 'is_menu_file'))
UnlinkOperation = namedtuple('UnlinkOperation', ('prefix_short_path', 'is_menu_file', 'path_type'))


def get_package_installer(prefix, index, dist):
    # a factory-type function for getting the correct PackageInstaller class
    record = index.get(dist, None)  # None can happen when handing .tar.bz2 file paths at the CLI
    if record and record.noarch and record.noarch.lower() == 'python':
        return NoarchPythonPackageInstaller(prefix, index, dist)
    else:
        return PackageInstaller(prefix, index, dist)


def determine_link_type(extracted_package_dir, target_prefix):
    source_test_file = join(extracted_package_dir, 'info', 'index.json')
    if context.always_copy:
        return LinkType.copy
    if context.always_softlink:
        return LinkType.softlink
    if hardlink_supported(source_test_file, target_prefix):
        return LinkType.hardlink
    if context.allow_softlinks and softlink_supported(source_test_file, target_prefix):
        return LinkType.softlink
    return LinkType.copy



def get_prefix_replace(path_info, requested_link_type):
    if path_info.prefix_placeholder:
        link_type = LinkType.copy
        prefix_placehoder = path_info.prefix_placeholder
        file_mode = path_info.file_mode
    elif path_info.no_link or path_info.path_type == PathType.softlink:
        link_type = LinkType.copy
        prefix_placehoder, file_mode = '', None
    else:
        link_type = requested_link_type
        prefix_placehoder, file_mode = '', None

    return link_type, prefix_placehoder, file_mode


def make_lateral_link_action(source_path_info, extracted_package_dir, target_prefix,
                             requested_link_type):
    # no side effects in this function!
    # a lateral link has the same 'short path' in both the package directory and the target prefix
    short_path = source_path_info.path
    link_type, prefix_placehoder, file_mode = get_prefix_replace(source_path_info, requested_link_type)
    return LinkPathAction(extracted_package_dir, short_path,
                          target_prefix, short_path, link_type,
                          prefix_placehoder, file_mode)



def get_python_noarch_target_path(source_short_path):
    if source_short_path.startswith('site-packages/'):
        sp_dir = '%(target_site_packages_short_path)s'
        # string interpolation done when necessary in the action
        return source_short_path.replace('site-packages', sp_dir, 1)
    elif source_short_path.startswith('python-scripts/'):
        bin_dir = '%(target_bin_dir)s'
        return source_short_path.replace('python-scripts', bin_dir, 1)
    else:
        return source_short_path









def make_link_actions(transaction_context, package_info, target_prefix, requested_link_type):
    # no side effects in this function!

    def make_directory_link_action(directory_short_path):
        # no side effects in this function!
        return LinkPathAction(None, None, target_prefix, directory_short_path, LinkType.directory,
                              None, None)

    def make_file_link_action(source_path_info):

        noarch = package_info.noarch
        if noarch and noarch.type == 'python':
            target_short_path = get_python_noarch_target_path(source_path_info.path)
        elif not noarch or noarch is True or (isinstance(noarch, string_types)
                                              and noarch == 'native'):
            target_short_path = source_path_info.path
        else:
            # TODO: need an error message
            raise CondaUpgradeError()

        link_type, placeholder, fmode = get_prefix_replace(source_path_info, requested_link_type)

        if placeholder:
            assert link_type == LinkType.copy
            return PrefixReplaceLinkAction(transaction_context, package_info,
                                           package_info.extracted_package_dir,
                                           source_path_info.path,
                                           target_prefix, target_short_path,
                                           placeholder, fmode)
        else:
            return LinkPathAction(transaction_context, package_info,
                                  package_info.extracted_package_dir, source_path_info.path,
                                  target_prefix, target_short_path, link_type)

    def make_entry_point_action(entry_point_def):
        command, module, func = parse_entry_point_def(entry_point_def)
        target_short_path = "%s/%s" % (get_bin_directory_short_path(), command)
        CreatePythonEntryPointAction(transaction_context, package_info,
                                     target_prefix, target_short_path, module, func)

    def make_entry_point_windows_executable_action(entry_point_def):
        source_directory = CONDA_PACKAGE_ROOT
        source_short_path = 'resources/cli-%d.exe' % context.bits
        command, _, _ = parse_entry_point_def(entry_point_def)
        target_short_path = "%s/%s.exe" % (get_bin_directory_short_path(), command)
        LinkPathAction(transaction_context, package_info, source_directory, source_short_path,
                       target_prefix, target_short_path, requested_link_type)


    def make_conda_meta_create_action(all_target_short_paths):
        link = Link(source=package_info.extracted_package_dir, type=requested_link_type)
        meta_record =  Record.from_objects(package_info.repodata_record,
                                           package_info.index_json_record,
                                           files=all_target_short_paths, link=link,
                                           url=package_info.url)

        return CreateCondaMetaAction(transaction_context, package_info, target_prefix, meta_record)

    file_link_actions = tuple(make_file_link_action(spi) for spi in package_info.paths)

    leaf_directories = get_leaf_directories(axn.target_short_path for axn in file_link_actions)
    directory_create_actions = tuple(make_directory_link_action(d) for d in leaf_directories)

    if on_win:
        menu_create_actions = tuple(MakeMenuAction(transaction_context, package_info,
                                                   target_prefix, spi.path)
                                    for spi in package_info.paths
                                    if bool(MENU_RE.match(spi.path)))
    else:
        menu_create_actions = ()

    if package_info.noarch.type == 'python':
        python_entry_point_actions = tuple(concatv(
            (make_entry_point_action(ep_def) for ep_def in package_info.noarch.entry_points),
            (make_entry_point_windows_executable_action(ep_def)
             for ep_def in package_info.noarch.entry_points) if on_win else (),
        ))

        py_files = (axn for axn in file_link_actions if axn.source_short_path.endswith('.py'))
        pyc_compile_actions = tuple(CompilePycAction(transaction_context, package_info,
                                                     target_prefix, pf) for pf in py_files)
    else:
        python_entry_point_actions = ()
        pyc_compile_actions = ()


    all_target_short_paths = concat(file_link_actions, python_entry_point_actions, pyc_compile_actions)
    meta_create_actions = (make_conda_meta_create_action(all_target_short_paths),)

    return tuple(concatv(directory_create_actions, file_link_actions, python_entry_point_actions,
                         pyc_compile_actions,  menu_create_actions, meta_create_actions))


class UnlinkLinkTransaction(object):

    def __init__(self, target_prefix, unlink_dists, link_dists):
        # type: (str, Sequence[Dist], Sequence[PackageInfo]])
        # order of unlink_dists and link_dists will be preserved throughout
        #   should be given in dependency-sorted order

        # gather information from disk and caches
        self.prefix_linked_data = linked_data(target_prefix)
        self.prefix_inventory = inventory_prefix(target_prefix)
        self.unlink_dists = unlink_dists
        self.link_packages_info = link_dists

        link_types = [determine_link_type(extracted_package_dir, target_prefix) for dist in link_dists]

        # make all the path_actions
        # No side effects!  All disk access should be done in the section above.
        # first unlink action should be conda-meta json file, because it will roll back
        # last link action should be conda-meta json file

        transaction_context = dict()
        self.unlink_actions = tuple((dist, make_unlink_actions(dist)) for dist in self.unlink_dists)
        self.link_actions = tuple((package_info.dist, make_link_actions(package_info)) for package_info in self.link_packages_info)
        
        # we won't definitively know location of site-packages until here
        transaction_context['target_bin_dir']
        transaction_context['target_python_version']
        transaction_context['target_site_packages_short_path']

        # verify link sources
        # for each LinkPathAction where extracted_package_dir and source_short_path are not None,
        # verify that the file is visible
        # in the future, consider checking hashsums

        # verify link destinations
        # make sure path doesn't exist or will be unlinked first
        [axn.verify() for axn in self.unlink_actions]
        [axn.verify() for axn in self.link_actions]


        # execute the transaction
        try:
            for q, dist, unlink_actions in enumerate(self.unlink_actions):
                run_script(target_prefix, dist, 'pre-unlink')
                
                for p, unlink_action in enumerate(unlink_actions):
                    unlink_action.execute()

                run_script(target_prefix, dist, 'post-unlink')
                
                # now here put in try/except/else for link
                # don't forget the PITA noarch_python
                try:
                    for n, dist, link_actions in enumerate(self.link_actions):
                        run_script(target_prefix, dist, 'pre-link')

                        for m, link_action in enumerate(link_actions):
                            link_action.execute()

                        run_script(target_prefix, dist, 'post-link')

                except Exception as e:
                    # print big explanatory error message
                    rollback_from = n, m
                    raise
                else:
                    for n, dist, link_actions in enumerate(self.link_actions):
                        for m, link_action in enumerate(link_actions):
                            link_action.cleanup()

        except Exception as e:
            # print big explanatory error message
            rollback_from = q, p
        else:
            for q, dist, unlink_actions in enumerate(self.unlink_actions):
                for p, unlink_action in enumerate(unlink_actions):
                    unlink_action.cleanup()






        # # create per-path unlink and link directives, grouped by dist, in dependency-sorted order
        # # self.unlink_directives = tuple((dist, self._make_unlink_operations(dist)) for dist in self.unlink_dists)
        # # # type: Tuple[Tuple[package_name, Tuple[UnlinkOperation]]]
        # self.package_unlinkers = tuple(PackageUnlinker(dist) for dist in self.unlink_dists)
        #
        # # self.link_directives = self._make_link_operations()
        # # # type: Tuple[Tuple[package_name, Tuple[LinkOperation]]]
        # self.package_linkers = tuple(PackageLinker(package_info) for package_info in self.link_packages_info)


        # # unlink
        # #   - assert can remove file
        # #     implies write access within parent directory on unix
        # #   - as assertions are executed, remove file from prefix_inventory
        # for unlinker in self.package_unlinkers:
        #     for directive in unlinker.directives:
        #         assert can_unlink(directive.path), unlinker.package_name
        #         # remove path from prefix_inventory
        #
        # # package cache
        # #   - assert all files exist and visible
        # #   - (maybe?) assert per-file sha sums
        # for package_info in self.link_packages_info:
        #     assert join(package_info.package_full_path, )
        #
        # # link
        # #   - build assertions based on algorithm
        # #   - as assertions are executed, add paths to prefix_inventory
        #
        # #   - create assertions, for both package cache and target_prefix
        # #   - run assertions against file system

        # execute unlink and link directives


    def _make_unlink_operations(self, dist):
        linked_package_data = self.prefix_linked_data[dist]
        package_files = linked_package_data.files
        all_directories = sorted(get_all_directories(package_files), key=len, reverse=True)

        def make_file_unlink_operation(path):
            prefix_short_path = path
            is_menu_file = bool(MENU_RE.match(path))
            # using hardlink here, because treatment would be no different than softlink
            return UnlinkOperation(prefix_short_path, is_menu_file, PathType.hardlink)

        def make_dir_unlink_operation(path):
            return UnlinkOperation(path, False, PathType.directory)

        return tuple(concatv(
            (make_file_unlink_operation(p) for p in package_files),
            (make_dir_unlink_operation(d) for d in all_directories),
        ))


class PackageInstaller(object):

    def __init__(self, prefix, index, dist):
        self.prefix = prefix
        self.index = index
        self.dist = dist
        self.package_info = None  # set in the link method

    def link(self, requested_link_type=LinkType.hardlink):
        log.debug("linking package %s with link type %s", self.dist, requested_link_type)
        self.extracted_package_dir = is_extracted(self.dist)
        assert self.extracted_package_dir is not None
        log.debug("linking package:\n"
                  "  prefix=%s\n"
                  "  source=%s\n"
                  "  link_type=%s\n",
                  self.prefix, self.extracted_package_dir, requested_link_type)

        # filesystem read actions
        #   do all filesystem reads necessary for the rest of the linking for this package
        self.package_info = collect_all_info_for_package(self.extracted_package_dir)
        url = read_url(self.dist)  # TODO: consider making this part of package_info

        # simple processing
        operations = self._make_link_operations(requested_link_type)

        # # run pre-link script
        # if not run_script(extracted_package_dir, self.dist, 'pre-link', self.prefix):
        #     raise LinkError('Error: pre-link failed: %s' % self.dist)

        dest_short_paths = self._execute_link_operations(operations)

        # run post-link script
        if not run_script(self.prefix, self.dist, 'post-link'):
            raise LinkError("Error: post-link failed for: %s" % self.dist)

        # create package's prefix/conda-meta file
        meta_record = self._create_meta(dest_short_paths, requested_link_type, url)
        write_conda_meta_record(self.prefix, meta_record)
        set_linked_data(self.prefix, self.dist.dist_name, meta_record)

    def _make_link_operations(self, requested_link_type):
        # no side effects in this method!
        package_info = self.package_info

        def make_link_operation(source_path_info):
            if source_path_info.prefix_placeholder:
                link_type = LinkType.copy
                prefix_placehoder = source_path_info.prefix_placeholder
                file_mode = source_path_info.file_mode
            elif source_path_info.no_link or source_path_info.path_type == PathType.softlink:
                link_type = LinkType.copy
                prefix_placehoder, file_mode = '', None
            else:
                link_type = requested_link_type
                prefix_placehoder, file_mode = '', None
            is_menu_file = bool(MENU_RE.match(source_path_info.path))
            dest_short_path = source_path_info.path
            return LinkOperation(source_path_info.path, dest_short_path, link_type,
                                 prefix_placehoder, file_mode, is_menu_file)

        def make_dir_operation(directory_short_path):
            return LinkOperation(None, directory_short_path, LinkType.directory,
                                 None, None, False)

        leaf_directories = get_leaf_directories(package_info.paths)
        directory_create_operations = (make_dir_operation(d) for d in leaf_directories)
        file_link_operations = (make_link_operation(p) for p in package_info.paths)

        return tuple(concatv(directory_create_operations, file_link_operations))

    def _execute_link_operations(self, link_operations):
        # major side-effects in this method

        dest_short_paths = []

        # Step 1. Make all directories
        for leaf_directory in leaf_directories:
            mkdir_p(join(self.prefix, win_path_ok(leaf_directory)))

        # Step 2. Do the actual file linking
        for op in link_operations:  # TODO: sort directories first
            try:
                create_link(join(self.extracted_package_dir, win_path_ok(op.source_short_path)),
                            join(self.prefix, win_path_ok(op.dest_short_path)),
                            op.link_type)
                dest_short_paths.append(op.dest_short_path)
            except OSError as e:
                raise CondaOSError('failed to link (src=%r, dst=%r, type=%r, error=%r)' %
                                   (op.source_path, op.dest_path, op.link_type, e))

        # Step 3. Replace prefix placeholder within all necessary files
        # Step 4. Make shortcuts on Windows
        for op in link_operations:
            if op.prefix_placeholder:
                try:
                    update_prefix(join(self.prefix, win_path_ok(op.dest_short_path)), self.prefix,
                                  op.prefix_placeholder, op.file_mode)
                except _PaddingError:
                    raise PaddingError(op.dest_path, op.prefix_placeholder,
                                       len(op.prefix_placeholder))
            if on_win and op.is_menu_file and context.shortcuts:
                make_menu(self.prefix, win_path_ok(op.dest_short_path), remove=False)

        if on_win:
            # make sure that the child environment behaves like the parent,
            #    wrt user/system install on win
            # This is critical for doing shortcuts correctly
            # TODO: I don't understand; talk to @msarahan
            # TODO: sys.prefix is almost certainly *wrong* here
            nonadmin = join(sys.prefix, ".nonadmin")
            if isfile(nonadmin):
                open(join(self.prefix, ".nonadmin"), 'w').close()

        return dest_short_paths

    def _create_meta(self, dest_short_paths, requested_link_type, url):
        """
        Create the conda metadata, in a given prefix, for a given package.
        """
        package_info = self.package_info
        record_from_index = self.index.get(self.dist, {})
        new_info = dict()
        new_info['url'] = url

        # alt_files_path is a hack for python_noarch
        alt_files_path = join(self.prefix, 'conda-meta', self.dist.to_filename('.files'))
        new_info['files'] = (list(yield_lines(alt_files_path)) if isfile(alt_files_path)
                             else dest_short_paths)
        new_info['link'] = Link(source=self.extracted_package_dir, type=requested_link_type)
        if 'icon' in record_from_index:
            new_info['icondata'] = package_info.icondata

        record_from_package = package_info.index_json_record
        return Record.from_objects(new_info, record_from_index, record_from_package)


class NoarchPythonPackageInstaller(PackageInstaller):

    def _make_link_operations(self, requested_link_type):
        # no side effects in this method!
        package_info = self.package_info
        site_packages_dir = get_site_packages_short_path(self.prefix)
        bin_dir = get_bin_directory_short_path()

        def make_link_operation(source_path_info):
            # first part, same as parent class
            if source_path_info.prefix_placeholder:
                link_type = LinkType.copy
                prefix_placehoder = source_path_info.prefix_placeholder
                file_mode = source_path_info.file_mode
            elif source_path_info.no_link or source_path_info.path_type == PathType.softlink:
                link_type = LinkType.copy
                prefix_placehoder, file_mode = '', None
            else:
                link_type = requested_link_type
                prefix_placehoder, file_mode = '', None
            is_menu_file = bool(MENU_RE.match(source_path_info.path))

            source_short_path = source_path_info.path
            # second part, noarch python-specific
            if source_short_path.startswith('site-packages/'):
                dest_short_path = site_packages_dir + source_short_path.replace(
                    'site-packages', '', 1)
            elif source_short_path.startswith('python-scripts/'):
                dest_short_path = bin_dir + source_short_path.replace('python-scripts', '', 1)
            else:
                dest_short_path = source_short_path
            return LinkOperation(source_short_path, dest_short_path, link_type, prefix_placehoder,
                                 file_mode, is_menu_file)

        leaf_directories = get_leaf_directories(package_info.paths)
        directory_create_operations = (make_dir_operation(d) for d in leaf_directories)
        return tuple(make_link_operation(p) for p in package_info.paths)

    def _execute_link_operations(self, leaf_directories, link_operations):
        dest_short_paths = super(NoarchPythonPackageInstaller, self)._execute_link_operations(
            leaf_directories, link_operations)

        # create pyc files
        python_veresion = get_python_version_for_prefix(self.prefix)
        extra_pyc_paths = compile_missing_pyc(self.prefix, python_veresion,
                                              tuple(op.dest_short_path for op in link_operations))

        # create entry points
        entry_points = self.package_info.noarch.entry_points
        entry_point_paths = []
        for entry_point in entry_points:
            entry_point_paths.extend(create_entry_point(entry_point, self.prefix))

        return sorted(concatv(dest_short_paths, extra_pyc_paths, entry_point_paths))


class PackageUninstaller(object):

    def __init__(self, prefix, dist):
        self.prefix = prefix
        self.dist = dist
        self.linked_data = None

    def unlink(self):
        """
        Remove a package from the specified environment, it is an error if the
        package does not exist in the prefix.
        """
        log.debug("unlinking package %s", self.dist)
        run_script(self.prefix, self.dist, 'pre-unlink')

        # file system reads
        self.linked_data = linked_data(self.prefix)
        meta = load_meta(self.prefix, self.dist)

        # computations
        self._make_unlink_operations()
        # files = meta['files']


        dirs_with_removals = set()

        for f in meta['files']:
            if on_win and bool(MENU_RE.match(f)):
                # Always try to run this - it should not throw errors where menus do not exist
                # note that it will probably remove the file though; rm_rf shouldn't care
                make_menu(self.prefix, win_path_ok(f), remove=True)

            dirs_with_removals.add(dirname(f))
            rm_rf(join(self.prefix, win_path_ok(f)))

        # remove the meta-file last
        #   TODO: why last?  Won't that leave things more broken than removing it first?
        #   maybe should rename the file with an extra extension, then remove it once the
        #   operation is complete
        delete_linked_data(self.prefix, self.dist, delete=True)

        dirs_with_removals.add('conda-meta')  # in case there is nothing left
        directory_removal_candidates = (join(self.prefix, win_path_ok(d)) for d in
                                        sorted(explode_directories(dirs_with_removals),
                                               reverse=True))

        # remove empty directories; don't crash if we can't
        for d in directory_removal_candidates:
            if isdir(d) and not listdir(d):
                try:
                    rm_rf(d)
                except (IOError, OSError) as e:
                    log.debug("Failed to remove '%s'. %r", d, e)

        alt_files_path = join(self.prefix, 'conda-meta', self.dist.to_filename('.files'))
        if isfile(alt_files_path):
            rm_rf(alt_files_path)


def run_script(prefix, dist, action='post-link', env_prefix=None):
    """
    call the post-link (or pre-unlink) script, and return True on success,
    False on failure
    """
    if action == 'pre-link':
        warnings.warn("The package %s uses a pre-link script.\n"
                      "Pre-link scripts may be deprecated in the near future.")
    path = join(prefix, 'Scripts' if on_win else 'bin', '.%s-%s.%s' % (
            dist.dist_name,
            action,
            'bat' if on_win else 'sh'))
    if not isfile(path):
        return True
    if on_win:
        try:
            args = [os.environ['COMSPEC'], '/c', path]
        except KeyError:
            return False
    else:
        shell_path = '/bin/sh' if 'bsd' in sys.platform else '/bin/bash'
        args = [shell_path, path]
    env = os.environ.copy()
    name, version, _, _ = dist.quad
    build_number = dist.build_number
    env[str('ROOT_PREFIX')] = sys.prefix
    env[str('PREFIX')] = str(env_prefix or prefix)
    env[str('PKG_NAME')] = name
    env[str('PKG_VERSION')] = version
    env[str('PKG_BUILDNUM')] = build_number
    if action == 'pre-link':
        env[str('SOURCE_DIR')] = str(prefix)
    try:
        check_call(args, env=env)
    except CalledProcessError:
        return False
    else:
        return True
    finally:
        messages(prefix)


def messages(prefix):
    path = join(prefix, '.messages.txt')
    if isfile(path):
        with open(path) as fi:
            fh = sys.stderr if context.json else sys.stdout
            fh.write(fi.read())
        rm_rf(path)



class PathAction(object):

    def __init__(self, transaction_context, associated_package_info,
                 source_prefix, source_short_path,
                 target_prefix, target_short_path):
        self.transaction_context = transaction_context
        self.associated_package_info = associated_package_info
        self._source_prefix = source_prefix
        self._source_short_path = source_short_path
        self._target_prefix = target_prefix
        self._target_short_path = target_short_path

    @property
    def source_full_path(self):
        prfx, shrt_pth = self.source_prefix, self.source_short_path
        return join(prfx, win_path_ok(shrt_pth)) if prfx and shrt_pth else None

    @property
    def target_full_path(self):
        trgt, shrt_pth = self.target_prefix, self.target_short_path
        return join(trgt, win_path_ok(shrt_pth)) if trgt and shrt_pth else None

    @property
    def source_prefix(self):
        return self._source_prefix % self.transaction_context

    @property
    def source_short_path(self):
        return self._source_short_path % self.transaction_context

    @property
    def target_prefix(self):
        return self._target_prefix % self.transaction_context

    @property
    def target_short_path(self):
        return self._target_short_path % self.transaction_context

    def verify(self):
        raise NotImplementedError()

    def execute(self):
        raise NotImplementedError()

    def reverse(self):
        raise NotImplementedError()

    def cleanup(self):
        raise NotImplementedError()


class CreatePathAction(PathAction):
    # All CreatePathAction subclasses must create a SINGLE new path
    #   the short/in-prefix version of that path must be returned by execute()

    def verify(self):
        if getattr(self, 'link_type', None) == LinkType.directory:
            return
        if self.target_short_path in self.transaction_context['prefix_inventory']:
            raise VerificationError()
        self.transaction_context['prefix_inventory'].update(self.target_short_path)

    def cleanup(self):
        # create actions typically won't need cleanup
        pass


class RemovePathAction(PathAction):
    # remove actions won't have a source

    def __init__(self, transaction_context, associated_package_info,
                 target_prefix, target_short_path):
        super(RemovePathAction, self).__init__(transaction_context, associated_package_info,
                                               None, None, target_prefix, target_short_path)

    def verify(self):
        # inability to remove will trigger a rollback
        # can't definitely know if path can be removed until it's done
        pass


class UnlinkPathAction(RemovePathAction):

    def __init__(self, transaction_context, associated_package_info,
                 target_prefix, target_short_path):
        super(UnlinkPathAction, self).__init__(transaction_context, associated_package_info,
                                               target_prefix, target_short_path)
        path = join(target_prefix, target_short_path)
        self.unlink_path = path
        self.holding_path = path + '.c~'

    def verify(self):
        self.transaction_context['prefix_inventory'].pop(self.target_short_path, None)
        # technically not verification; this is mutating state of the prefix_inventory
        # however it's related to verification, because the functionality is used in other
        #   verification methods

    def execute(self):
        rename(self.unlink_path, self.holding_path)

    def reverse(self):
        rename(self.holding_path, self.unlink_path)

    def cleanup(self):
        rm_rf(self.holding_path)


class LinkPathAction(CreatePathAction):

    def __init__(self, transaction_context, associated_package_info,
                 extracted_package_dir, source_short_path,
                 target_prefix, target_short_path, link_type):
        super(LinkPathAction, self).__init__(transaction_context, associated_package_info,
                                             extracted_package_dir, source_short_path,
                                             target_prefix, target_short_path)
        self.link_type = link_type

    def verify(self):
        super(LinkPathAction, self).verify()
        if not self.link_type == LinkType.directory and not exists(self.source_full_path):
            raise VerificationError()

    def execute(self):
        create_link(self.source_full_path, self.target_full_path, self.link_type)

    def reverse(self):
        if self.link_type == LinkType.directory:
            maybe_rmdir_if_empty(self.target_full_path)
        else:
            rm_rf(self.target_full_path)


class PrefixReplaceLinkAction(LinkPathAction):

    def __init__(self, transaction_context, associated_package_info,
                 extracted_package_dir, source_short_path,
                 target_prefix, target_short_path, prefix_placeholder, file_mode):
        super(PrefixReplaceLinkAction, self).__init__(transaction_context, associated_package_info,
                                                      extracted_package_dir, source_short_path,
                                                      target_prefix, target_short_path,
                                                      LinkType.copy)
        assert prefix_placeholder
        self.prefix_placeholder = prefix_placeholder
        self.file_mode = file_mode

    def execute(self):
        super(PrefixReplaceLinkAction, self).execute()
        try:
            update_prefix(self.target_full_path, self.target_prefix, self.prefix_placeholder,
                          self.file_mode)
        except _PaddingError:
            raise PaddingError(self.target_full_path, self.prefix_placeholder,
                               len(self.prefix_placeholder))


class MakeMenuAction(CreatePathAction):

    def __init__(self, transaction_context, associated_package_info,
                 target_prefix, target_short_path):
        super(MakeMenuAction, self).__init__(transaction_context, associated_package_info,
                                             None, None, target_prefix, target_short_path)

    def verify(self):
        pass

    def execute(self):
        make_menu(self.target_prefix, self.target_short_path, remove=False)

    def reverse(self):
        make_menu(self.target_prefix, self.target_short_path, remove=True)


class RemoveMenuAction(RemovePathAction):

    def __init__(self, transaction_context, associated_package_info,
                 target_prefix, target_short_path):
        super(RemoveMenuAction, self).__init__(transaction_context, associated_package_info,
                                               target_prefix, target_short_path)

    def execute(self):
        make_menu(self.target_prefix, self.target_short_path, remove=False)

    def reverse(self):
        make_menu(self.target_prefix, self.target_short_path, remove=True)

    def cleanup(self):
        pass


class CompilePycAction(CreatePathAction):

    def __init__(self, transaction_context, associated_package_info, target_prefix,
                 source_short_path):
        super(CompilePycAction, self).__init__(transaction_context, associated_package_info,
                                               target_prefix, source_short_path,
                                               target_prefix, None)

    def execute(self):
        python_short_path = get_python_path(self.transaction_context['target_python_version'])
        python_full_path = join(self.target_prefix, win_path_ok(python_short_path))
        compile_pyc(python_full_path, self.source_full_path)

    def reverse(self):
        rm_rf(self.target_full_path)

    @property
    def target_short_path(self):
        return pyc_path(self._target_short_path, self.transaction_context['python_version'])


class CreatePythonEntryPointAction(CreatePathAction):
    # create_entry_point(entry_point, self.prefix)

    def __init__(self, transaction_context, associated_package_info,
                 target_prefix, target_short_path, module, func):
        args = (transaction_context, associated_package_info, None, None,
                target_prefix, target_short_path)
        super(CreatePythonEntryPointAction, self).__init__(*args)
        self.module = module
        self.func = func

    def execute(self):
        if on_win:
            create_windows_entry_point_py(self.target_full_path, self.module, self.func)
        else:
            python_short_path = get_python_path(self.transaction_context['target_python_version'])
            python_full_path = join(self.target_prefix, win_path_ok(python_short_path))
            create_unix_entry_point(self.target_full_path, python_full_path, self.module, self.func)

    def reverse(self):
        rm_rf(self.target_full_path)

    @property
    def target_short_path(self):
        return self._target_short_path + '-script.py' if on_win else self._target_short_path


class CreateCondaMetaAction(CreatePathAction):

    def __init__(self, transaction_context, associated_package_info, target_prefix, meta_record):
        target_short_path = 'conda-meta/' + associated_package_info.dist.to_filename('.json')
        super(CreateCondaMetaAction, self).__init__(transaction_context, associated_package_info,
                                                    None, None, target_prefix, target_short_path)
        self.meta_record = meta_record

    def execute(self):
        write_conda_meta_record(self.target_prefix, self.meta_record)
        set_linked_data(self.target_prefix, self.associated_package_info.dist.dist_name,
                        self.meta_record)

    def reverse(self):
        delete_linked_data(self.target_prefix, self.associated_package_info.dist, delete=False)
        rm_rf(self.target_full_path)


class RemoveCondaMetaAction(RemovePathAction):

    def __init__(self, transaction_context, associated_package_info,
                 target_prefix, target_short_path):
        super(RemoveCondaMetaAction, self).__init__(transaction_context, associated_package_info,
                                                    target_prefix, target_short_path)

    def execute(self):
        super(RemoveCondaMetaAction, self).execute()
        delete_linked_data(self.target_prefix, self.associated_package_info.dist, delete=False)

    def reverse(self):
        super(RemoveCondaMetaAction, self).reverse()
        with open(self.target_full_path, 'r') as fh:
            meta_record = Record(**json.loads(fh.read()))
        set_linked_data(self.target_prefix, self.associated_package_info.dist.dist_name,
                        meta_record)





