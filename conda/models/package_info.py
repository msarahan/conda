# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from logging import getLogger

from .enums import FileMode
from .index_record import IndexRecord
from .._vendor.auxlib.entity import (BooleanField, ComposableField, Entity, EnumField,
                                     IntegerField, ListField, StringField, ImmutableEntity)
from ..common.compat import string_types
from ..models.channel import Channel
from ..models.enums import PathType

log = getLogger(__name__)


class Noarch(Entity):
    type = StringField()
    entry_points = ListField(string_types, required=False)


class PreferredEnv(Entity):
    name = StringField()
    executable_paths = ListField(string_types, required=False)


class PackageMetadata(Entity):
    # from info/package_metadata.json
    package_metadata_version = IntegerField()
    noarch = ComposableField(Noarch, required=False)
    preferred_env = ComposableField(PreferredEnv, required=False)


class PathData(Entity):
    _path = StringField()
    prefix_placeholder = StringField(required=False, nullable=True)
    file_mode = EnumField(FileMode, required=False, nullable=True)
    no_link = BooleanField(required=False, nullable=True)
    path_type = EnumField(PathType)

    @property
    def path(self):
        # because I don't have aliases as an option for entity fields yet
        return self._path


class PathDataV1(PathData):
    sha256 = StringField()
    size_in_bytes = IntegerField()
    inode_paths = ListField(string_types, required=False, nullable=True)


class PathsData(Entity):
    # from info/paths.json
    paths_version = IntegerField()
    paths = ListField(PathData)


class PackageInfo(ImmutableEntity):

    # attributes external to the package tarball
    extracted_package_dir = StringField()
    channel = ComposableField(Channel)
    repodata_record = ComposableField(IndexRecord)
    url = StringField()

    # attributes within the package tarball
    index_json_record = ComposableField(IndexRecord)
    icondata = StringField(required=False, nullable=True)
    package_metadata = ComposableField(PackageMetadata, required=False, nullable=True)
    paths_data = ComposableField(PathsData)



