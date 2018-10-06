# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from conda._vendor.auxlib.ish import dals
from conda.base.context import context, reset_context
from conda.common.compat import odict
from conda.common.configuration import YamlRawParameter
from conda.common.url import join_url
from conda.common.yaml import yaml_load
from conda.models.channel import Channel
from conda.utils import on_win
from logging import getLogger
from unittest import TestCase

log = getLogger(__name__)


class DefaultConfigChannelTests(TestCase):

    @classmethod
    def setUpClass(cls):
        reset_context()
        cls.platform = context.subdir
        cls.DEFAULT_URLS = ['https://repo.continuum.io/pkgs/free/%s' % cls.platform,
                            'https://repo.continuum.io/pkgs/free/noarch',
                            'https://repo.continuum.io/pkgs/pro/%s' % cls.platform,
                            'https://repo.continuum.io/pkgs/pro/noarch']
        if on_win:
            cls.DEFAULT_URLS.extend(['https://repo.continuum.io/pkgs/msys2/%s' % cls.platform,
                                     'https://repo.continuum.io/pkgs/msys2/noarch'])

    def test_channel_alias_channels(self):
        channel = Channel('binstar/label/dev')
        assert channel.channel_name == "binstar/label/dev"
        assert channel.channel_location == "conda.anaconda.org"
        assert channel.platform is None
        assert channel.package_filename is None
        assert channel.canonical_name == "binstar/label/dev"
        assert channel.urls() == [
            'https://conda.anaconda.org/binstar/label/dev/%s' % context.subdir,
            'https://conda.anaconda.org/binstar/label/dev/noarch',
        ]

    def test_channel_cache(self):
        Channel._reset_state()
        assert len(Channel._cache_) == 0
        dc = Channel('defaults')
        assert len(Channel._cache_) == 1
        dc1 = Channel('defaults')
        assert len(Channel._cache_) == 1
        dc2 = Channel('defaults')
        assert len(Channel._cache_) == 1

        assert dc1 is dc
        assert dc2 is dc

        dc3 = Channel(dc)
        assert len(Channel._cache_) == 1
        assert dc3 is dc

        ccc = Channel('conda-canary')
        assert len(Channel._cache_) == 2

        ccc1 = Channel('conda-canary')
        assert len(Channel._cache_) == 2
        assert ccc1 is ccc

    def test_default_channel(self):
        dc = Channel('defaults')
        assert dc.canonical_name == 'defaults'
        assert dc.urls() == self.DEFAULT_URLS

    def test_url_channel_w_platform(self):
        channel = Channel('https://repo.continuum.io/pkgs/free/osx-64')

        assert channel.scheme == "https"
        assert channel.location == "repo.continuum.io"
        assert channel.platform == 'osx-64'
        assert channel.name == 'pkgs/free'

        assert channel.base_url == 'https://repo.continuum.io/pkgs/free'
        assert channel.canonical_name == 'defaults'
        assert channel.url() == 'https://repo.continuum.io/pkgs/free/osx-64'
        assert channel.urls() == [
            'https://repo.continuum.io/pkgs/free/osx-64',
            'https://repo.continuum.io/pkgs/free/noarch',
        ]

    def test_bare_channel(self):
        url = "http://conda-01"
        channel = Channel(url)
        assert channel.scheme == "http"
        assert channel.location == "conda-01"
        assert channel.platform is None
        assert channel.canonical_name == url
        assert channel.name is None

        assert channel.base_url == url
        assert channel.url() == join_url(url, context.subdir)
        assert channel.urls() == [
            join_url(url, context.subdir),
            join_url(url, 'noarch'),
        ]


class AnacondaServerChannelTests(TestCase):

    @classmethod
    def setUpClass(cls):
        string = dals("""
        channel_alias: https://10.2.3.4:8080/conda/t/tk-123-45
        migrated_channel_aliases:
          - https://conda.anaconda.org
          - http://10.2.3.4:7070/conda
        """)
        reset_context()
        rd = odict(testdata=YamlRawParameter.make_raw_parameters('testdata', yaml_load(string)))
        context._add_raw_data(rd)
        Channel._reset_state()

        cls.platform = context.subdir

    @classmethod
    def tearDownClass(cls):
        reset_context()

    def test_channel_alias_w_conda_path(self):
        channel = Channel('bioconda')
        assert channel.channel_name == "bioconda"
        assert channel.channel_location == "10.2.3.4:8080/conda"
        assert channel.platform is None
        assert channel.package_filename is None
        assert channel.auth is None
        assert channel.scheme == "https"
        assert channel.canonical_name == 'bioconda'
        assert channel.urls() == [
            "https://10.2.3.4:8080/conda/bioconda/%s" % self.platform,
            "https://10.2.3.4:8080/conda/bioconda/noarch",
        ]
        assert channel.token == "tk-123-45"

    def test_channel_alias_w_subhcnnale(self):
        channel = Channel('bioconda/label/dev')
        assert channel.channel_name == "bioconda/label/dev"
        assert channel.channel_location == "10.2.3.4:8080/conda"
        assert channel.platform is None
        assert channel.package_filename is None
        assert channel.auth is None
        assert channel.scheme == "https"
        assert channel.canonical_name == 'bioconda/label/dev'
        assert channel.urls() == [
            "https://10.2.3.4:8080/conda/bioconda/label/dev/%s" % self.platform,
            "https://10.2.3.4:8080/conda/bioconda/label/dev/noarch",
        ]
        assert channel.token == "tk-123-45"

    def test_custom_token_in_channel(self):
        channel = Channel("https://10.2.3.4:8080/conda/t/x1029384756/bioconda")
        assert channel.channel_name == "bioconda"
        assert channel.channel_location == "10.2.3.4:8080/conda"
        assert channel.platform is None
        assert channel.package_filename is None
        assert channel.auth is None
        assert channel.token == "x1029384756"
        assert channel.scheme == "https"
        assert channel.canonical_name == 'bioconda'
        assert channel.urls() == [
            "https://10.2.3.4:8080/conda/bioconda/%s" % self.platform,
            "https://10.2.3.4:8080/conda/bioconda/noarch",
        ]

    def test_canonicalized_url_gets_correct_token(self):
        channel = Channel("bioconda")
        assert channel.urls() == [
            "https://10.2.3.4:8080/conda/bioconda/%s" % self.platform,
            "https://10.2.3.4:8080/conda/bioconda/noarch",
        ]
        assert channel.urls(with_credentials=True) == [
            "https://10.2.3.4:8080/conda/t/tk-123-45/bioconda/%s" % self.platform,
            "https://10.2.3.4:8080/conda/t/tk-123-45/bioconda/noarch",
        ]

        channel = Channel("https://10.2.3.4:8080/conda/bioconda")
        assert channel.urls() == [
            "https://10.2.3.4:8080/conda/bioconda/%s" % self.platform,
            "https://10.2.3.4:8080/conda/bioconda/noarch",
        ]
        assert channel.urls(with_credentials=True) == [
            "https://10.2.3.4:8080/conda/t/tk-123-45/bioconda/%s" % self.platform,
            "https://10.2.3.4:8080/conda/t/tk-123-45/bioconda/noarch",
        ]

        channel = Channel("https://10.2.3.4:8080/conda/t/x1029384756/bioconda")
        assert channel.urls() == [
            "https://10.2.3.4:8080/conda/bioconda/%s" % self.platform,
            "https://10.2.3.4:8080/conda/bioconda/noarch",
        ]
        assert channel.urls(with_credentials=True) == [
            "https://10.2.3.4:8080/conda/t/x1029384756/bioconda/%s" % self.platform,
            "https://10.2.3.4:8080/conda/t/x1029384756/bioconda/noarch",
        ]

        # what happens with the token if it's in the wrong places?
        channel = Channel("https://10.2.3.4:8080/t/x1029384756/conda/bioconda")
        assert channel.urls() == [
            "https://10.2.3.4:8080/conda/bioconda/%s" % self.platform,
            "https://10.2.3.4:8080/conda/bioconda/noarch",
        ]
        assert channel.urls(with_credentials=True) == [
            "https://10.2.3.4:8080/conda/t/x1029384756/bioconda/%s" % self.platform,
            "https://10.2.3.4:8080/conda/t/x1029384756/bioconda/noarch",
        ]


class CustomConfigChannelTests(TestCase):
    """
    Some notes about the tests in this class:
      * The 'pkgs/free' channel is 'migrated' while the 'pkgs/pro' channel is not.
        Thus test_pkgs_free and test_pkgs_pro have substantially different behavior.
    """

    @classmethod
    def setUpClass(cls):
        string = dals("""
        custom_channels:
          darwin: https://some.url.somewhere/stuff
          chuck: http://user1:pass2@another.url:8080/t/tk-1234/with/path
          pkgs/free: http://192.168.0.15:8080
        migrated_custom_channels:
          darwin: s3://just/cant
          chuck: file:///var/lib/repo/
          pkgs/free: https://repo.continuum.io
        migrated_channel_aliases:
          - https://conda.anaconda.org
        channel_alias: ftp://new.url:8082
        default_channels:
          - http://192.168.0.15:8080/pkgs/free
          - http://192.168.0.15:8080/pkgs/pro
          - http://192.168.0.15:8080/pkgs/msys2
        """)
        reset_context()
        rd = odict(testdata=YamlRawParameter.make_raw_parameters('testdata', yaml_load(string)))
        context._add_raw_data(rd)
        Channel._reset_state()

        cls.platform = context.subdir

        cls.DEFAULT_URLS = ['http://192.168.0.15:8080/pkgs/free/%s' % cls.platform,
                            'http://192.168.0.15:8080/pkgs/free/noarch',
                            'http://192.168.0.15:8080/pkgs/pro/%s' % cls.platform,
                            'http://192.168.0.15:8080/pkgs/pro/noarch',
                            'http://192.168.0.15:8080/pkgs/msys2/%s' % cls.platform,
                            'http://192.168.0.15:8080/pkgs/msys2/noarch',
                            ]

    @classmethod
    def tearDownClass(cls):
        reset_context()

    def test_pkgs_free(self):
        channel = Channel('pkgs/free')
        assert channel.channel_name == "pkgs/free"
        assert channel.channel_location == "192.168.0.15:8080"
        assert channel.canonical_name == "defaults"
        assert channel.urls() == [
            'http://192.168.0.15:8080/pkgs/free/%s' % self.platform,
            'http://192.168.0.15:8080/pkgs/free/noarch',
        ]

        channel = Channel('https://repo.continuum.io/pkgs/free')
        assert channel.channel_name == "pkgs/free"
        assert channel.channel_location == "192.168.0.15:8080"
        assert channel.canonical_name == "defaults"
        assert channel.urls() == [
            'http://192.168.0.15:8080/pkgs/free/%s' % self.platform,
            'http://192.168.0.15:8080/pkgs/free/noarch',
        ]

        channel = Channel('https://repo.continuum.io/pkgs/free/noarch')
        assert channel.channel_name == "pkgs/free"
        assert channel.channel_location == "192.168.0.15:8080"
        assert channel.canonical_name == "defaults"
        assert channel.urls() == [
            'http://192.168.0.15:8080/pkgs/free/noarch',
        ]

        channel = Channel('https://repo.continuum.io/pkgs/free/label/dev')
        assert channel.channel_name == "pkgs/free/label/dev"
        assert channel.channel_location == "192.168.0.15:8080"
        assert channel.canonical_name == "pkgs/free/label/dev"
        assert channel.urls() == [
            'http://192.168.0.15:8080/pkgs/free/label/dev/%s' % self.platform,
            'http://192.168.0.15:8080/pkgs/free/label/dev/noarch',
        ]

        channel = Channel('https://repo.continuum.io/pkgs/free/noarch/flask-1.0.tar.bz2')
        assert channel.channel_name == "pkgs/free"
        assert channel.channel_location == "192.168.0.15:8080"
        assert channel.platform == "noarch"
        assert channel.package_filename == "flask-1.0.tar.bz2"
        assert channel.canonical_name == "defaults"
        assert channel.urls() == [
            'http://192.168.0.15:8080/pkgs/free/noarch',
        ]

    def test_pkgs_pro(self):
        channel = Channel('pkgs/pro')
        assert channel.channel_name == "pkgs/pro"
        assert channel.channel_location == "192.168.0.15:8080"
        assert channel.canonical_name == "defaults"
        assert channel.urls() == [
            'http://192.168.0.15:8080/pkgs/pro/%s' % self.platform,
            'http://192.168.0.15:8080/pkgs/pro/noarch',
        ]

        channel = Channel('https://repo.continuum.io/pkgs/pro')
        assert channel.channel_name == "pkgs/pro"
        assert channel.channel_location == "repo.continuum.io"
        assert channel.canonical_name == "defaults"
        assert channel.urls() == [
            'https://repo.continuum.io/pkgs/pro/%s' % self.platform,
            'https://repo.continuum.io/pkgs/pro/noarch',
        ]

        channel = Channel('https://repo.continuum.io/pkgs/pro/noarch')
        assert channel.channel_name == "pkgs/pro"
        assert channel.channel_location == "repo.continuum.io"
        assert channel.canonical_name == "defaults"
        assert channel.urls() == [
            'https://repo.continuum.io/pkgs/pro/noarch',
        ]

        channel = Channel('https://repo.continuum.io/pkgs/pro/label/dev')
        assert channel.channel_name == "pkgs/pro/label/dev"
        assert channel.channel_location == "repo.continuum.io"
        assert channel.canonical_name == "pkgs/pro/label/dev"
        assert channel.urls() == [
            'https://repo.continuum.io/pkgs/pro/label/dev/%s' % self.platform,
            'https://repo.continuum.io/pkgs/pro/label/dev/noarch',
        ]

        channel = Channel('https://repo.continuum.io/pkgs/pro/noarch/flask-1.0.tar.bz2')
        assert channel.channel_name == "pkgs/pro"
        assert channel.channel_location == "repo.continuum.io"
        assert channel.platform == "noarch"
        assert channel.package_filename == "flask-1.0.tar.bz2"
        assert channel.canonical_name == "defaults"
        assert channel.urls() == [
            'https://repo.continuum.io/pkgs/pro/noarch',
        ]

    def test_custom_channels(self):
        channel = Channel('darwin')
        assert channel.channel_name == "darwin"
        assert channel.channel_location == "some.url.somewhere/stuff"

        channel = Channel('https://some.url.somewhere/stuff/darwin')
        assert channel.channel_name == "darwin"
        assert channel.channel_location == "some.url.somewhere/stuff"

        channel = Channel('https://some.url.somewhere/stuff/darwin/label/dev')
        assert channel.channel_name == "darwin/label/dev"
        assert channel.channel_location == "some.url.somewhere/stuff"
        assert channel.platform is None

        channel = Channel('https://some.url.somewhere/stuff/darwin/label/dev/linux-64')
        assert channel.channel_name == "darwin/label/dev"
        assert channel.channel_location == "some.url.somewhere/stuff"
        assert channel.platform == 'linux-64'
        assert channel.package_filename is None

        channel = Channel('https://some.url.somewhere/stuff/darwin/label/dev/linux-64/flask-1.0.tar.bz2')
        assert channel.channel_name == "darwin/label/dev"
        assert channel.channel_location == "some.url.somewhere/stuff"
        assert channel.platform == 'linux-64'
        assert channel.package_filename == 'flask-1.0.tar.bz2'
        assert channel.auth is None
        assert channel.token is None
        assert channel.scheme == "https"

        channel = Channel('https://some.url.somewhere/stuff/darwin/label/dev/linux-64/flask-1.0.tar.bz2')
        assert channel.channel_name == "darwin/label/dev"
        assert channel.channel_location == "some.url.somewhere/stuff"
        assert channel.platform == 'linux-64'
        assert channel.package_filename == 'flask-1.0.tar.bz2'
        assert channel.auth is None
        assert channel.token is None
        assert channel.scheme == "https"

    def test_custom_channels_port_token_auth(self):
        channel = Channel('chuck')
        assert channel.channel_name == "chuck"
        assert channel.channel_location == "another.url:8080/with/path"
        assert channel.auth == 'user1:pass2'
        assert channel.token == 'tk-1234'
        assert channel.scheme == "http"

        channel = Channel('https://another.url:8080/with/path/chuck/label/dev/linux-64/flask-1.0.tar.bz2')
        assert channel.channel_name == "chuck/label/dev"
        assert channel.channel_location == "another.url:8080/with/path"
        assert channel.auth == 'user1:pass2'
        assert channel.token == 'tk-1234'
        assert channel.scheme == "https"
        assert channel.platform == 'linux-64'
        assert channel.package_filename == 'flask-1.0.tar.bz2'

    def test_migrated_custom_channels(self):
        channel = Channel('s3://just/cant/darwin/osx-64')
        assert channel.channel_name == "darwin"
        assert channel.channel_location == "some.url.somewhere/stuff"
        assert channel.platform == 'osx-64'
        assert channel.package_filename is None
        assert channel.auth is None
        assert channel.token is None
        assert channel.scheme == "https"
        assert channel.canonical_name == "darwin"
        assert channel.url() == "https://some.url.somewhere/stuff/darwin/osx-64"
        assert channel.urls() == [
            "https://some.url.somewhere/stuff/darwin/osx-64",
            "https://some.url.somewhere/stuff/darwin/noarch",
        ]
        assert Channel(channel.canonical_name).urls() == [
            "https://some.url.somewhere/stuff/darwin/%s" % self.platform,
            "https://some.url.somewhere/stuff/darwin/noarch",
        ]

        channel = Channel('https://some.url.somewhere/stuff/darwin/noarch/a-mighty-fine.tar.bz2')
        assert channel.channel_name == "darwin"
        assert channel.channel_location == "some.url.somewhere/stuff"
        assert channel.platform == 'noarch'
        assert channel.package_filename == 'a-mighty-fine.tar.bz2'
        assert channel.auth is None
        assert channel.token is None
        assert channel.scheme == "https"
        assert channel.canonical_name == "darwin"
        assert channel.url() == "https://some.url.somewhere/stuff/darwin/noarch/a-mighty-fine.tar.bz2"
        assert channel.urls() == [
            "https://some.url.somewhere/stuff/darwin/noarch",
        ]
        assert Channel(channel.canonical_name).urls() == [
            "https://some.url.somewhere/stuff/darwin/%s" % self.platform,
            "https://some.url.somewhere/stuff/darwin/noarch",
        ]

    def test_local_channel(self):
        local = Channel('local')
        assert local.canonical_name == "local"
        build_path = path_to_url(context.local_build_root)
        local_urls = ['%s/%s/' % (build_path, context.subdir),
                      '%s/noarch/' % build_path]
        assert local.urls == local_urls

        lc = Channel(build_path)
        assert lc.canonical_name == "local"
        assert lc.urls == local_urls

        lc_noarch = Channel(local_urls[1])
        assert lc_noarch.canonical_name == "local"
        assert lc_noarch.urls == local_urls

    def test_canonical_name(self):
        assert Channel('https://repo.continuum.io/pkgs/free').canonical_name == "defaults"
        assert Channel('http://repo.continuum.io/pkgs/free/linux-64').canonical_name == "defaults"
        assert Channel('https://conda.anaconda.org/bioconda').canonical_name == "bioconda"
        assert Channel('http://conda.anaconda.org/bioconda/win-64').canonical_name == "bioconda"
        assert Channel('http://conda.anaconda.org/bioconda/label/main/osx-64').canonical_name == "bioconda/label/main"
        assert Channel('http://conda.anaconda.org/t/tk-abc-123-456/bioconda/win-64').canonical_name == "bioconda"

    def test_urls_from_name(self):
        platform = context.subdir
        assert Channel("bioconda").urls == ["https://conda.anaconda.org/bioconda/%s/" % platform,
                                            "https://conda.anaconda.org/bioconda/noarch/"]
        assert Channel("bioconda/label/dev").urls == [
            "https://conda.anaconda.org/bioconda/label/dev/%s/" % platform,
            "https://conda.anaconda.org/bioconda/label/dev/noarch/"]

    def test_regular_url_channels(self):
        platform = context.subdir
        c = Channel('https://some.other.com/pkgs/free/')
        assert c.canonical_name == "https://some.other.com/pkgs/free"
        assert c.urls == ["https://some.other.com/pkgs/free/%s/" % platform,
                          "https://some.other.com/pkgs/free/noarch/"]

        c = Channel('https://some.other.com/pkgs/free/noarch')
        assert c.canonical_name == "https://some.other.com/pkgs/free"
        assert c.urls == ["https://some.other.com/pkgs/free/%s/" % platform,
                          "https://some.other.com/pkgs/free/noarch/"]

    def test_auth(self):
        assert Channel('http://user:pass@conda.anaconda.org/t/tk-abc-123-456/bioconda/win-64').canonical_name == "bioconda"
        assert Channel('http://conda.anaconda.org/bioconda/label/main/osx-64')._auth == None
        assert Channel('http://user:pass@conda.anaconda.org/bioconda/label/main/osx-64')._auth == 'user:pass'
        assert Channel('http://user:pass@path/to/repo')._auth == 'user:pass'
        assert Channel('http://user:pass@path/to/repo').canonical_name == 'http://path/to/repo'
