# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from collections import defaultdict, namedtuple
from logging import getLogger
import os
from os.path import dirname, isdir, join
from subprocess import CalledProcessError
import sys
from traceback import format_exc
import warnings

from .linked_data import PrefixData, get_python_version_for_prefix, linked_data as get_linked_data
from .package_cache import PackageCache
from .path_actions import (CompilePycAction, CreateApplicationEntryPointAction,
                           CreateApplicationSoftlinkAction, CreateLinkedPackageRecordAction,
                           CreateNonadminAction, CreatePythonEntryPointAction, LinkPathAction,
                           MakeMenuAction, RegisterEnvironmentLocationAction,
                           RegisterPrivateEnvAction, RemoveLinkedPackageRecordAction,
                           RemoveMenuAction, UnlinkPathAction, UnregisterEnvironmentLocationAction,
                           UnregisterPrivateEnvAction, UpdateHistoryAction)
from .. import CondaError, CondaMultiError, conda_signal_handler
from .._vendor.auxlib.collection import first
from .._vendor.auxlib.ish import dals
from ..base.context import context
from ..common.compat import ensure_text_type, iteritems, itervalues, odict, on_win, text_type
from ..common.path import (explode_directories, get_all_directories, get_major_minor_version,
                           get_python_site_packages_short_path)
from ..common.signals import signal_handler
from ..exceptions import (KnownPackageClobberError, LinkError, RemoveError,
                          SharedLinkPathClobberError, UnknownPackageClobberError, maybe_raise)
from ..gateways.disk import mkdir_p
from ..gateways.disk.delete import rm_rf
from ..gateways.disk.read import isfile, lexists, read_package_info
from ..gateways.disk.test import hardlink_supported, softlink_supported
from ..gateways.subprocess import subprocess_call
from ..models.dist import Dist
from ..models.enums import LinkType
from ..resolve import MatchSpec

try:
    from cytoolz.itertoolz import concat, concatv, groupby, interleave, take
except ImportError:  # pragma: no cover
    from .._vendor.toolz.itertoolz import concat, concatv, groupby, interleave, take  # NOQA

log = getLogger(__name__)


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


def make_unlink_actions(transaction_context, target_prefix, linked_package_data):
    # no side effects in this function!
    unlink_path_actions = tuple(UnlinkPathAction(transaction_context, linked_package_data,
                                                 target_prefix, trgt)
                                for trgt in linked_package_data.files)

    remove_menu_actions = RemoveMenuAction.create_actions(transaction_context,
                                                          linked_package_data,
                                                          target_prefix)

    meta_short_path = '%s/%s' % ('conda-meta', Dist(linked_package_data).to_filename('.json'))
    remove_conda_meta_actions = (RemoveLinkedPackageRecordAction(transaction_context,
                                                                 linked_package_data,
                                                                 target_prefix, meta_short_path),)

    _all_d = get_all_directories(axn.target_short_path for axn in unlink_path_actions)
    all_directories = sorted(explode_directories(_all_d, already_split=True), reverse=True)
    directory_remove_actions = tuple(UnlinkPathAction(transaction_context, linked_package_data,
                                                      target_prefix, d, LinkType.directory)
                                     for d in all_directories)

    unregister_private_package_actions = UnregisterPrivateEnvAction.create_actions(
        transaction_context, linked_package_data, target_prefix
    )

    return tuple(concatv(
        remove_menu_actions,
        unlink_path_actions,
        directory_remove_actions,
        unregister_private_package_actions,
        remove_conda_meta_actions,
    ))


class UnlinkLinkTransaction(object):

    @classmethod
    def create_from_dists(cls, index, target_prefix, unlink_dists, link_dists):
        # This constructor method helps to patch into the 'plan' framework
        lnkd_pkg_data = (load_meta(target_prefix, dist) for dist in unlink_dists)
        # TODO: figure out if this filter shouldn't be an assert not None
        linked_packages_data_to_unlink = tuple(lpd for lpd in lnkd_pkg_data if lpd)

        log.debug("instantiating UnlinkLinkTransaction with\n"
                  "  target_prefix: %s\n"
                  "  unlink_dists:\n"
                  "    %s\n"
                  "  link_dists:\n"
                  "    %s\n",
                  target_prefix,
                  '\n    '.join(text_type(d) for d in unlink_dists),
                  '\n    '.join(text_type(d) for d in link_dists))

        pkg_dirs_to_link = tuple(PackageCache.get_entry_to_link(dist).extracted_package_dir
                                 for dist in link_dists)
        assert all(pkg_dirs_to_link)
        packages_info_to_link = tuple(read_package_info(index[dist], pkg_dir)
                                      for dist, pkg_dir in zip(link_dists, pkg_dirs_to_link))


class UnlinkLinkTransaction(object):

    def __init__(self, *setups):
        self.prefix_setups = odict((stp.target_prefix, stp) for stp in setups)
        self.prefix_action_groups = odict()

        for stp in itervalues(self.prefix_setups):
            log.debug("instantiating UnlinkLinkTransaction with\n"
                      "  target_prefix: %s\n"
                      "  unlink_dists:\n"
                      "    %s\n"
                      "  link_dists:\n"
                      "    %s\n",
                      stp.target_prefix,
                      '\n    '.join(text_type(d) for d in stp.unlink_dists),
                      '\n    '.join(text_type(d) for d in stp.link_dists))

        self._prepared = False
        self._verified = False

    @property
    def nothing_to_do(self):
        return not any((stp.unlink_dists or stp.link_dists)
                       for stp in itervalues(self.prefix_setups))

    def get_pfe(self):
        from .package_cache import ProgressiveFetchExtract
        if not self.prefix_setups:
            return ProgressiveFetchExtract({}, ())
        else:
            index = next(itervalues(self.prefix_setups)).index
            link_dists = set(concat(stp.link_dists for stp in itervalues(self.prefix_setups)))
            return ProgressiveFetchExtract(index, link_dists)

    def prepare(self):
        if self._prepared:
            return

        for stp in itervalues(self.prefix_setups):
            grps = self._prepare(stp.index, stp.target_prefix, stp.unlink_dists, stp.link_dists,
                                 stp.command_action, stp.requested_specs)
            self.prefix_action_groups[stp.target_prefix] = PrefixActionGroup(*grps)

        self._prepared = True

    def verify(self):
        if not self._prepared:
            self.prepare()

        if context.skip_safety_checks:
            self._verified = True
            return

        exceptions = self._verify(self.prefix_setups, self.prefix_action_groups)

        if exceptions:
            maybe_raise(CondaMultiError(exceptions), context)
        else:
            log.info(exceptions)

        self._verified = True

    def execute(self):
        if not self._verified:
            self.verify()
        self._execute(tuple(concat(interleave(itervalues(self.prefix_action_groups)))))

    @classmethod
    def _prepare(cls, index, target_prefix, unlink_dists, link_dists, command_action,
                 requested_specs):

        # make sure prefix directory exists
        if not isdir(target_prefix):
            try:
                mkdir_p(target_prefix)
            except (IOError, OSError) as e:
                log.debug(repr(e))
                raise CondaError("Unable to create prefix directory '%s'.\n"
                                 "Check that you have sufficient permissions."
                                 "" % target_prefix)

        # gather information from disk and caches
        prefix_data = PrefixData(target_prefix)
        linked_pkgs_data_to_unlink = (prefix_data.get(dist.name) for dist in unlink_dists)
        # NOTE: load_meta can return None
        # TODO: figure out if this filter shouldn't be an assert not None
        linked_pkgs_data_to_unlink = tuple(lpd for lpd in linked_pkgs_data_to_unlink if lpd)
        pkg_dirs_to_link = tuple(PackageCache.get_entry_to_link(index[dist]).extracted_package_dir
                                 for dist in link_dists)
        assert all(pkg_dirs_to_link)
        packages_info_to_link = tuple(read_package_info(index[dist], pkg_dir)
                                      for dist, pkg_dir in zip(link_dists, pkg_dirs_to_link))

        link_types = tuple(determine_link_type(pkg_info.extracted_package_dir, target_prefix)
                           for pkg_info in packages_info_to_link)

        # make all the path actions
        # no side effects allowed when instantiating these action objects
        transaction_context = dict()
        python_version = cls.get_python_version(target_prefix,
                                                linked_packages_data_to_unlink,
                                                packages_info_to_link)
        transaction_context['target_python_version'] = python_version
        sp = get_python_site_packages_short_path(python_version)
        transaction_context['target_site_packages_short_path'] = sp

        unlink_action_groups = tuple(
            ActionGroup('unlink', lnkd_pkg_data, make_unlink_actions(transaction_context,
                                                                     target_prefix,
                                                                     lnkd_pkg_data),
                        target_prefix)
            for lnkd_pkg_data in linked_packages_data_to_unlink
        )

        if unlink_action_groups:
            axns = UnregisterEnvironmentLocationAction(transaction_context, target_prefix),
            unregister_action_groups = ActionGroup('unregister', None, axns, target_prefix),
        else:
            unregister_action_groups = ()

        matchspecs_for_link_dists = match_specs_to_dists(packages_info_to_link, requested_specs)
        link_action_groups = tuple(
            ActionGroup('link', pkg_info, cls.make_link_actions(transaction_context, pkg_info,
                                                                target_prefix, lt, spec),
                        target_prefix)
            for pkg_info, lt, spec in zip(packages_info_to_link, link_types,
                                          matchspecs_for_link_dists)
        )

        history_actions = UpdateHistoryAction.create_actions(
            transaction_context, target_prefix, requested_specs, command_action)
        if link_action_groups:
            register_actions = RegisterEnvironmentLocationAction(transaction_context,
                                                                 target_prefix),
        else:
            register_actions = ()

        register_action_groups = ActionGroup('register', None,
                                             register_actions + history_actions,
                                             target_prefix),

        return PrefixActionGroup(
            unlink_action_groups,
            unregister_action_groups,
            link_action_groups,
            register_action_groups,
        )

    @staticmethod
    def _verify_individual_level(prefix_action_group):
        all_actions = concat(axngroup.actions
                             for action_groups in prefix_action_group
                             for axngroup in action_groups)

        # run all per-action verify methods
        #   one of the more important of these checks is to verify that a file listed in
        #   the packages manifest (i.e. info/files) is actually contained within the package
        for axn in all_actions:
            if axn.verified:
                continue
            error_result = axn.verify()
            if error_result:
                log.debug("Verification error in action %s", axn)
                log.debug(format_exc())
                yield error_result

    @staticmethod
    def _verify_prefix_level(target_prefix, prefix_action_group):
        # further verification of the whole transaction
        # for each path we are creating in link_actions, we need to make sure
        #   1. each path either doesn't already exist in the prefix, or will be unlinked
        #   2. there's only a single instance of each path
        #   3. if the target is a private env, leased paths need to be verified
        #   4. make sure conda-meta/history file is writable
        #   5. make sure envs/catalog.json is writable; done with RegisterEnvironmentLocationAction
        #   6. make sure we're not removing pinned packages without no-pin flag
        # TODO: 3, 4, 6

        unlink_action_groups = (axn_grp
                                for action_groups in prefix_action_group
                                for axn_grp in action_groups
                                if axn_grp.type == 'unlink')
        link_action_groups = (axn_grp
                              for action_groups in prefix_action_group
                              for axn_grp in action_groups
                              if axn_grp.type == 'link')

        # paths are case-insensitive on windows apparently
        lower_on_win = lambda p: p.lower() if on_win else p
        unlink_paths = set(lower_on_win(axn.target_short_path)
                           for grp in unlink_action_groups
                           for axn in grp.actions
                           if isinstance(axn, UnlinkPathAction))
        # we can get all of the paths being linked by looking only at the
        #   CreateLinkedPackageRecordAction actions
        create_lpr_actions = (axn
                              for grp in link_action_groups
                              for axn in grp.actions
                              if isinstance(axn, CreateLinkedPackageRecordAction))

        # Verification 1. each path either doesn't already exist in the prefix, or will be unlinked
        link_paths_dict = defaultdict(list)
        for axn in create_lpr_actions:
            for path in axn.linked_package_record.files:
                path = lower_on_win(path)
                link_paths_dict[path].append(axn)
                if path not in unlink_paths and lexists(join(target_prefix, path)):
                    # we have a collision; at least try to figure out where it came from
                    linked_data = get_linked_data(target_prefix)
                    colliding_linked_package_record = first(
                        (lpr for lpr in itervalues(linked_data)),
                        key=lambda lpr: path in lpr.files
                    )
                    if colliding_linked_package_record:
                        yield KnownPackageClobberError(path, Dist(axn.linked_package_record),
                                                       Dist(colliding_linked_package_record),
                                                       context)
                    else:
                        yield UnknownPackageClobberError(path, Dist(axn.linked_package_record),
                                                         context)

        # Verification 2. there's only a single instance of each path
        for path, axns in iteritems(link_paths_dict):
            if len(axns) > 1:
                yield SharedLinkPathClobberError(
                    path, tuple(Dist(axn.linked_package_record) for axn in axns), context
                )

    @staticmethod
    def _verify_transaction_level(prefix_setups):
        # 1. make sure we're not removing conda or a conda dependency from conda's env
        # 2. make sure we're not removing a conda dependency from conda's env
        conda_prefixes = (join(context.root_prefix, 'envs', '_conda_'), context.root_prefix)
        conda_setups = tuple(setup for setup in itervalues(prefix_setups)
                             if setup.target_prefix in conda_prefixes)

        conda_unlinked = any(d.name == 'conda'
                             for setup in conda_setups
                             for d in setup.unlink_dists)

        conda_dist, conda_final_setup = next(
            ((d, setup)
             for setup in conda_setups
             for d in setup.link_dists
             if d.name == 'conda'),
            (None, None)
        )

        if conda_unlinked and conda_final_setup is None:
            # means conda is being unlinked and not re-linked anywhere
            # this should never be able to be skipped, even with --force
            yield RemoveError("This operation will remove conda without replacing it with\n"
                              "another version of conda.")

        if conda_final_setup is None:
            # means we're not unlinking then linking a new package, so look up current conda record
            conda_final_prefix = context.conda_prefix
            pkg_names_already_lnkd = tuple(rec.name for rec in get_linked_data(conda_final_prefix)
                                           or ())
            pkg_names_being_lnkd = ()
            pkg_names_being_unlnkd = ()
            _prefix_records = itervalues(get_linked_data(conda_final_prefix))
            conda_linked_depends = next((record.depends for record in _prefix_records
                                         if record.name == 'conda'), ())
        else:
            conda_final_prefix = conda_final_setup.target_prefix
            pkg_names_already_lnkd = tuple(rec.name for rec in get_linked_data(conda_final_prefix)
                                           or ())
            pkg_names_being_lnkd = tuple(d.name for d in conda_final_setup.link_dists or ())
            pkg_names_being_unlnkd = tuple(d.name for d in conda_final_setup.unlink_dists or ())
            conda_linked_depends = conda_final_setup.index[conda_dist].depends

        for conda_dependency in conda_linked_depends:
            dep_name = MatchSpec(conda_dependency).name
            if dep_name not in pkg_names_being_lnkd and (dep_name not in pkg_names_already_lnkd
                                                         or dep_name in pkg_names_being_unlnkd):
                # equivalent to not (dep_name in pkg_names_being_lnkd or
                #  (dep_name in pkg_names_already_lnkd and dep_name not in pkg_names_being_unlnkd))
                yield RemoveError("'%s' is a dependency of conda and cannot be removed from\n"
                                  "conda's operating environment." % dep_name)

    @classmethod
    def _verify(cls, prefix_setups, prefix_action_groups):
        exceptions = tuple(exc for exc in concatv(
            concat(cls._verify_individual_level(prefix_group)
                   for prefix_group in itervalues(prefix_action_groups)),
            concat(cls._verify_prefix_level(target_prefix, prefix_group)
                   for target_prefix, prefix_group in iteritems(prefix_action_groups)),
            cls._verify_transaction_level(prefix_setups),
        ) if exc)
        return exceptions

        if exceptions:
            maybe_raise(CondaMultiError(exceptions), context)
        else:
            log.info(exceptions)

        self._verified = True

    def execute(self):
        if not self._verified:
            self.verify()

        assert not context.dry_run
        # make sure prefix directory exists
        if not isdir(self.target_prefix):
            try:
                mkdir_p(self.target_prefix)
            except (IOError, OSError) as e:
                log.debug(repr(e))
                raise CondaError("Unable to create prefix directory '%s'.\n"
                                 "Check that you have sufficient permissions."
                                 "" % self.target_prefix)

        with signal_handler(conda_signal_handler):
            pkg_idx = 0
            try:
                for pkg_idx, axngroup in enumerate(all_action_groups):
                    cls._execute_actions(pkg_idx, axngroup)
            except Exception as execute_multi_exc:
                # reverse all executed packages except the one that failed
                rollback_excs = []
                if context.rollback_enabled:
                    failed_pkg_idx = pkg_idx
                    reverse_actions = reversed(tuple(enumerate(
                        take(failed_pkg_idx, all_action_groups)
                    )))
                    for pkg_idx, axngroup in reverse_actions:
                        excs = cls._reverse_actions(pkg_idx, axngroup)
                        rollback_excs.extend(excs)

                raise CondaMultiError(tuple(concatv(
                    (execute_multi_exc.errors
                     if isinstance(execute_multi_exc, CondaMultiError)
                     else (execute_multi_exc,)),
                    rollback_excs,
                )))
            else:
                for axngroup in all_action_groups:
                    for action in axngroup.actions:
                        action.cleanup()

    @staticmethod
    def _execute_actions(pkg_idx, axngroup):
        target_prefix = axngroup.target_prefix
        axn_idx, action, is_unlink = 0, None, axngroup.type == 'unlink'
        pkg_data = axngroup.pkg_data
        dist = pkg_data and Dist(pkg_data)

        if not isdir(join(target_prefix, 'conda-meta')):
            mkdir_p(join(target_prefix, 'conda-meta'))

        try:
            if axngroup.type == 'unlink':
                log.info("===> UNLINKING PACKAGE: %s <===\n"
                         "  prefix=%s\n", dist, target_prefix)

            elif axngroup.type == 'link':
                log.info("===> LINKING PACKAGE: %s <===\n"
                         "  prefix=%s\n"
                         "  source=%s\n", dist, target_prefix, pkg_data.extracted_package_dir)

            if axngroup.type in ('unlink', 'link'):
                run_script(target_prefix if is_unlink else pkg_data.extracted_package_dir,
                           dist,
                           'pre-unlink' if is_unlink else 'pre-link',
                           target_prefix)
            for axn_idx, action in enumerate(axngroup.actions):
                action.execute()
            if axngroup.type in ('unlink', 'link'):
                run_script(target_prefix, Dist(pkg_data),
                           'post-unlink' if is_unlink else 'post-link')
        except Exception as e:  # this won't be a multi error
            # reverse this package
            log.debug("Error in action #%d for pkg_idx #%d %r", axn_idx, pkg_idx, action)
            log.debug(format_exc())
            reverse_excs = ()
            if context.rollback_enabled:
                log.error("An error occurred while %s package '%s'.\n"
                          "%r\n"
                          "Attempting to roll back.\n",
                          'uninstalling' if is_unlink else 'installing', Dist(pkg_data), e)
                reverse_excs = UnlinkLinkTransaction._reverse_actions(
                    pkg_idx, axngroup, reverse_from_idx=axn_idx
                )
            raise CondaMultiError(tuple(concatv(
                (e,),
                reverse_excs,
            )))

    @staticmethod
    def _reverse_actions(pkg_idx, axngroup, reverse_from_idx=-1):
        target_prefix = axngroup.target_prefix

        # reverse_from_idx = -1 means reverse all actions
        pkg_data = axngroup.pkg_data
        dist = pkg_data and Dist(pkg_data)

        if axngroup.type == 'unlink':
            log.info("===> REVERSING PACKAGE UNLINK: %s <===\n"
                     "  prefix=%s\n", dist, target_prefix)

        elif axngroup.type == 'link':
            log.info("===> REVERSING PACKAGE LINK: %s <===\n"
                     "  prefix=%s\n", dist, target_prefix)

        log.debug("reversing pkg_idx #%d from axn_idx #%d", pkg_idx, reverse_from_idx)

        exceptions = []
        if reverse_from_idx < 0:
            reverse_actions = axngroup.actions
        else:
            reverse_actions = axngroup.actions[:reverse_from_idx+1]
        for axn_idx, action in reversed(tuple(enumerate(reverse_actions))):
            try:
                action.reverse()
            except Exception as e:
                log.debug("action.reverse() error in action #%d for pkg_idx #%d %r", axn_idx,
                          pkg_idx, action)
                log.debug(format_exc())
                exceptions.append(e)
        return exceptions

    @staticmethod
    def get_python_version(target_prefix, linked_packages_data_to_unlink, packages_info_to_link):
        # this method determines the python version that will be present at the
        # end of the transaction
        linking_new_python = next((package_info for package_info in packages_info_to_link
                                   if package_info.index_json_record.name == 'python'),
                                  None)
        if linking_new_python:
            # is python being linked? we're done
            full_version = linking_new_python.index_json_record.version
            assert full_version
            return get_major_minor_version(full_version)

        # is python already linked and not being unlinked? that's ok too
        linked_python_version = get_python_version_for_prefix(target_prefix)
        if linked_python_version:
            find_python = (lnkd_pkg_data for lnkd_pkg_data in linked_packages_data_to_unlink
                           if lnkd_pkg_data.name == 'python')
            unlinking_this_python = next(find_python, None)
            if unlinking_this_python is None:
                # python is not being unlinked
                return linked_python_version

        # there won't be any python in the finished environment
        return None

    @staticmethod
    def make_link_actions(transaction_context, package_info, target_prefix, requested_link_type,
                          requested_spec):
        required_quad = transaction_context, package_info, target_prefix, requested_link_type

        file_link_actions = LinkPathAction.create_file_link_actions(*required_quad)
        create_directory_actions = LinkPathAction.create_directory_actions(
            *required_quad, file_link_actions=file_link_actions
        )
        create_nonadmin_actions = CreateNonadminAction.create_actions(*required_quad)
        create_menu_actions = MakeMenuAction.create_actions(*required_quad)

        python_entry_point_actions = CreatePythonEntryPointAction.create_actions(*required_quad)
        compile_pyc_actions = CompilePycAction.create_actions(*required_quad,
                                                              file_link_actions=file_link_actions)

        if requested_spec:
            application_entry_point_actions = CreateApplicationEntryPointAction.create_actions(
                *required_quad
            )
            application_softlink_actions = CreateApplicationSoftlinkAction.create_actions(
                *required_quad
            )
        else:
            application_entry_point_actions = ()
            application_softlink_actions = ()

        all_target_short_paths = tuple(axn.target_short_path for axn in concatv(
            file_link_actions,
            python_entry_point_actions,
            compile_pyc_actions,
        ))

        leased_paths = tuple(axn.leased_path_entry for axn in concatv(
            application_entry_point_actions,
            application_softlink_actions,
        ))

        meta_create_actions = CreateLinkedPackageRecordAction.create_actions(
            *required_quad, all_target_short_paths=all_target_short_paths,
            leased_paths=leased_paths
        )

        if requested_spec:
            register_private_env_actions = RegisterPrivateEnvAction.create_actions(
                transaction_context, package_info, target_prefix, requested_spec, leased_paths
            )
        else:
            register_private_env_actions = ()

        # the ordering here is significant
        return tuple(concatv(
            meta_create_actions,
            create_directory_actions,
            file_link_actions,
            create_nonadmin_actions,
            python_entry_point_actions,
            compile_pyc_actions,
            create_menu_actions,
            application_entry_point_actions,
            register_private_env_actions,
        ))

    def make_legacy_action_groups(self, pfe):
        legacy_action_groups = []

        for q, (prefix, setup) in enumerate(iteritems(self.prefix_setups)):
            actions = defaultdict(list)
            if q == 0:
                pfe.prepare()
                for axn in pfe.cache_actions:
                    actions['FETCH'].append(Dist(axn.url))

            actions['PREFIX'] = setup.target_prefix
            for dist in setup.unlink_dists:
                actions['UNLINK'].append(dist)
            for dist in setup.link_dists:
                actions['LINK'].append(dist)

            legacy_action_groups.append(actions)

        return legacy_action_groups

    def display_actions(self, pfe):
        from ..plan import display_actions
        legacy_action_groups = self.make_legacy_action_groups(pfe)

        for actions, (prefix, setup) in zip(legacy_action_groups, iteritems(self.prefix_setups)):
            display_actions(actions, setup.index, show_channel_urls=context.show_channel_urls)

        return legacy_action_groups


def run_script(prefix, dist, action='post-link', env_prefix=None):
    """
    call the post-link (or pre-unlink) script, and return True on success,
    False on failure
    """
    path = join(prefix,
                'Scripts' if on_win else 'bin',
                '.%s-%s.%s' % (dist.name, action, 'bat' if on_win else 'sh'))
    if not isfile(path):
        return True

    env = os.environ.copy()

    if action == 'pre-link':  # pragma: no cover
        # old no-arch support; deprecated
        is_old_noarch = False
        try:
            with open(path) as f:
                script_text = ensure_text_type(f.read())
            if ((on_win and "%PREFIX%\\python.exe %SOURCE_DIR%\\link.py" in script_text)
                    or "$PREFIX/bin/python $SOURCE_DIR/link.py" in script_text):
                is_old_noarch = True
        except Exception as e:
            import traceback
            log.debug(e)
            log.debug(traceback.format_exc())

        env['SOURCE_DIR'] = prefix
        if not is_old_noarch:
            warnings.warn(dals("""
            Package %s uses a pre-link script. Pre-link scripts are potentially dangerous.
            This is because pre-link scripts have the ability to change the package contents in the
            package cache, and therefore modify the underlying files for already-created conda
            environments.  Future versions of conda may deprecate and ignore pre-link scripts.
            """) % dist)

    if on_win:
        try:
            command_args = [os.environ[str('COMSPEC')], '/c', path]
        except KeyError:
            log.info("failed to run %s for %s due to COMSPEC KeyError", action, dist)
            return False
    else:
        shell_path = 'sh' if 'bsd' in sys.platform else 'bash'
        command_args = [shell_path, "-x", path]

    env['ROOT_PREFIX'] = context.root_prefix
    env['PREFIX'] = env_prefix or prefix
    env['PKG_NAME'] = dist.name
    env['PKG_VERSION'] = dist.version
    env['PKG_BUILDNUM'] = dist.build_number
    env['PATH'] = os.pathsep.join((dirname(path), env.get('PATH', '')))

    try:
        log.debug("for %s at %s, executing script: $ %s",
                  dist, env['PREFIX'], ' '.join(command_args))
        subprocess_call(command_args, env=env, path=dirname(path))
    except CalledProcessError as e:  # pragma: no cover
        m = messages(prefix)
        if action in ('pre-link', 'post-link'):
            if 'openssl' in text_type(dist):
                # this is a hack for conda-build string parsing in the conda_build/build.py
                #   create_env function
                message = "%s failed for: %s" % (action, dist)
            else:
                message = dals("""
                %s script failed for package %s
                running your command again with `-v` will provide additional information
                location of failed script: %s
                ==> script messages <==
                %s
                """) % (action, dist, path, m or "<None>")
            raise LinkError(message)
        else:
            log.warn("%s script failed for package %s\n"
                     "consider notifying the package maintainer", action, dist)
            return False
    else:
        messages(prefix)
        return True


def messages(prefix):
    path = join(prefix, '.messages.txt')
    try:
        if isfile(path):
            with open(path) as fi:
                m = fi.read()
                print(m, file=sys.stderr if context.json else sys.stdout)
                return m
    finally:
        rm_rf(path)
