# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from copy import copy
from itertools import chain
from logging import getLogger

from ..base.constants import (DEFAULTS_CHANNEL_NAME, DEFAULT_CHANNELS_UNIX, DEFAULT_CHANNELS_WIN,
                              MAX_CHANNEL_PRIORITY, UNKNOWN_CHANNEL)
from ..base.context import context
from ..common.compat import ensure_text_type, isiterable, iteritems, odict, with_metaclass
from ..common.path import is_path, win_path_backout
from ..common.url import (Url, has_scheme, is_url, join_url, path_to_url,
                          split_conda_url_easy_parts, split_scheme_auth_token, urlparse,
                          split_platform)

try:
    from cytoolz.functoolz import excepts
    from cytoolz.itertoolz import concatv, drop
except ImportError:  # pragma: no cover
    from .._vendor.toolz.functoolz import excepts  # NOQA
    from .._vendor.toolz.itertoolz import concatv, drop  # NOQA

log = getLogger(__name__)


# backward compatibility for conda-build
def get_conda_build_local_url():
    return context.local_build_root,


def tokenized_startswith(test_iterable, startswith_iterable):
    return all(t == sw for t, sw in zip(test_iterable, startswith_iterable))


def tokenized_conda_url_startswith(test_url, startswith_url):
    test_url, startswith_url = urlparse(test_url), urlparse(startswith_url)
    if test_url.host != startswith_url.host or test_url.port != startswith_url.port:
        return False
    norm_url_path = lambda url: url.path.strip('/') or '/'
    return tokenized_startswith(norm_url_path(test_url).split('/'),
                                norm_url_path(startswith_url).split('/'))


def _get_channel_for_name(channel_name):
    def _get_channel_for_name_helper(name):
        if name in context.custom_channels:
            return context.custom_channels[name]
        else:
            test_name = name.rsplit('/', 1)[0]  # progressively strip off path segments
            if test_name == name:
                return None
            return _get_channel_for_name_helper(test_name)

    channel = _get_channel_for_name_helper(channel_name)

    if channel is not None:
        # stripping off path threw information away from channel_name (i.e. any potential subname)
        # channel.name *should still be* channel_name
        channel.name = channel_name
        return channel
    else:
        ca = context.channel_alias
        return Channel(scheme=ca.scheme, auth=ca.auth, location=ca.location, token=ca.token,
                       name=channel_name)


def _read_channel_configuration(scheme, host, port, path):
    # return location, name, scheme, auth, token

    path = path and path.rstrip('/')
    test_url = Url(host=host, port=port, path=path).url

    # Step 1. No path given; channel name is None
    if not path:
        return Url(host=host, port=port).url.rstrip('/'), None, scheme or None, None, None

    # Step 2. migrated_custom_channels matches
    for name, location in sorted(context.migrated_custom_channels.items(), reverse=True,
                                 key=lambda x: len(x[0])):
        location, _scheme, _auth, _token = split_scheme_auth_token(location)
        if tokenized_conda_url_startswith(test_url, join_url(location, name)):
            # translate location to new location, with new credentials
            subname = test_url.replace(join_url(location, name), '', 1).strip('/')
            channel_name = join_url(name, subname)
            channel = _get_channel_for_name(channel_name)
            return channel.location, channel_name, channel.scheme, channel.auth, channel.token

    # Step 3. migrated_channel_aliases matches
    for migrated_alias in context.migrated_channel_aliases:
        if test_url.startswith(migrated_alias.location):
            name = test_url.replace(migrated_alias.location, '', 1).strip('/')
            ca = context.channel_alias
            return ca.location, name, ca.scheme, ca.auth, ca.token

    # Step 4. custom_channels matches
    for name, channel in sorted(context.custom_channels.items(), reverse=True,
                                key=lambda x: len(x[0])):
        that_test_url = join_url(channel.location, channel.name)
        if test_url.startswith(that_test_url):
            subname = test_url.replace(that_test_url, '', 1).strip('/')
            return (channel.location, join_url(channel.name, subname), scheme,
                    channel.auth, channel.token)

    # Step 5. channel_alias match
    ca = context.channel_alias
    if ca.location and test_url.startswith(ca.location):
        name = test_url.replace(ca.location, '', 1).strip('/') or None
        return ca.location, name, scheme, ca.auth, ca.token

    # Step 6. not-otherwise-specified file://-type urls
    if host is None:
        # this should probably only happen with a file:// type url
        assert port is None
        location, name = test_url.rsplit('/', 1)
        if not location:
            location = '/'
        _scheme, _auth, _token = 'file', None, None
        return location, name, _scheme, _auth, _token

    # Step 7. fall through to host:port as channel_location and path as channel_name
    return (Url(host=host, port=port).url.rstrip('/'), path.strip('/') or None,
            scheme or None, None, None)


def parse_conda_channel_url(url):
    (scheme, auth, token, platform, package_filename,
     host, port, path, query) = split_conda_url_easy_parts(url, context.known_subdirs)

    # recombine host, port, path to get a channel_name and channel_location
    (channel_location, channel_name, configured_scheme, configured_auth,
     configured_token) = _read_channel_configuration(scheme, host, port, path)

    # if we came out with no channel_location or channel_name, we need to figure it out
    # from host, port, path
    assert channel_location is not None or channel_name is not None

    return Channel(configured_scheme or 'https',
                   auth or configured_auth,
                   channel_location,
                   token or configured_token,
                   channel_name,
                   platform,
                   package_filename)


class ChannelType(type):
    """
    This metaclass does basic caching and enables static constructor method usage with a
    single arg.
    """

    def __call__(cls, *args, **kwargs):
        if len(args) == 1 and not kwargs:
            value = args[0]
            if isinstance(value, Channel):
                return value
            elif value in Channel._cache_:
                return Channel._cache_[value]
            else:
                c = Channel._cache_[value] = Channel.from_value(value)
                return c
        else:
            if 'channels' in kwargs:
                # presence of 'channels' kwarg indicates MultiChannel
                name = kwargs['name']
                channels = tuple(super(ChannelType, cls).__call__(**_kwargs)
                                 for _kwargs in kwargs['channels'])
                return MultiChannel(name, channels)
            else:
                return super(ChannelType, cls).__call__(*args, **kwargs)


@with_metaclass(ChannelType)
class Channel(object):
    """
    Channel:
    scheme <> auth <> location <> token <> channel <> subchannel <> platform <> package_filename

    Package Spec:
    channel <> subchannel <> namespace <> package_name

    """
    _cache_ = {}

    @staticmethod
    def _reset_state():
        Channel._cache_ = {}

    def __init__(self, scheme=None, auth=None, location=None, token=None, name=None,
                 platform=None, package_filename=None):
        self.scheme = scheme
        self.auth = auth
        self.location = location
        self.token = token
        self.name = name
        self.platform = platform
        self.package_filename = package_filename

    @property
    def channel_location(self):
        return self.location

    @property
    def channel_name(self):
        return self.name

    @property
    def subdir(self):
        return self.platform

    @staticmethod
    def from_url(url):
        return parse_conda_channel_url(url)

    @staticmethod
    def from_channel_name(channel_name):
        return _get_channel_for_name(channel_name)

    @staticmethod
    def from_value(value):
        if value in (None, '<unknown>', 'None:///<unknown>', 'None'):
            return Channel(name=UNKNOWN_CHANNEL)
        if hasattr(value, 'decode'):
            value = value.decode(UTF8)
        if has_scheme(value):
            if value.startswith('file:'):
                value = win_path_backout(value)
            return Channel.from_url(value)
        elif is_path(value):
            return Channel.from_url(path_to_url(value))
        elif value.endswith('.tar.bz2'):
            if value.startswith('file:'):
                value = win_path_backout(value)
            return Channel.from_url(value)
        else:
            # at this point assume we don't have a bare (non-scheme) url
            #   e.g. this would be bad:  repo.continuum.io/pkgs/free
            _stripped, platform = split_platform(value)
            if _stripped in context.custom_multichannels:
                return MultiChannel(_stripped, context.custom_multichannels[_stripped], platform)
            else:
                return Channel.from_channel_name(value)

    @property
    def canonical_name(self):
        for multiname, channels in iteritems(context.custom_multichannels):
            for channel in channels:
                if self.name == channel.name:
                    return multiname

        for that_name in context.custom_channels:
            if self.name and tokenized_startswith(self.name.split('/'), that_name.split('/')):
                return self.name

        if any(c.location == self.location
               for c in concatv((context.channel_alias,), context.migrated_channel_aliases)):
            return self.name

        # fall back to the equivalent of self.base_url
        # re-defining here because base_url for MultiChannel is None
        if self.scheme:
            return "%s://%s" % (self.scheme, join_url(self.location, self.name))
        else:
            return join_url(self.location, self.name).lstrip('/')

    def urls(self, with_credentials=False, subdirs=None):
        if subdirs is None:
            subdirs = context.subdirs

        assert isiterable(subdirs), subdirs  # subdirs must be a non-string iterable

        if self.canonical_name == UNKNOWN_CHANNEL:
            return Channel(DEFAULTS_CHANNEL_NAME).urls(with_credentials, subdirs)

        base = [self.location]
        if with_credentials and self.token:
            base.extend(['t', self.token])
        base.append(self.name)
        base = join_url(*base)

        def _platforms():
            if self.platform:
                yield self.platform
                if self.platform != 'noarch':
                    yield 'noarch'
            else:
                for subdir in subdirs:
                    yield subdir

        bases = (join_url(base, p) for p in _platforms())

        if with_credentials and self.auth:
            return ["%s://%s@%s" % (self.scheme, self.auth, b) for b in bases]
        else:
            return ["%s://%s" % (self.scheme, b) for b in bases]

    def url(self, with_credentials=False):
        if self.canonical_name == UNKNOWN_CHANNEL:
            return None

        base = [self.location]
        if with_credentials and self.token:
            base.extend(['t', self.token])
        base.append(self.name)
        if self.platform:
            base.append(self.platform)
            if self.package_filename:
                base.append(self.package_filename)
        else:
            first_non_noarch = next((s for s in context.subdirs if s != 'noarch'), 'noarch')
            base.append(first_non_noarch)

    def _urls_helper(self, platform=None):
        subdir = platform if platform is not None else context.subdir
        return [join_url(self.base_url, subdir), join_url(self.base_url, 'noarch')]

    def get_urls(self, platform):
        if self.canonical_name in context.channel_map:
            if platform != context.subdir:
                url_channels = get_default_channels_canonical_name(platform)
            else:
                url_channels = context.channel_map[self.canonical_name]
            return list(chain.from_iterable(c._urls_helper(platform) for c in url_channels))
        else:
            return self._urls_helper(platform)

    @property
    def urls(self):
        return self.get_urls(context.subdir)

    @property
    def base_url(self):
        if self.canonical_name == UNKNOWN_CHANNEL:
            return None
        return "%s://%s" % (self.scheme, join_url(self.location, self.name))

    def __str__(self):
        return self.base_url or ""

    def __repr__(self):
        return ("Channel(scheme=%r, auth=%r, location=%r, token=%r, name=%r, platform=%r, "
                "package_filename=%r)" % (self.scheme,
                                          self.auth and "%s:<PASSWORD>" % self.auth.split(':')[0],
                                          self.location,
                                          self.token and "<TOKEN>",
                                          self.name,
                                          self.platform,
                                          self.package_filename))

    def __eq__(self, other):
        if isinstance(other, Channel):
            return self.location == other.location and self.name == other.name
        else:
            try:
                _other = Channel(other)
                return self.location == _other.location and self.name == _other.name
            except Exception as e:
                log.debug("%r", e)
                return False

    def __hash__(self):
        return hash((self.location, self.name))

    def __init__(self, url):
        log.debug("making channel object for url: %s", url)
        if url.endswith('.tar.bz2'):
            # throw away filename from url
            url = url.rsplit('/', 1)[0]
        if not has_scheme(url):
            url = path_to_url(url)
        self._raw_value = url
        parsed = urlparse(url)
        self._scheme = parsed.scheme
        self._netloc = parsed.netloc
        self._auth = parsed.auth
        _path, self._platform = split_platform(parsed.path)
        self._token, self._path = split_token(_path)

    def __bool__(self):
        return self.__nonzero__()

    def __json__(self):
        return self.__dict__

    @property
    def url_channel_wtf(self):
        return self.base_url, self.canonical_name

    def dump(self):
        return {
            "scheme": self.scheme,
            "auth": self.auth,
            "location": self.location,
            "token": self.token,
            "name": self.name,
            "platform": self.platform,
            "package_filename": self.package_filename,
        }


class MultiChannel(Channel):

    def __init__(self, name, channels, platform=None):
        self.name = name
        self.location = None

        if platform:
            c_dicts = tuple(c.dump() for c in channels)
            any(cd.update(platform=platform) for cd in c_dicts)
            self._channels = tuple(Channel(**cd) for cd in c_dicts)
        else:
            self._channels = channels

        self.scheme = None
        self.auth = None
        self.token = None
        self.platform = platform
        self.package_filename = None

    def __init__(self, value):
        self._raw_value = value
        self._scheme = self._netloc = self._auth = self._token = self._path = self._platform = None

    @property
    def canonical_name(self):
        return self.name

    def urls(self, with_credentials=False, subdirs=None):
        _channels = self._channels
        if self.name == 'defaults':
            platform = next((s for s in reversed(subdirs or context.subdirs) if s != 'noarch'), '')
            if platform != context.subdir:
                # necessary shenanigan because different platforms have different default channels
                urls = DEFAULT_CHANNELS_WIN if 'win' in platform else DEFAULT_CHANNELS_UNIX
                ca = context.channel_alias
                _channels = tuple(Channel.make_simple_channel(ca, v) for v in urls)
        return list(chain.from_iterable(c.urls(with_credentials, subdirs) for c in _channels))

    @property
    def base_url(self):
        return None

    def url(self, with_credentials=False):
        return None

    def dump(self):
        return {
            "name": self.name,
            "channels": tuple(c.dump() for c in self._channels)
        }


def tokenized_startswith(test_iterable, startswith_iterable):
    return all(t == sw for t, sw in zip(test_iterable, startswith_iterable))


def tokenized_conda_url_startswith(test_url, startswith_url):
    test_url, startswith_url = urlparse(test_url), urlparse(startswith_url)
    if test_url.host != startswith_url.host or test_url.port != startswith_url.port:
        return False
    norm_url_path = lambda url: url.path.strip('/') or '/'
    return tokenized_startswith(norm_url_path(test_url).split('/'),
                                norm_url_path(startswith_url).split('/'))


def _get_channel_for_name(channel_name):
    def _get_channel_for_name_helper(name):
        if name in context.custom_channels:
            return context.custom_channels[name]
        else:
            test_name = name.rsplit('/', 1)[0]  # progressively strip off path segments
            if test_name == name:
                return None
            return _get_channel_for_name_helper(test_name)

    _stripped, platform = split_platform(channel_name)
    channel = _get_channel_for_name_helper(_stripped)

    if channel is not None:
        # stripping off path threw information away from channel_name (i.e. any potential subname)
        # channel.name *should still be* channel_name
        channel = copy(channel)
        channel.name = channel_name
        if platform:
            channel.platform = platform
        return channel
    else:
        ca = context.channel_alias
        return Channel(scheme=ca.scheme, auth=ca.auth, location=ca.location, token=ca.token,
                       name=_stripped, platform=platform)


def _read_channel_configuration(scheme, host, port, path):
    # return location, name, scheme, auth, token

    path = path and path.rstrip('/')
    test_url = Url(host=host, port=port, path=path).url

    # Step 1. No path given; channel name is None
    if not path:
        return Url(host=host, port=port).url.rstrip('/'), None, scheme or None, None, None

    # Step 2. migrated_custom_channels matches
    for name, location in sorted(context.migrated_custom_channels.items(), reverse=True,
                                 key=lambda x: len(x[0])):
        location, _scheme, _auth, _token = split_scheme_auth_token(location)
        if tokenized_conda_url_startswith(test_url, join_url(location, name)):
            # translate location to new location, with new credentials
            subname = test_url.replace(join_url(location, name), '', 1).strip('/')
            channel_name = join_url(name, subname)
            channel = _get_channel_for_name(channel_name)
            return channel.location, channel_name, channel.scheme, channel.auth, channel.token

    # Step 3. migrated_channel_aliases matches
    for migrated_alias in context.migrated_channel_aliases:
        if test_url.startswith(migrated_alias.location):
            name = test_url.replace(migrated_alias.location, '', 1).strip('/')
            ca = context.channel_alias
            return ca.location, name, ca.scheme, ca.auth, ca.token

    # Step 4. custom_channels matches
    for name, channel in sorted(context.custom_channels.items(), reverse=True,
                                key=lambda x: len(x[0])):
        that_test_url = join_url(channel.location, channel.name)
        if test_url.startswith(that_test_url):
            subname = test_url.replace(that_test_url, '', 1).strip('/')
            return (channel.location, join_url(channel.name, subname), scheme,
                    channel.auth, channel.token)

    # Step 5. channel_alias match
    ca = context.channel_alias
    if ca.location and test_url.startswith(ca.location):
        name = test_url.replace(ca.location, '', 1).strip('/') or None
        return ca.location, name, scheme, ca.auth, ca.token

    # Step 6. not-otherwise-specified file://-type urls
    if host is None:
        # this should probably only happen with a file:// type url
        assert port is None
        location, name = test_url.rsplit('/', 1)
        if not location:
            location = '/'
        _scheme, _auth, _token = 'file', None, None
        return location, name, _scheme, _auth, _token

    # Step 7. fall through to host:port as channel_location and path as channel_name
    #  but bump the first token of paths starting with /conda for compatibility with
    #  Anaconda Enterprise Repository software.
    bump = None
    path_parts = path.strip('/').split('/')
    if path_parts and path_parts[0] == 'conda':
        bump, path = 'conda', '/'.join(drop(1, path_parts))
    return (Url(host=host, port=port, path=bump).url.rstrip('/'), path.strip('/') or None,
            scheme or None, None, None)


def parse_conda_channel_url(url):
    (scheme, auth, token, platform, package_filename,
     host, port, path, query) = split_conda_url_easy_parts(url)

    # recombine host, port, path to get a channel_name and channel_location
    (channel_location, channel_name, configured_scheme, configured_auth,
     configured_token) = _read_channel_configuration(scheme, host, port, path)

    # if we came out with no channel_location or channel_name, we need to figure it out
    # from host, port, path
    assert channel_location is not None or channel_name is not None

    return Channel(configured_scheme or 'https',
                   auth or configured_auth,
                   channel_location,
                   token or configured_token,
                   channel_name,
                   platform,
                   package_filename)


# backward compatibility for conda-build
def get_conda_build_local_url():
    return context.local_build_root,


def prioritize_channels(channels, with_credentials=True, subdirs=None):
    # prioritize_channels returns and OrderedDict with platform-specific channel
    #   urls as the key, and a tuple of canonical channel name and channel priority
    #   number as the value
    # ('https://conda.anaconda.org/conda-forge/osx-64/', ('conda-forge', 1))
    result = odict()
    for channel_priority, chn in enumerate(channels):
        channel = Channel(chn)
        for url in channel.urls(with_credentials, subdirs):
            if url in result:
                continue
            result[url] = channel.canonical_name, min(channel_priority, MAX_CHANNEL_PRIORITY - 1)
    return result


def offline_keep(url):
    return not context.offline or not is_url(url) or url.startswith('file:/')


context.register_reset_callaback(Channel._reset_state)
