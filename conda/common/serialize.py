# -*- coding: utf-8 -*-
# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import absolute_import, division, print_function, unicode_literals

#import json
# import ujson as json
import json
from rapidjson import loads, dumps, Encoder
from logging import getLogger

from .compat import PY2, odict, ensure_text_type
from .._vendor.auxlib.decorators import memoize

log = getLogger(__name__)


@memoize
def get_yaml():
    try:
        import ruamel_yaml as yaml
    except ImportError:  # pragma: no cover
        try:
            import ruamel.yaml as yaml
        except ImportError:
            raise ImportError("No yaml library available.\n"
                              "To proceed, conda install "
                              "ruamel_yaml")
    return yaml


yaml = get_yaml()


def represent_ordereddict(dumper, data):
    value = []

    for item_key, item_value in data.items():
        node_key = dumper.represent_data(item_key)
        node_value = dumper.represent_data(item_value)

        value.append((node_key, node_value))

    return yaml.nodes.MappingNode(u'tag:yaml.org,2002:map', value)


yaml.representer.RoundTripRepresenter.add_representer(odict, represent_ordereddict)

if PY2:
    def represent_unicode(self, data):
        return self.represent_str(data.encode('utf-8'))


    yaml.representer.RoundTripRepresenter.add_representer(unicode, represent_unicode)  # NOQA


def yaml_load(string):
    return yaml.load(string, Loader=yaml.RoundTripLoader, version="1.2")


def yaml_load_safe(string):
    """
    Examples:
        >>> yaml_load_safe("key: value")
        {'key': 'value'}

    """
    return yaml.load(string, Loader=yaml.SafeLoader, version="1.2")


def yaml_load_standard(string):
    """Uses the default (unsafe) loader.

    Examples:
        >>> yaml_load_standard("prefix: !!python/unicode '/Users/darwin/test'")
        {'prefix': '/Users/darwin/test'}
    """
    return yaml.load(string, Loader=yaml.Loader, version="1.2")


def yaml_dump(object):
    """dump object to string"""
    return yaml.dump(object, Dumper=yaml.RoundTripDumper,
                     block_seq_indent=2, default_flow_style=False,
                     indent=2)


def json_load(string):
    return loads(string)


def json_dump(object):
    class EntityEncoder(Encoder):
        def __call__(self, obj, stream=None, chunk_size=65535):
            res = super().__call__(obj, stream=stream, chunk_size=chunk_size)
            if res is not None:
                res += '\n'
            else:
                stream.write('\n')
            return res

        def default(self, obj):
            if hasattr(obj, 'dump'):
                return obj.dump()
            elif hasattr(obj, '__json__'):
                return obj.__json__()
            elif hasattr(obj, 'to_json'):
                return obj.to_json()
            elif hasattr(obj, 'as_json'):
                return obj.as_json()
            elif isinstance(obj, Enum):
                return obj.value
            return json.JSONEncoder.default(self, obj)

    encode = EntityEncoder(indent=2, sort_keys=True)
    return ensure_text_type(encode(object)).replace(":'", ": '")
