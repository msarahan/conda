# (c) 2012-2015 Continuum Analytics, Inc. / http://continuum.io
# All Rights Reserved
#
# conda is distributed under the terms of the BSD 3-clause license.
# Consult LICENSE.txt or http://opensource.org/licenses/BSD-3-Clause.

from __future__ import print_function, division, absolute_import

import logging
import os
import sys
from collections import namedtuple
from os.path import abspath, basename, dirname, expanduser, isfile, isdir, join
from platform import machine

from .base.constants import DEFAULT_CHANNEL_ALIAS, ROOT_ENV_NAME
from .common.yaml import yaml_load
from .compat import string_types
from .exceptions import ProxyError, CondaRuntimeError
from .utils import try_write

log = logging.getLogger(__name__)
stderrlog = logging.getLogger('stderrlog')

output_json = False
debug_on = False
offline = False

# ----- rc file -----

# This is used by conda config to check which keys are allowed in the config
# file. Be sure to update it when new keys are added.

#################################################################
# Also update the example condarc file when you add a key here! #
#################################################################

rc_list_keys = [
    'channels',
    'disallow',
    'create_default_packages',
    'track_features',
    'envs_dirs',
    'default_channels',
]

ADD_BINSTAR_TOKEN = True

rc_bool_keys = [
    'add_binstar_token',
    'add_anaconda_token',
    'add_pip_as_python_dependency',
    'always_yes',
    'always_copy',
    'allow_softlinks',
    'auto_update_conda',
    'changeps1',
    'use_pip',
    'offline',
    'binstar_upload',
    'anaconda_upload',
    'show_channel_urls',
    'allow_other_channels',
    'update_dependencies',
    'channel_priority',
    'shortcuts',
]

rc_string_keys = [
    'ssl_verify',
    'channel_alias',
]

# Not supported by conda config yet
rc_other = [
    'proxy_servers',
]

if basename(sys.prefix) == '_conda' and basename(dirname(sys.prefix)) == 'envs':
    # conda is located in it's own private environment named '_conda'
    conda_in_root = False
    conda_private = True
    root_prefix = abspath(join(sys.prefix, '..', '..'))
    conda_prefix = sys.prefix
else:
    # conda is located in the root environment
    conda_in_root = True
    conda_private = False
    root_prefix = conda_prefix = abspath(sys.prefix)


root_dir = root_prefix
# root_dir = context.root_dir
root_writable = context.root_writable

# root_dir is an alias for root_prefix, we prefer the name "root_prefix"
# because it is more consistent with other names

user_rc_path = abspath(expanduser('~/.condarc'))
sys_rc_path = join(sys.prefix, '.condarc')

# add_anaconda_token = ADD_BINSTAR_TOKEN


class RC(object):

    def get(self, key, default=None):
        return getattr(context, key, default)


rc = RC()
envs_dirs = context.envs_dirs

def get_rc_path():
    path = os.getenv('CONDARC')
    if path == ' ':
        return None
    if path:
        return path
    for path in user_rc_path, sys_rc_path:
        if isfile(path):
            return path
    return None

rc_path = get_rc_path()

def load_condarc_(path):
    if not path or not isfile(path):
        return {}
    with open(path) as f:
        return yaml_load(f) or {}

sys_rc = load_condarc_(sys_rc_path) if isfile(sys_rc_path) else {}

# ----- local directories -----

def _default_envs_dirs():
    lst = [join(root_dir, 'envs')]
    if not root_writable:
        # ~/envs for backwards compatibility
        lst = ['~/.conda/envs', '~/envs'] + lst
    return lst

def _pathsep_env(name):
    x = os.getenv(name)
    if x is None:
        return []
    res = []
    for path in x.split(os.pathsep):
        if path == 'DEFAULTS':
            for p in rc.get('envs_dirs') or _default_envs_dirs():
                res.append(p)
        else:
            res.append(path)
    return res

def pkgs_dir_from_envs_dir(envs_dir):
    if abspath(envs_dir) == abspath(join(root_dir, 'envs')):
        return join(root_dir, 'pkgs32' if force_32bit else 'pkgs')
    else:
        return join(envs_dir, '.pkgs')

# ----- channels -----

# Note, get_*_urls() return unnormalized urls.

def get_local_urls(clear_cache=True):
    # remove the cache such that a refetch is made,
    # this is necessary because we add the local build repo URL
    if clear_cache:
        from .fetch import fetch_index
        fetch_index.cache = {}
    if local_channel:
        return local_channel
    from os.path import exists
    from conda.common.url import path_to_url
    try:
        from conda_build.config import croot
        if exists(croot):
            local_channel.append(path_to_url(croot))
    except ImportError:
        pass
    return local_channel


defaults_ = [
    'https://repo.continuum.io/pkgs/free',
    'https://repo.continuum.io/pkgs/pro',
]
if platform == "win":
    defaults_.append('https://repo.continuum.io/pkgs/msys2')


def get_default_urls(merged=False):
    if 'default_channels' in sys_rc:
        res = sys_rc['default_channels']
        if merged:
            res = list(res)
            res.extend(c for c in defaults_ if c not in res)
        return res
    return defaults_

def get_rc_urls():
    if rc is None or rc.get('channels') is None:
        return []
    if 'system' in rc['channels']:
        raise CondaRuntimeError("system cannot be used in .condarc")
    return rc['channels']

def set_offline():
    global offline
    offline = True

def is_offline():
    return offline


BINSTAR_TOKEN_PAT = re.compile(r'((:?binstar\.org|anaconda\.org)/?)(t/[0-9a-zA-Z\-<>]{4,})/')


def init_binstar(quiet=False):
    global binstar_client, binstar_domain, binstar_domain_tok
    global binstar_regex, BINSTAR_TOKEN_PAT
    if binstar_domain is not None:
        return
    elif binstar_client is None:
        try:
            from binstar_client.utils import get_binstar
            # Turn off output in offline mode so people don't think we're going online
            args = namedtuple('args', 'log_level')(0) if quiet or offline else None
            binstar_client = get_binstar(args)
        except ImportError:
            log.debug("Could not import binstar")
            binstar_client = ()
        except Exception as e:
            stderrlog.info("Warning: could not import binstar_client (%s)" % e)
    if binstar_client == ():
        binstar_domain = DEFAULT_CHANNEL_ALIAS
        binstar_domain_tok = None
    else:
        binstar_domain = binstar_client.domain.replace("api", "conda").rstrip('/') + '/'
        if add_anaconda_token and binstar_client.token:
            binstar_domain_tok = binstar_domain + 't/%s/' % (binstar_client.token,)
    binstar_regex = (r'((:?%s|binstar\.org|anaconda\.org)/?)(t/[0-9a-zA-Z\-<>]{4,})/' %
                     re.escape(binstar_domain[:-1]))
    BINSTAR_TOKEN_PAT = re.compile(binstar_regex)


def channel_prefix(token=False):
    global channel_alias, channel_alias_tok
    if channel_alias is None or (channel_alias_tok is None and token):
        init_binstar()
        if channel_alias is None or channel_alias == binstar_domain:
            channel_alias = binstar_domain
            channel_alias_tok = binstar_domain_tok
        if channel_alias is None:
            channel_alias = DEFAULT_CHANNEL_ALIAS
    if channel_alias_tok is None:
        channel_alias_tok = channel_alias
    return channel_alias_tok if token and add_anaconda_token else channel_alias

def add_binstar_tokens(url):
    if binstar_domain_tok and url.startswith(binstar_domain):
        url2 = BINSTAR_TOKEN_PAT.sub(r'\1', url)
        if url2 == url:
            return binstar_domain_tok + url.split(binstar_domain, 1)[1]
    return url

def hide_binstar_tokens(url):
    return BINSTAR_TOKEN_PAT.sub(r'\1t/<TOKEN>/', url)

def remove_binstar_tokens(url):
    return BINSTAR_TOKEN_PAT.sub(r'\1', url)


def get_channel_urls(platform=None):
    if os.getenv('CIO_TEST'):
        import cio_test
        base_urls = cio_test.base_urls
    elif 'channels' in rc:
        base_urls = ['system']
    else:
        base_urls = ['defaults']
    return base_urls


# ----- allowed channels -----

def get_allowed_channels():
    if not isfile(sys_rc_path):
        return None
    if sys_rc.get('allow_other_channels', True):
        return None
    if 'channels' in sys_rc:
        base_urls = ['system']
    else:
        base_urls = ['default']
    return base_urls

allowed_channels = get_allowed_channels()

# ----- proxy -----

def get_proxy_servers():
    res = rc.get('proxy_servers') or {}
    if isinstance(res, dict):
        return res
    raise ProxyError('proxy_servers setting not a mapping')


def load_condarc(path=None):
    global rc
    if path is not None:
        rc = load_condarc_(path)

    root_writable = try_write(root_dir)

    globals().update(locals())

    envs_dirs = [abspath(expanduser(p)) for p in (
            _pathsep_env('CONDA_ENVS_PATH') or
            rc.get('envs_dirs') or
            _default_envs_dirs()
            )]

    pkgs_dirs = [pkgs_dir_from_envs_dir(envs_dir) for envs_dir in envs_dirs]

    _default_env = os.getenv('CONDA_DEFAULT_ENV')
    if _default_env in (None, ROOT_ENV_NAME):
        default_prefix = root_dir
    elif os.sep in _default_env:
        default_prefix = abspath(_default_env)
    else:
        for envs_dir in envs_dirs:
            default_prefix = join(envs_dir, _default_env)
            if isdir(default_prefix):
                break
        else:
            default_prefix = join(envs_dirs[0], _default_env)

    # ----- foreign -----

    try:
        with open(join(root_dir, 'conda-meta', 'foreign')) as fi:
            foreign = fi.read().split()
    except IOError:
        foreign = [] if isdir(join(root_dir, 'conda-meta')) else ['python']

    binstar_regex = r'((:?binstar\.org|anaconda\.org)/?)(t/[0-9a-zA-Z\-<>]{4,})/'
    BINSTAR_TOKEN_PAT = re.compile(binstar_regex)
    channel_alias = rc.get('channel_alias', None)
    if not sys_rc.get('allow_other_channels', True) and 'channel_alias' in sys_rc:
        channel_alias = sys_rc['channel_alias']
    if channel_alias is not None:
        channel_alias = remove_binstar_tokens(channel_alias.rstrip('/') + '/')
    channel_alias_tok = binstar_client = binstar_domain = binstar_domain_tok = None

    offline = bool(rc.get('offline', False))
    add_anaconda_token = rc.get('add_anaconda_token',
                                rc.get('add_binstar_token', ADD_BINSTAR_TOKEN))

    add_pip_as_python_dependency = bool(rc.get('add_pip_as_python_dependency', True))
    always_yes = bool(rc.get('always_yes', False))
    always_copy = bool(rc.get('always_copy', False))
    changeps1 = bool(rc.get('changeps1', True))
    use_pip = bool(rc.get('use_pip', True))
    binstar_upload = rc.get('anaconda_upload',
                            rc.get('binstar_upload', None))  # None means ask
    allow_softlinks = bool(rc.get('allow_softlinks', True))
    auto_update_conda = bool(rc.get('auto_update_conda',
                                    rc.get('self_update',
                                           sys_rc.get('auto_update_conda', True))))
    # show channel URLs when displaying what is going to be downloaded
    show_channel_urls = rc.get('show_channel_urls', None)  # None means letting conda decide
    # set packages disallowed to be installed
    disallow = set(rc.get('disallow', []))
    # packages which are added to a newly created environment by default
    create_default_packages = list(rc.get('create_default_packages', []))
    update_dependencies = bool(rc.get('update_dependencies', True))
    channel_priority = bool(rc.get('channel_priority', True))
    shortcuts = bool(rc.get('shortcuts', True))

    # ssl_verify can be a boolean value or a filename string
    ssl_verify = rc.get('ssl_verify', True)

    try:
        track_features = rc.get('track_features', [])
        if isinstance(track_features, string_types):
            track_features = track_features.split()
        track_features = set(track_features)
    except KeyError:
        track_features = None


# offline = bool(rc.get('offline', False))
# add_anaconda_token = rc.get('add_anaconda_token',
#                             rc.get('add_binstar_token', ADD_BINSTAR_TOKEN))
#
# add_pip_as_python_dependency = bool(rc.get('add_pip_as_python_dependency', True))
# always_yes = context.always_yes
# always_copy = bool(rc.get('always_copy', False))
# changeps1 = bool(rc.get('changeps1', True))
# use_pip = bool(rc.get('use_pip', True))
# binstar_upload = rc.get('anaconda_upload',
#                         rc.get('binstar_upload', None))  # None means ask
# allow_softlinks = bool(rc.get('allow_softlinks', True))
# auto_update_conda = bool(rc.get('auto_update_conda',
#                                 rc.get('self_update',
#                                        sys_rc.get('auto_update_conda', True))))
# # show channel URLs when displaying what is going to be downloaded
# show_channel_urls = rc.get('show_channel_urls', None)  # None means letting conda decide
# # set packages disallowed to be installed
# disallow = set(rc.get('disallow', []))
# # packages which are added to a newly created environment by default
# create_default_packages = list(rc.get('create_default_packages', []))
# update_dependencies = bool(rc.get('update_dependencies', True))
# channel_priority = bool(rc.get('channel_priority', True))
# shortcuts = bool(rc.get('shortcuts', True))
#
# # ssl_verify can be a boolean value or a filename string
# ssl_verify = rc.get('ssl_verify', True)
#
# try:
#     track_features = rc.get('track_features', [])
#     if isinstance(track_features, string_types):
#         track_features = track_features.split()
#     track_features = set(track_features)
# except KeyError:
#     track_features = None
