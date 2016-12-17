# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

import bz2
from contextlib import closing
from functools import wraps
import hashlib
import json
from logging import DEBUG, getLogger
from os import makedirs
from os.path import getmtime, join
from time import time
import warnings
from requests.exceptions import ConnectionError, HTTPError, SSLError
from requests.packages.urllib3.exceptions import InsecureRequestWarning

from .linked_data import linked_data
from .._vendor.auxlib.entity import EntityEncoder
from .._vendor.auxlib.logz import stringify
from ..base.context import context
from ..common.compat import iteritems, itervalues
from ..common.url import join_url
from ..connection import CondaSession
from ..exceptions import CondaHTTPError, CondaRuntimeError
from ..models.channel import Channel, offline_keep, prioritize_channels
from ..models.dist import Dist
from ..models.index_record import EMPTY_LINK, IndexRecord

log = getLogger(__name__)
dotlog = getLogger('dotupdate')
stdoutlog = getLogger('stdoutlog')
stderrlog = getLogger('stderrlog')

fail_unknown_host = False


def supplement_index_with_prefix(index, prefix, channel_priority_map):
    # type: (Dict[Dist, IndexRecord], str, Dict[channel_url, Tuple[canonical_name, priority]) -> None  # NOQA
    # supplement index with information from prefix/conda-meta
    assert prefix

    priorities = {chnl: prrty for chnl, prrty in itervalues(channel_priority_map)}
    maxp = max(itervalues(priorities)) + 1 if priorities else 1
    for dist, info in iteritems(linked_data(prefix)):
        fn = info['fn']
        schannel = info['schannel']
        prefix = '' if schannel == DEFAULTS else schannel + '::'
        priority = priorities.get(schannel, maxp)
        key = Dist(prefix + fn)
        if key in index:
            # Copy the link information so the resolver knows this is installed
            old_record = index[key]
            link = info.get('link') or EMPTY_LINK
            index[key] = IndexRecord.from_objects(old_record, link=link)
        else:
            # # only if the package in not in the repodata, use local
            # # conda-meta (with 'depends' defaulting to [])
            # info.setdefault('depends', ())

            # If the schannel is known but the package is not in the index, it is
            # because 1) the channel is unavailable offline or 2) the package has
            # been removed from that channel. Either way, we should prefer any
            # other version of the package to this one.
            priority = MAX_CHANNEL_PRIORITY if schannel in priorities else priority
            index[key] = IndexRecord.from_objects(info, priority=priority)


def get_index(channel_urls=(), prepend=True, platform=None,
              use_local=False, use_cache=False, unknown=False, prefix=False):
    """
    Return the index of packages available on the channels

    If prepend=False, only the channels passed in as arguments are used.
    If platform=None, then the current platform is used.
    If prefix is supplied, then the packages installed in that prefix are added.
    """
    if use_local:
        channel_urls = ['local'] + list(channel_urls)
    if prepend:
        channel_urls += context.channels
    channel_priority_map = prioritize_channels(channel_urls, platform=platform)
    index = fetch_index(channel_priority_map, use_cache=use_cache, unknown=unknown)

    if prefix:
        priorities = {c: p for c, p in itervalues(channel_urls)}
        maxp = max(itervalues(priorities)) + 1 if priorities else 1
        for dist, info in iteritems(linked_data(prefix)):
            fn = info['fn']
            schannel = info['schannel']
            prefix = '' if schannel == DEFAULTS else schannel + '::'
            priority = priorities.get(schannel, maxp)
            key = Dist(prefix + fn)
            if key in index:
                # Copy the link information so the resolver knows this is installed
                link = info.get('link') or EMPTY_LINK
                index[key] = Record.from_objects(index[key], link=link)
            else:
                # # only if the package in not in the repodata, use local
                # # conda-meta (with 'depends' defaulting to [])
                # info.setdefault('depends', [])  # disabled because already default for Record

                # If the schannel is known but the package is not in the index, it is
                # because 1) the channel is unavailable offline or 2) the package has
                # been removed from that channel. Either way, we should prefer any
                # other version of the package to this one.
                priority = MAX_CHANNEL_PRIORITY if schannel in priorities else priority
                index[key] = Record.from_objects(info, priority=priority)

    return index


# We need a decorator so that the dot gets printed *after* the repodata is fetched
class dotlog_on_return(object):
    def __init__(self, msg):
        self.msg = msg

    def __call__(self, f):
        @wraps(f)
        def func(*args, **kwargs):
            res = f(*args, **kwargs)
            dotlog.debug("%s args %s kwargs %s" % (self.msg, args, kwargs))
            return res
        return func


@dotlog_on_return("fetching repodata:")
def fetch_repodata(url, cache_dir=None, use_cache=False, session=None):
    if not offline_keep(url):
        return {'packages': {}}
    cache_path = join(cache_dir or create_cache_dir(), cache_fn_url(url))
    try:
        log.debug("Opening repodata cache for %s at %s", url, cache_path)
        mtime = getmtime(cache_path)
        if time() - mtime < context.repodata_timeout_secs:
            with open(cache_path) as f:
                cache = json.load(f)
            return cache
        else:
            mod_etag_headers = read_mod_and_etag(cache_path)
    except (IOError, ValueError):
        log.debug("No local cache found for %s at %s", url, cache_path)
        cache = {'packages': {}}

    if use_cache:
        return cache

    if not context.ssl_verify:
        warnings.simplefilter('ignore', InsecureRequestWarning)

    session = session or CondaSession()

    headers = {}
    if "_etag" in cache:
        headers["If-None-Match"] = cache["_etag"]
    if "_mod" in cache:
        headers["If-Modified-Since"] = cache["_mod"]

    if 'repo.continuum.io' in url or url.startswith("file://"):
        filename = 'repodata.json.bz2'
        headers['Accept-Encoding'] = 'identity'
    else:
        headers['Accept-Encoding'] = 'gzip, deflate, compress, identity'
        headers['Content-Type'] = 'application/json'
        filename = 'repodata.json'

    try:
        timeout = context.remote_connect_timeout_secs, context.remote_read_timeout_secs
        resp = session.get(join_url(url, filename), headers=headers, proxies=session.proxies,
                           timeout=timeout)
        if log.isEnabledFor(DEBUG):
            log.debug(stringify(resp))
        resp.raise_for_status()

        if resp.status_code != 304:
            def get_json_str(filename, resp_content):
                if filename.endswith('.bz2'):
                    return bz2.decompress(resp_content).decode('utf-8')
                else:
                    return resp_content.decode('utf-8')

            if url.startswith('file://'):
                json_str = get_json_str(filename, resp.content)
            else:
                json_str = get_json_str(filename, resp.content)

            cache = json.loads(json_str)
            add_http_value_to_dict(resp, 'Etag', cache, '_etag')
            add_http_value_to_dict(resp, 'Last-Modified', cache, '_mod')

    except ValueError as e:
        raise CondaRuntimeError("Invalid index file: {0}{1}: {2}"
                                .format(url, filename, e))

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 407:  # Proxy Authentication Required
            handle_proxy_407(url, session)
            # Try again
            return fetch_repodata(url, cache_dir=cache_dir, use_cache=use_cache, session=session)

        if e.response.status_code == 404:
            if url.endswith('/noarch/'):  # noarch directory might not exist
                return None
            msg = 'Could not find URL: %s' % url
        elif e.response.status_code == 403 and url.endswith('/noarch/'):
            return None

        elif e.response.status_code == 401 and context.channel_alias in url:
            # Note, this will not trigger if the binstar configured url does
            # not match the conda configured one.
            msg = ("Warning: you may need to login to anaconda.org again with "
                   "'anaconda login' to access private packages(%s, %s)" %
                   (url, e))
            stderrlog.info(msg)
            return fetch_repodata(url, cache_dir=cache_dir, use_cache=use_cache, session=session)

        else:
            help_message = "An HTTP error occurred when trying to retrieve this URL.\n%r" % e

        raise CondaHTTPError(help_message,
                             getattr(e.response, 'url', None),
                             status_code,
                             getattr(e.response, 'reason', None),
                             getattr(e.response, 'elapsed', None))

    cache['_url'] = url
    try:
        with open(cache_path, 'w') as fo:
            json.dump(cache, fo, indent=2, sort_keys=True, cls=EntityEncoder)
    except IOError:
        pass

    return cache or None


def _collect_repodatas_serial(use_cache, urls):
    # type: (bool, List[str]) -> List[Sequence[str, Option[Dict[Dist, IndexRecord]]]]
    session = CondaSession()
    repodatas = [(url, fetch_repodata(url, use_cache=use_cache, session=session))
                 for url in urls]
    return repodatas


def _collect_repodatas_concurrent(executor, use_cache, urls):
    futures = tuple(executor.submit(fetch_repodata, url, use_cache=use_cache,
                                    session=CondaSession()) for url in urls)
    repodatas = [(u, f.result()) for u, f in zip(urls, futures)]
    return repodatas


def _collect_repodatas(use_cache, urls):
    # TODO: there HAS to be a way to clean up this logic
    if context.concurrent:
        try:
            import concurrent.futures
            executor = concurrent.futures.ThreadPoolExecutor(10)
        except (ImportError, RuntimeError) as e:
            log.debug(repr(e))
            # concurrent.futures is only available in Python >= 3.2 or if futures is installed
            # RuntimeError is thrown if number of threads are limited by OS
            repodatas = _collect_repodatas_serial(use_cache, urls)
        else:
            try:
                repodatas = _collect_repodatas_concurrent(executor, use_cache, urls)
            except RuntimeError as e:
                # Cannot start new thread, then give up parallel execution
                log.debug(repr(e))
                repodatas = _collect_repodatas_serial(use_cache, urls)
            finally:
                executor.shutdown(wait=True)
    else:
        repodatas = _collect_repodatas_serial(use_cache, urls)

    return repodatas


def fetch_index(channel_urls, use_cache=False, unknown=False, index=None):
    # type: (prioritize_channels(), bool, bool, Dict[Dist, IndexRecord]) -> Dict[Dist, IndexRecord]
    log.debug('channel_urls=' + repr(channel_urls))
    # pool = ThreadPool(5)
    if index is None:
        index = {}
    stdoutlog.info("Fetching package metadata ...")
    # if not isinstance(channel_urls, dict):
    #     channel_urls = prioritize_channels(channel_urls)

    urls = tuple(filter(offline_keep, channel_urls))
    repodatas = _collect_repodatas(use_cache, urls)
    # type: List[Sequence[str, Option[Dict[Dist, IndexRecord]]]]
    #   this is sorta a lie; actually more primitve types

    def make_index(repodatas):
        result = dict()
        for channel, repodata in repodatas:
            if repodata is None:
                continue
            url_s, priority = channel_urls[channel]
            channel = channel.rstrip('/')
            for fn, info in iteritems(repodata['packages']):
                full_url = join_url(channel_url, fn)
                info.update(dict(fn=fn,
                                 schannel=canonical_name,
                                 channel=channel_url,
                                 priority=priority,
                                 url=full_url,
                                 auth=channel.auth,
                                 ))
                key = Dist(canonical_name + '::' + fn if canonical_name != 'defaults' else fn)
                result[key] = IndexRecord(**info)
        return result

    index = make_index(repodatas)

    stdoutlog.info('\n')
    if unknown:
        add_unknown(index, channel_urls)
    if context.add_pip_as_python_dependency:
        add_pip_dependency(index)
    return index


def cache_fn_url(url):
    md5 = hashlib.md5(url.encode('utf-8')).hexdigest()
    return '%s.json' % (md5[:8],)


def add_http_value_to_dict(resp, http_key, d, dict_key):
    value = resp.headers.get(http_key)
    if value:
        d[dict_key] = value


def add_unknown(index, priorities):
    # TODO: discuss with @mcg1969 and document
    raise NotImplementedError()
    # priorities = {p[0]: p[1] for p in itervalues(priorities)}
    # maxp = max(itervalues(priorities)) + 1 if priorities else 1
    # for dist, info in iteritems(package_cache()):
    #     # schannel, dname = dist2pair(dist)
    #     fname = dist.to_filename()
    #     # fkey = dist + '.tar.bz2'
    #     if dist in index or not info['dirs']:
    #         continue
    #     try:
    #         with open(join(info['dirs'][0], 'info', 'index.json')) as fi:
    #             meta = json.load(fi)
    #     except IOError:
    #         continue
    #     if info['urls']:
    #         url = info['urls'][0]
    #     elif meta.get('url'):
    #         url = meta['url']
    #     elif meta.get('channel'):
    #         url = meta['channel'].rstrip('/') + '/' + fname
    #     else:
    #         url = '<unknown>/' + fname
    #     if url.rsplit('/', 1)[-1] != fname:
    #         continue
    #     channel, schannel2 = Channel(url).url_channel_wtf
    #     if schannel2 != dist.channel:
    #         continue
    #     priority = priorities.get(dist.channel, maxp)
    #     if 'link' in meta:
    #         del meta['link']
    #     meta.update({'fn': fname,
    #                  'url': url,
    #                  'channel': channel,
    #                  'schannel': dist.channel,
    #                  'priority': priority,
    #                  })
    #     meta.setdefault('depends', [])
    #     log.debug("adding cached pkg to index: %s" % dist)
    #     index[dist] = Record(**meta)


def add_pip_dependency(index):
    # TODO: discuss with @mcg1969 and document
    for dist, info in iteritems(index):
        if info['name'] == 'python' and info['version'].startswith(('2.', '3.')):
            index[dist] = IndexRecord.from_objects(info, depends=info['depends'] + ('pip',))


def create_cache_dir():
    cache_dir = join(context.pkgs_dirs[0], 'cache')
    try:
        makedirs(cache_dir)
    except OSError:
        pass
    return cache_dir
