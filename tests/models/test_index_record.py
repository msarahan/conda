# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals
from logging import getLogger

from conda.models.index_record import IndexRecord

log = getLogger(__name__)


def test_index_record_timestamp():
    # regression test for #6096
    ts = 1507565728
    new_ts = ts * 1000
    rec = IndexRecord(
        name='test-package',
        version='1.2.3',
        build='2',
        build_number=2,
        timestamp=ts
    )
    assert rec.timestamp == new_ts
    assert rec.dump()['timestamp'] == new_ts

    ts = 1507565728999
    new_ts = ts
    rec = IndexRecord(
        name='test-package',
        version='1.2.3',
        build='2',
        build_number=2,
        timestamp=ts
    )
    assert rec.timestamp == new_ts
    assert rec.dump()['timestamp'] == new_ts

    def test_prefix_record_no_channel(self):
        pr = PrefixRecord(
            name='austin',
            version='1.2.3',
            build_string='py34_2',
            build_number=2,
            url="https://repo.continuum.io/pkgs/free/win-32/austin-1.2.3-py34_2.tar.bz2",
            subdir="win-32",
            md5='0123456789',
            files=(),
        )
        assert pr.url == "https://repo.continuum.io/pkgs/free/win-32/austin-1.2.3-py34_2.tar.bz2"
        assert pr.channel.canonical_name == 'defaults'
        assert pr.subdir == "win-32"
        assert pr.fn == "austin-1.2.3-py34_2.tar.bz2"
        channel_str = text_type(Channel("https://repo.continuum.io/pkgs/free/win-32/austin-1.2.3-py34_2.tar.bz2"))
        assert channel_str == "https://repo.continuum.io/pkgs/free"
        assert dict(pr.dump()) == dict(
            name='austin',
            version='1.2.3',
            build='py34_2',
            build_number=2,
            url="https://repo.continuum.io/pkgs/free/win-32/austin-1.2.3-py34_2.tar.bz2",
            md5='0123456789',
            files=(),
            channel=channel_str,
            subdir="win-32",
            fn="austin-1.2.3-py34_2.tar.bz2",
            constrains=(),
            depends=(),
        )

    def test_index_record_timestamp(self):
        # regression test for #6096
        ts = 1507565728
        new_ts = ts * 1000
        rec = PackageRecord(
            name='test-package',
            version='1.2.3',
            build='2',
            build_number=2,
            timestamp=ts
        )
        assert rec.timestamp == new_ts
        assert rec.dump()['timestamp'] == new_ts

        ts = 1507565728999
        new_ts = ts
        rec = PackageRecord(
            name='test-package',
            version='1.2.3',
            build='2',
            build_number=2,
            timestamp=ts
        )
        assert rec.timestamp == new_ts
        assert rec.dump()['timestamp'] == new_ts
