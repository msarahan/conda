# -*- coding: utf-8 -*-
# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import absolute_import, division, print_function, unicode_literals
from logging import getLogger
import operator as op
import re

from .._vendor.toolz import excepts
from ..common.compat import string_types, text_type, with_metaclass
from ..exceptions import InvalidVersionSpec

from ronda import RustyVersion as VersionOrder, RustyVersionSpec as VersionSpec

log = getLogger(__name__)

# normalized_version() is needed by conda-env
# It is currently being pulled from resolve instead, but
# eventually it ought to come from here
def normalized_version(version):
    return VersionOrder(version)


def ver_eval(vtest, spec):
    return VersionSpec(spec).match(vtest)


version_check_re = re.compile(r'^[\*\.\+!_0-9a-z]+$')
version_split_re = re.compile('([0-9]+|[*]+|[^0-9*]+)')
version_cache = {}


class SingleStrArgCachingType(type):

    def __call__(cls, arg):
        if isinstance(arg, cls):
            return arg
        elif isinstance(arg, string_types):
            try:
                return cls._cache_[arg]
            except KeyError:
                val = cls._cache_[arg] = super(SingleStrArgCachingType, cls).__call__(arg)
                return val
        else:
            return super(SingleStrArgCachingType, cls).__call__(arg)


# each token slurps up leading whitespace, which we strip out.
VSPEC_TOKENS = (r'\s*\^[^$]*[$]|'  # regexes
                r'\s*[()|,]|'      # parentheses, logical and, logical or
                r'[^()|,]+')       # everything else


def treeify(spec_str):
    """
    Examples:
        >>> treeify("1.2.3")
        '1.2.3'
        >>> treeify("1.2.3,>4.5.6")
        (',', '1.2.3', '>4.5.6')
        >>> treeify("1.2.3,4.5.6|<=7.8.9")
        ('|', (',', '1.2.3', '4.5.6'), '<=7.8.9')
        >>> treeify("(1.2.3|4.5.6),<=7.8.9")
        (',', ('|', '1.2.3', '4.5.6'), '<=7.8.9')
        >>> treeify("((1.5|((1.6|1.7), 1.8), 1.9 |2.0))|2.1")
        ('|', '1.5', (',', ('|', '1.6', '1.7'), '1.8', '1.9'), '2.0', '2.1')
        >>> treeify("1.5|(1.6|1.7),1.8,1.9|2.0|2.1")
        ('|', '1.5', (',', ('|', '1.6', '1.7'), '1.8', '1.9'), '2.0', '2.1')
    """
    # Converts a VersionSpec expression string into a tuple-based
    # expression tree.
    assert isinstance(spec_str, string_types)
    tokens = re.findall(VSPEC_TOKENS, '(%s)' % spec_str)
    output = []
    stack = []

    def apply_ops(cstop):
        # cstop: operators with lower precedence
        while stack and stack[-1] not in cstop:
            if len(output) < 2:
                raise InvalidVersionSpec(spec_str, "cannot join single expression")
            c = stack.pop()
            r = output.pop()
            # Fuse expressions with the same operator; e.g.,
            #   ('|', ('|', a, b), ('|', c, d))becomes
            #   ('|', a, b, c d)
            # We're playing a bit of a trick here. Instead of checking
            # if the left or right entries are tuples, we're counting
            # on the fact that if we _do_ see a string instead, its
            # first character cannot possibly be equal to the operator.
            r = r[1:] if r[0] == c else (r,)
            left = output.pop()
            left = left[1:] if left[0] == c else (left,)
            output.append((c,)+left+r)

    for item in tokens:
        item = item.strip()
        if item == '|':
            apply_ops('(')
            stack.append('|')
        elif item == ',':
            apply_ops('|(')
            stack.append(',')
        elif item == '(':
            stack.append('(')
        elif item == ')':
            apply_ops('(')
            if not stack or stack[-1] != '(':
                raise InvalidVersionSpec(spec_str, "expression must start with '('")
            stack.pop()
        else:
            output.append(item)
    if stack:
        raise InvalidVersionSpec(spec_str, "unable to convert to expression tree: %s" % stack)
    return output[0]


def untreeify(spec, _inand=False, depth=0):
    """
    Examples:
        >>> untreeify('1.2.3')
        '1.2.3'
        >>> untreeify((',', '1.2.3', '>4.5.6'))
        '1.2.3,>4.5.6'
        >>> untreeify(('|', (',', '1.2.3', '4.5.6'), '<=7.8.9'))
        '(1.2.3,4.5.6)|<=7.8.9'
        >>> untreeify((',', ('|', '1.2.3', '4.5.6'), '<=7.8.9'))
        '(1.2.3|4.5.6),<=7.8.9'
        >>> untreeify(('|', '1.5', (',', ('|', '1.6', '1.7'), '1.8', '1.9'), '2.0', '2.1'))
        '1.5|((1.6|1.7),1.8,1.9)|2.0|2.1'
    """
    if isinstance(spec, tuple):
        if spec[0] == '|':
            res = '|'.join(map(lambda x: untreeify(x, depth=depth + 1), spec[1:]))
            if _inand or depth > 0:
                res = '(%s)' % res
        else:
            res = ','.join(map(lambda x: untreeify(x, _inand=True, depth=depth + 1), spec[1:]))
            if depth > 0:
                res = '(%s)' % res
        return res
    return spec


def compatible_release_operator(x, y):
    return op.__ge__(x, y) and x.startswith(VersionOrder(".".join(text_type(y).split(".")[:-1])))


# This RE matches the operators '==', '!=', '<=', '>=', '<', '>'
# followed by a version string. It rejects expressions like
# '<= 1.2' (space after operator), '<>1.2' (unknown operator),
# and '<=!1.2' (nonsensical operator).
version_relation_re = re.compile(r'^(=|==|!=|<=|>=|<|>|~=)(?![=<>!~])(\S+)$')
regex_split_re = re.compile(r'.*[()|,^$]')
OPERATOR_MAP = {
    '==': op.__eq__,
    '!=': op.__ne__,
    '<=': op.__le__,
    '>=': op.__ge__,
    '<': op.__lt__,
    '>': op.__gt__,
    '=': lambda x, y: x.startswith(y),
    "!=startswith": lambda x, y: not x.startswith(y),
    "~=": compatible_release_operator,
}
OPERATOR_START = frozenset(('=', '<', '>', '!', '~'))

class BaseSpec(object):

    def __init__(self, spec_str, matcher, is_exact):
        self.spec_str = spec_str
        self._is_exact = is_exact
        self.match = matcher

    @property
    def spec(self):
        return self.spec_str

    def is_exact(self):
        return self._is_exact

    def __eq__(self, other):
        try:
            other_spec = other.spec
        except AttributeError:
            other_spec = self.__class__(other).spec
        return self.spec == other_spec

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.spec)

    def __str__(self):
        return self.spec

    def __repr__(self):
        return "%s('%s')" % (self.__class__.__name__, self.spec)

    @property
    def raw_value(self):
        return self.spec

    @property
    def exact_value(self):
        return self.is_exact() and self.spec or None

    def merge(self, other):
        raise NotImplementedError()

    def regex_match(self, spec_str):
        return bool(self.regex.match(spec_str))

    def operator_match(self, spec_str):
        return self.operator_func(VersionOrder(text_type(spec_str)), self.matcher_vo)

    def any_match(self, spec_str):
        return any(s.match(spec_str) for s in self.tup)

    def all_match(self, spec_str):
        return all(s.match(spec_str) for s in self.tup)

    def exact_match(self, spec_str):
        return self.spec == spec_str

    def always_true_match(self, spec_str):
        return True


@with_metaclass(SingleStrArgCachingType)
class VersionSpec(BaseSpec):  # lgtm [py/missing-equals]
    _cache_ = {}

    def __init__(self, vspec):
        vspec_str, matcher, is_exact = self.get_matcher(vspec)
        super(VersionSpec, self).__init__(vspec_str, matcher, is_exact)

    def get_matcher(self, vspec):

        if isinstance(vspec, string_types) and regex_split_re.match(vspec):
            vspec = treeify(vspec)

        if isinstance(vspec, tuple):
            vspec_tree = vspec
            _matcher = self.any_match if vspec_tree[0] == '|' else self.all_match
            tup = tuple(VersionSpec(s) for s in vspec_tree[1:])
            vspec_str = untreeify((vspec_tree[0],) + tuple(t.spec for t in tup))
            self.tup = tup
            matcher = _matcher
            is_exact = False
            return vspec_str, matcher, is_exact

        vspec_str = text_type(vspec).strip()
        if vspec_str[0] == '^' or vspec_str[-1] == '$':
            if vspec_str[0] != '^' or vspec_str[-1] != '$':
                raise InvalidVersionSpec(vspec_str, "regex specs must start "
                                                    "with '^' and end with '$'")
            self.regex = re.compile(vspec_str)
            matcher = self.regex_match
            is_exact = False
        elif vspec_str[0] in OPERATOR_START:
            m = version_relation_re.match(vspec_str)
            if m is None:
                raise InvalidVersionSpec(vspec_str, "invalid operator")
            operator_str, vo_str = m.groups()
            if vo_str[-2:] == '.*':
                if operator_str in ("=", ">="):
                    vo_str = vo_str[:-2]
                elif operator_str == "!=":
                    vo_str = vo_str[:-2]
                    operator_str = "!=startswith"
                elif operator_str == "~=":
                    raise InvalidVersionSpec(vspec_str, "invalid operator with '.*'")
                else:
                    log.warning("Using .* with relational operator is superfluous and deprecated "
                                "and will be removed in a future version of conda. Your spec was "
                                "{}, but conda is ignoring the .* and treating it as {}"
                                .format(vo_str, vo_str[:-2]))
                    vo_str = vo_str[:-2]
            try:
                self.operator_func = OPERATOR_MAP[operator_str]
            except KeyError:
                raise InvalidVersionSpec(vspec_str, "invalid operator: %s" % operator_str)
            self.matcher_vo = VersionOrder(vo_str)
            matcher = self.operator_match
            is_exact = operator_str == "=="
        elif vspec_str == '*':
            matcher = self.always_true_match
            is_exact = False
        elif '*' in vspec_str.rstrip('*'):
            rx = vspec_str.replace('.', r'\.').replace('+', r'\+').replace('*', r'.*')
            rx = r'^(?:%s)$' % rx
            self.regex = re.compile(rx)
            matcher = self.regex_match
            is_exact = False
        elif vspec_str[-1] == '*':
            if vspec_str[-2:] != '.*':
                vspec_str = vspec_str[:-1] + '.*'

            # if vspec_str[-1] in OPERATOR_START:
            #     m = version_relation_re.match(vspec_str)
            #     if m is None:
            #         raise InvalidVersionSpecError(vspec_str)
            #     operator_str, vo_str = m.groups()
            #
            #
            # else:
            #     pass

            vo_str = vspec_str.rstrip('*').rstrip('.')
            self.operator_func = VersionOrder.startswith
            self.matcher_vo = VersionOrder(vo_str)
            matcher = self.operator_match
            is_exact = False
        elif '@' not in vspec_str:
            self.operator_func = OPERATOR_MAP["=="]
            self.matcher_vo = VersionOrder(vspec_str)
            matcher = self.operator_match
            is_exact = True
        else:
            matcher = self.exact_match
            is_exact = True
        return vspec_str, matcher, is_exact

    def merge(self, other):
        assert isinstance(other, self.__class__)
        return self.__class__(','.join(sorted((self.raw_value, other.raw_value))))

    def union(self, other):
        assert isinstance(other, self.__class__)
        options = set((self.raw_value, other.raw_value))
        # important: we only return a string here because the parens get gobbled otherwise
        #    this info is for visual display only, not for feeding into actual matches
        return '|'.join(sorted(options))


# TODO: someday switch out these class names for consistency
VersionMatch = VersionSpec


@with_metaclass(SingleStrArgCachingType)
class BuildNumberMatch(BaseSpec):  # lgtm [py/missing-equals]
    _cache_ = {}

    def __init__(self, vspec):
        vspec_str, matcher, is_exact = self.get_matcher(vspec)
        super(BuildNumberMatch, self).__init__(vspec_str, matcher, is_exact)

    def get_matcher(self, vspec):
        try:
            vspec = int(vspec)
        except ValueError:
            pass
        else:
            matcher = self.exact_match
            is_exact = True
            return vspec, matcher, is_exact

        vspec_str = text_type(vspec).strip()
        if vspec_str == '*':
            matcher = self.always_true_match
            is_exact = False
        elif vspec_str.startswith(('=', '<', '>', '!')):
            m = version_relation_re.match(vspec_str)
            if m is None:
                raise InvalidVersionSpec(vspec_str, "invalid operator")
            operator_str, vo_str = m.groups()
            try:
                self.operator_func = OPERATOR_MAP[operator_str]
            except KeyError:
                raise InvalidVersionSpec(vspec_str, "invalid operator: %s" % operator_str)
            self.matcher_vo = VersionOrder(vo_str)
            matcher = self.operator_match

            is_exact = operator_str == "=="
        elif vspec_str[0] == '^' or vspec_str[-1] == '$':
            if vspec_str[0] != '^' or vspec_str[-1] != '$':
                raise InvalidVersionSpec(vspec_str, "regex specs must start "
                                                    "with '^' and end with '$'")
            self.regex = re.compile(vspec_str)
            matcher = self.regex_match
            is_exact = False
        # if hasattr(spec, 'match'):
        #     self.spec = _spec
        #     self.match = spec.match
        else:
            matcher = self.exact_match
            is_exact = True
        return vspec_str, matcher, is_exact

    def merge(self, other):
        if self.raw_value != other.raw_value:
            raise ValueError("Incompatible component merge:\n  - %r\n  - %r"
                             % (self.raw_value, other.raw_value))
        return self.raw_value

    def union(self, other):
        options = set((self.raw_value, other.raw_value))
        return '|'.join(options)

    @property
    def exact_value(self):
        return excepts(ValueError, int(self.raw_value))

    def __str__(self):
        return text_type(self.spec)

    def __repr__(self):
        return text_type(self.spec)
