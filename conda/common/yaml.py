# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from logging import getLogger

from .compat import PY2, odict
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
    yaml = get_yaml()
    return yaml.load(string, Loader=yaml.RoundTripLoader, version="1.2")


def yaml_dump(object):
    """dump object to string"""
    return yaml.dump(object, Dumper=yaml.RoundTripDumper,
                     block_seq_indent=2, default_flow_style=False,
                     indent=2)
