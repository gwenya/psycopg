"""
Utility module to manipulate queries
"""

# Copyright (C) 2020 The Psycopg Team

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Match, NamedTuple
from typing import Sequence, TYPE_CHECKING
from functools import lru_cache

from . import pq
from . import errors as e
from .sql import Composable
from .abc import Buffer, Query, Params
from ._enums import PyFormat
from ._compat import TypeAlias, TypeGuard
from ._encodings import conn_encoding

if TYPE_CHECKING:
    from .abc import Transformer

MAX_CACHED_STATEMENT_LENGTH = 4096
MAX_CACHED_STATEMENT_PARAMS = 50


class QueryPart(NamedTuple):
    pre: bytes
    item: int | str
    format: PyFormat


class PostgresQuery:
    """
    Helper to convert a Python query and parameters into Postgres format.
    """

    __slots__ = """
        query params types formats
        _tx _want_formats _parts _encoding _order
        """.split()

    def __init__(self, transformer: Transformer):
        self._tx = transformer

        self.params: Sequence[Buffer | None] | None = None
        # these are tuples so they can be used as keys e.g. in prepared stmts
        self.types: tuple[int, ...] = ()

        # The format requested by the user and the ones to really pass Postgres
        self._want_formats: list[PyFormat] | None = None
        self.formats: Sequence[pq.Format] | None = None

        self._encoding = conn_encoding(transformer.connection)
        self._parts: list[QueryPart]
        self.query = b""
        self._order: list[str] | None = None

    def convert(self, query: Query, vars: Params | None) -> None:
        """
        Set up the query and parameters to convert.

        The results of this function can be obtained accessing the object
        attributes (`query`, `params`, `types`, `formats`).
        """
        if isinstance(query, str):
            bquery = query.encode(self._encoding)
        elif isinstance(query, Composable):
            bquery = query.as_bytes(self._tx)
        else:
            bquery = query

        if vars is not None:
            # Avoid caching queries extremely long or with a huge number of
            # parameters. They are usually generated by ORMs and have poor
            # cacheablility (e.g. INSERT ... VALUES (...), (...) with varying
            # numbers of tuples.
            # see https://github.com/psycopg/psycopg/discussions/628
            if (
                len(bquery) <= MAX_CACHED_STATEMENT_LENGTH
                and len(vars) <= MAX_CACHED_STATEMENT_PARAMS
            ):
                f: _Query2Pg = _query2pg
            else:
                f = _query2pg_nocache

            (self.query, self._want_formats, self._order, self._parts) = f(
                bquery, self._encoding
            )
        else:
            self.query = bquery
            self._want_formats = self._order = None

        self.dump(vars)

    def dump(self, vars: Params | None) -> None:
        """
        Process a new set of variables on the query processed by `convert()`.

        This method updates `params` and `types`.
        """
        if vars is not None:
            params = self.validate_and_reorder_params(self._parts, vars, self._order)
            assert self._want_formats is not None
            self.params = self._tx.dump_sequence(params, self._want_formats)
            self.types = self._tx.types or ()
            self.formats = self._tx.formats
        else:
            self.params = None
            self.types = ()
            self.formats = None

    @staticmethod
    def is_params_sequence(vars: Params) -> TypeGuard[Sequence[Any]]:
        # Try concrete types, then abstract types
        t = type(vars)
        if t is list or t is tuple:
            sequence = True
        elif t is dict:
            sequence = False
        elif isinstance(vars, Sequence) and not isinstance(vars, (bytes, str)):
            sequence = True
        elif isinstance(vars, Mapping):
            sequence = False
        else:
            raise TypeError(
                "query parameters should be a sequence or a mapping,"
                f" got {type(vars).__name__}"
            )
        return sequence

    @staticmethod
    def validate_and_reorder_params(
        parts: list[QueryPart], vars: Params, order: list[str] | None
    ) -> Sequence[Any]:
        """
        Verify the compatibility between a query and a set of params.
        """

        if PostgresQuery.is_params_sequence(vars):
            if len(vars) != len(parts) - 1:
                raise e.ProgrammingError(
                    f"the query has {len(parts) - 1} placeholders but"
                    f" {len(vars)} parameters were passed"
                )
            if vars and not isinstance(parts[0].item, int):
                raise TypeError("named placeholders require a mapping of parameters")
            return vars

        else:
            if vars and len(parts) > 1 and not isinstance(parts[0][1], str):
                raise TypeError(
                    "positional placeholders (%s) require a sequence of parameters"
                )
            try:
                if order:
                    return [vars[item] for item in order]  # type: ignore[call-overload]
                else:
                    return ()

            except KeyError:
                raise e.ProgrammingError(
                    "query parameter missing:"
                    f" {', '.join(sorted(i for i in order or () if i not in vars))}"
                )


# The type of the _query2pg() and _query2pg_nocache() methods
_Query2Pg: TypeAlias = Callable[
    [bytes, str], tuple[bytes, list[PyFormat], list[str] | None, list[QueryPart]]
]


def _query2pg_nocache(
    query: bytes, encoding: str
) -> tuple[bytes, list[PyFormat], list[str] | None, list[QueryPart]]:
    """
    Convert Python query and params into something Postgres understands.

    - Convert Python placeholders (``%s``, ``%(name)s``) into Postgres
      format (``$1``, ``$2``)
    - placeholders can be %s, %t, or %b (auto, text or binary)
    - return ``query`` (bytes), ``formats`` (list of formats) ``order``
      (sequence of names used in the query, in the position they appear)
      ``parts`` (splits of queries and placeholders).
    """
    parts = _split_query(query, encoding)
    order: list[str] | None = None
    chunks: list[bytes] = []
    formats = []

    if isinstance(parts[0].item, int):
        for part in parts[:-1]:
            assert isinstance(part.item, int)
            chunks.append(part.pre)
            chunks.append(b"$%d" % (part.item + 1))
            formats.append(part.format)

    elif isinstance(parts[0].item, str):
        seen: dict[str, tuple[bytes, PyFormat]] = {}
        order = []
        for part in parts[:-1]:
            assert isinstance(part.item, str)
            chunks.append(part.pre)
            if part.item not in seen:
                ph = b"$%d" % (len(seen) + 1)
                seen[part.item] = (ph, part.format)
                order.append(part.item)
                chunks.append(ph)
                formats.append(part.format)
            else:
                if seen[part.item][1] != part.format:
                    raise e.ProgrammingError(
                        f"placeholder '{part.item}' cannot have different formats"
                    )
                chunks.append(seen[part.item][0])

    # last part
    chunks.append(parts[-1].pre)

    return b"".join(chunks), formats, order, parts


# Note: the cache size is 128 items, but someone has reported throwing ~12k
# queries (of type `INSERT ... VALUES (...), (...)` with a varying amount of
# records), and the resulting cache size is >100Mb. So, we will avoid to cache
# large queries or queries with a large number of params. See
# https://github.com/sqlalchemy/sqlalchemy/discussions/10270
_query2pg = lru_cache()(_query2pg_nocache)


class PostgresClientQuery(PostgresQuery):
    """
    PostgresQuery subclass merging query and arguments client-side.
    """

    __slots__ = ("template",)

    def convert(self, query: Query, vars: Params | None) -> None:
        """
        Set up the query and parameters to convert.

        The results of this function can be obtained accessing the object
        attributes (`query`, `params`, `types`, `formats`).
        """
        if isinstance(query, str):
            bquery = query.encode(self._encoding)
        elif isinstance(query, Composable):
            bquery = query.as_bytes(self._tx)
        else:
            bquery = query

        if vars is not None:
            if (
                len(bquery) <= MAX_CACHED_STATEMENT_LENGTH
                and len(vars) <= MAX_CACHED_STATEMENT_PARAMS
            ):
                f: _Query2PgClient = _query2pg_client
            else:
                f = _query2pg_client_nocache

            (self.template, self._order, self._parts) = f(bquery, self._encoding)
        else:
            self.query = bquery
            self._order = None

        self.dump(vars)

    def dump(self, vars: Params | None) -> None:
        """
        Process a new set of variables on the query processed by `convert()`.

        This method updates `params` and `types`.
        """
        if vars is not None:
            params = self.validate_and_reorder_params(self._parts, vars, self._order)
            self.params = tuple(
                self._tx.as_literal(p) if p is not None else b"NULL" for p in params
            )
            self.query = self.template % self.params
        else:
            self.params = None


_Query2PgClient: TypeAlias = Callable[
    [bytes, str], tuple[bytes, list[str] | None, list[QueryPart]]
]


def _query2pg_client_nocache(
    query: bytes, encoding: str
) -> tuple[bytes, list[str] | None, list[QueryPart]]:
    """
    Convert Python query and params into a template to perform client-side binding
    """
    parts = _split_query(query, encoding, collapse_double_percent=False)
    order: list[str] | None = None
    chunks: list[bytes] = []

    if isinstance(parts[0].item, int):
        for part in parts[:-1]:
            assert isinstance(part.item, int)
            chunks.append(part.pre)
            chunks.append(b"%s")

    elif isinstance(parts[0].item, str):
        seen: dict[str, tuple[bytes, PyFormat]] = {}
        order = []
        for part in parts[:-1]:
            assert isinstance(part.item, str)
            chunks.append(part.pre)
            if part.item not in seen:
                ph = b"%s"
                seen[part.item] = (ph, part.format)
                order.append(part.item)
                chunks.append(ph)
            else:
                chunks.append(seen[part.item][0])
                order.append(part.item)

    # last part
    chunks.append(parts[-1].pre)

    return b"".join(chunks), order, parts


_query2pg_client = lru_cache()(_query2pg_client_nocache)


_re_placeholder = re.compile(
    rb"""(?x)
        %                       # a literal %
        (?:
            (?:
                \( ([^)]+) \)   # or a name in (braces)
                .               # followed by a format
            )
            |
            (?:.)               # or any char, really
        )
        """
)


def _split_query(
    query: bytes, encoding: str = "ascii", collapse_double_percent: bool = True
) -> list[QueryPart]:
    parts: list[tuple[bytes, Match[bytes] | None]] = []
    cur = 0

    # pairs [(fragment, match], with the last match None
    m = None
    for m in _re_placeholder.finditer(query):
        pre = query[cur : m.span(0)[0]]
        parts.append((pre, m))
        cur = m.span(0)[1]
    if m:
        parts.append((query[cur:], None))
    else:
        parts.append((query, None))

    rv = []

    # drop the "%%", validate
    i = 0
    phtype = None
    while i < len(parts):
        pre, m = parts[i]
        if m is None:
            # last part
            rv.append(QueryPart(pre, 0, PyFormat.AUTO))
            break

        ph = m.group(0)
        if ph == b"%%":
            # unescape '%%' to '%' if necessary, then merge the parts
            if collapse_double_percent:
                ph = b"%"
            pre1, m1 = parts[i + 1]
            parts[i + 1] = (pre + ph + pre1, m1)
            del parts[i]
            continue

        if ph == b"%(":
            raise e.ProgrammingError(
                "incomplete placeholder:"
                f" '{query[m.span(0)[0]:].split()[0].decode(encoding)}'"
            )
        elif ph == b"% ":
            # explicit message for a typical error
            raise e.ProgrammingError(
                "incomplete placeholder: '%'; if you want to use '%' as an"
                " operator you can double it up, i.e. use '%%'"
            )
        elif ph[-1:] not in b"sbt":
            raise e.ProgrammingError(
                "only '%s', '%b', '%t' are allowed as placeholders, got"
                f" '{m.group(0).decode(encoding)}'"
            )

        # Index or name
        item: int | str
        item = m.group(1).decode(encoding) if m.group(1) else i

        if not phtype:
            phtype = type(item)
        elif phtype is not type(item):
            raise e.ProgrammingError(
                "positional and named placeholders cannot be mixed"
            )

        format = _ph_to_fmt[ph[-1:]]
        rv.append(QueryPart(pre, item, format))
        i += 1

    return rv


_ph_to_fmt = {
    b"s": PyFormat.AUTO,
    b"t": PyFormat.TEXT,
    b"b": PyFormat.BINARY,
}
