# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals
from unittest import TestCase

import pytest

from conda._vendor.auxlib.collection import AttrDict
from conda.base.context import reset_context
from conda.common.io import captured, env_var
from conda.exceptions import DryRunExit, CondaSystemExit

try:
    from unittest.mock import Mock, patch
except ImportError:
    from mock import Mock, patch


class ConfirmTests(TestCase):

    @patch("sys.stdin.readline", side_effect=('blah\n', 'y\n'))
    def test_confirm_yn_yes(self, stdin_mock):
        args = AttrDict({
            'dry_run': False,
        })
        from conda.cli.common import confirm_yn
        with captured() as c:
            choice = confirm_yn()
        assert choice is True
        assert "Invalid choice" in c.stdout

    @patch("sys.stdin.readline", return_value='n\n')
    def test_confirm_yn_no(self, stdin_mock):
        args = AttrDict({
            'dry_run': False,
        })
        from conda.cli.common import confirm_yn
        with pytest.raises(CondaSystemExit):
            confirm_yn(args)

    def test_dry_run_exit(self):
        with env_var('CONDA_DRY_RUN', 'true', reset_context):
            from conda.cli.common import confirm_yn
            with pytest.raises(DryRunExit):
                confirm_yn()

            from conda.cli.common import confirm
            with pytest.raises(DryRunExit):
                confirm()

    def test_always_yes(self):
        with env_var('CONDA_ALWAYS_YES', 'true', reset_context):
            with env_var('CONDA_DRY_RUN', 'false', reset_context):
                from conda.cli.common import confirm_yn
                choice = confirm_yn()
                assert choice is True
