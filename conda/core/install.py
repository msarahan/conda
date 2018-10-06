# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

import os
import re
import sys
import warnings
from collections import namedtuple
from logging import getLogger
from os.path import isfile, join
from subprocess import CalledProcessError, check_call

from .package_cache import is_extracted, read_url
from ..base.constants import LinkType
from ..base.context import context
from ..common.path import get_leaf_directories
from ..core.linked_data import set_linked_data, get_python_version_for_prefix
from ..exceptions import CondaOSError, LinkError, PaddingError
from ..gateways.disk.create import link as create_link, make_menu, mkdir_p, write_conda_meta_record, \
    compile_missing_pyc
from ..gateways.disk.delete import rm_rf
from ..gateways.disk.read import collect_all_info_for_package, yield_lines
from ..gateways.disk.update import _PaddingError, update_prefix
from ..models.record import Link
from ..utils import on_win

try:
    from cytoolz.itertoolz import concatv, groupby
except ImportError:
    from .._vendor.toolz.itertoolz import concatv, groupby  # NOQA

log = getLogger(__name__)

MENU_RE = re.compile(r'^menu/.*\.json$', re.IGNORECASE)
LinkOperation = namedtuple('LinkOperation',
                           ('source_short_path', 'dest_short_path', 'link_type',
                            'prefix_placeholder', 'file_mode', 'is_menu_file'))


class PackageInstaller(object):

    def __init__(self, prefix, index, dist):
        self.prefix = prefix
        self.index = index
        self.dist = dist

    def link(self, requested_link_type=LinkType.hard_link):
        log.debug("linking package %s with link type %s", self.dist, requested_link_type)
        extracted_package_dir = is_extracted(self.dist)
        assert extracted_package_dir is not None
        log.debug("linking package:\n"
                  "  prefix=%s\n"
                  "  source=%s\n"
                  "  link_type=%s\n",
                  self.prefix, extracted_package_dir, requested_link_type)

        # filesystem read actions
        #   do all filesystem reads necessaary for the rest of the linking for this package
        package_info = collect_all_info_for_package(extracted_package_dir)
        url = read_url(self.dist)

        # simple processing
        operations = self._make_link_operations(self.prefix, requested_link_type, package_info)
        leaf_directories = get_leaf_directories(join(self.prefix, op.dest_short_path)
                                                for op in operations)

        # run pre-link script
        if not run_script(extracted_package_dir, self.dist, 'pre-link', self.prefix):
            raise LinkError('Error: pre-link failed: %s' % self.dist)

        dest_short_paths = self._execute_link_operations(self.prefix, leaf_directories, operations)

        # run post-link script
        if not run_script(self.prefix, self.dist, 'post-link'):
            raise LinkError("Error: post-link failed for: %s" % self.dist)

        # create package's prefix/conda-meta file
        meta_record = self._create_meta(extracted_package_dir, dest_short_paths,
                                        requested_link_type, package_info, url)
        write_conda_meta_record(self.prefix, meta_record)
        set_linked_data(self.prefix, self.dist.dist_name, meta_record)

    def _make_link_operations(self, requested_link_type, package_info):
        def make_link_operation(source_short_path):
            if source_short_path in package_info.has_prefix_files:
                link_type = LinkType.copy
                prefix_placehoder, file_mode = package_info.has_prefix_files[source_short_path]
            elif source_short_path in concatv(package_info.no_link, package_info.soft_links):
                link_type = LinkType.copy
                prefix_placehoder, file_mode = '', None
            else:
                link_type = requested_link_type
                prefix_placehoder, file_mode = '', None
            is_menu_file = bool(MENU_RE.match(source_short_path))
            dest_short_path = source_short_path
            return LinkOperation(source_short_path, dest_short_path, link_type, prefix_placehoder,
                                 file_mode, is_menu_file)

        return (make_link_operation(p) for p in package_info.files)

    def _execute_link_operations(self, leaf_directories, link_operations):
        dest_short_paths = []

        # Step 1. Make all directories
        for leaf_directory in leaf_directories:
            mkdir_p(leaf_directory)

        # Step 2. Do the actual file linking
        for op in link_operations:
            try:
                create_link(op.source_path, op.dest_path, op.link_type)
                dest_short_paths.append(op.short_path)
            except OSError as e:
                raise CondaOSError('failed to link (src=%r, dst=%r, type=%r, error=%r)' %
                                   (op.source_path, op.dest_path, op.link_type, e))

        # Step 3. Replace prefix placeholder within all necessary files
        # Step 4. Make shortcuts on Windows
        for op in link_operations:
            if op.prefix_placeholder:
                try:
                    update_prefix(op.dest_path, self.prefix, op.prefix_placeholder, op.file_mode)
                except _PaddingError:
                    raise PaddingError(op.dest_path, op.prefix_placeholder,
                                       len(op.prefix_placeholder))
            if on_win and op.is_menu_file and context.shortcuts:
                make_menu(self.prefix, op.short_path, remove=False)

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

    def _create_meta(self, extracted_package_dir, dest_short_paths, requested_link_type,
                      package_info, url):
        """
        Create the conda metadata, in a given prefix, for a given package.
        """
        meta_dict = self.index.get(self.dist, {})
        meta_dict['url'] = url

        # alt_files_path is a hack for python_noarch
        alt_files_path = join(self.prefix, 'conda-meta', self.dist.to_filename('.files'))
        meta_dict['files'] = (list(yield_lines(alt_files_path)) if isfile(alt_files_path)
                              else dest_short_paths)
        meta_dict['link'] = Link(source=extracted_package_dir, type=requested_link_type)
        if 'icon' in meta_dict:
            meta_dict['icondata'] = package_info.icondata

        meta = package_info.index_json_record
        meta.update(meta_dict)

        return meta


class NoarchPythonPackageInstaller(PackageInstaller):

    def _make_link_operations(self, requested_link_type, package_info):
        site_packages_dir = NoarchPythonPackageInstaller.get_site_packages_dir(self.prefix)
        bin_dir = NoarchPythonPackageInstaller.get_bin_dir(self.prefix)

        def make_link_operation(source_short_path):
            # first part, same as parent class
            if source_short_path in package_info.has_prefix_files:
                link_type = LinkType.copy
                prefix_placehoder, file_mode = package_info.has_prefix_files[source_short_path]
            elif source_short_path in concatv(package_info.no_link, package_info.soft_links):
                link_type = LinkType.copy
                prefix_placehoder, file_mode = '', None
            else:
                link_type = requested_link_type
                prefix_placehoder, file_mode = '', None
            is_menu_file = bool(MENU_RE.match(source_short_path))

            # second part, noarch python-specific
            if source_short_path.startswith('site-packages/'):
                dest_short_path = site_packages_dir + source_short_path
            elif source_short_path.startswith('python-scripts/'):
                dest_short_path = bin_dir + source_short_path.replace('python-scripts/', '', 1)
            else:
                dest_short_path = source_short_path
            return LinkOperation(source_short_path, dest_short_path, link_type, prefix_placehoder,
                                 file_mode, is_menu_file)

        return (make_link_operation(p) for p in package_info.files)

    def _execute_link_operations(self, leaf_directories, link_operations):
        dest_short_paths = super(NoarchPythonPackageInstaller, self)._execute_link_operations(
            leaf_directories, link_operations)

        # create pyc files
        python_veresion = get_python_version_for_prefix(self.prefix)
        extra_pyc_paths = compile_missing_pyc(self.prefix, python_veresion,
                                              (op.dest_short_path for op in link_operations))
        entry_point_paths = create_entry_points(src_dir, bin_dir, prefix)

        return sorted(dest_short_paths, extra_pyc_paths, entry_point_paths)


    @staticmethod
    def get_site_packages_dir(prefix):
        if on_win:
            return join(prefix, 'Lib')
        else:
            return join(prefix, 'lib', 'python%s' % get_python_version_for_prefix(prefix))

    @staticmethod
    def get_bin_dir(prefix):
        if on_win:
            return join(prefix, 'Scripts')
        else:
            return join(prefix, 'bin')




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
    build_number = dist.build_number()
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
