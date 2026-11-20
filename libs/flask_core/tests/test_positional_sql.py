"""Tests for `$N` -> adapter-placeholder conversion (flask_core.positional_sql).

regression: the old `str.replace` loop in AsyncDAL.execute mishandled repeated and
out-of-order markers, `$1`/`$10` prefixes, and `$N` inside literals/comments.
Runs against sqlite (always) and a real Postgres (TEST_DATABASE_URL or a throwaway
docker container; skipped, never failed, when neither is available).
"""

import os
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing

import pytest

from flask_core.positional_sql import PositionalSQLError, convert_positional


def test_sequential():
    sql, vals = convert_positional("SELECT * FROM t WHERE a=$1 AND b=$2", ["x", "y"])
    assert sql == "SELECT * FROM t WHERE a=%s AND b=%s"
    assert vals == ["x", "y"]


def test_repeated_marker_expands_params():
    sql, vals = convert_positional("a=$1 OR b=$1", ["x"])
    assert sql == "a=%s OR b=%s"
    assert vals == ["x", "x"]


def test_out_of_order_markers():
    sql, vals = convert_positional("a=$2 AND b=$1", ["one", "two"])
    assert sql == "a=%s AND b=%s"
    assert vals == ["two", "one"]


def test_prefix_collision_one_and_ten():
    params = [f"p{i}" for i in range(1, 11)]
    sql, vals = convert_positional("a=$1 AND b=$10 AND c=$2", params)
    assert sql == "a=%s AND b=%s AND c=%s"
    assert vals == ["p1", "p10", "p2"]


def test_marker_inside_string_literal_untouched():
    sql, vals = convert_positional("SELECT '$1 and ''$2''' , $1", ["x"])
    assert sql == "SELECT '$1 and ''$2''' , %s"
    assert vals == ["x"]


def test_marker_inside_comments_untouched():
    sql, vals = convert_positional("SELECT $1 -- $2\n, /* $3 */ $1", ["x"])
    assert sql == "SELECT %s -- $2\n, /* $3 */ %s"
    assert vals == ["x", "x"]


def test_marker_inside_dollar_quoted_body_untouched():
    sql, vals = convert_positional("SELECT $f$ $1 $f$, $1", ["x"])
    assert sql == "SELECT $f$ $1 $f$, %s"
    assert vals == ["x"]


def test_quoted_identifier_untouched():
    sql, vals = convert_positional('SELECT "a$1" FROM t WHERE x=$1', ["v"])
    assert sql == 'SELECT "a$1" FROM t WHERE x=%s'
    assert vals == ["v"]


def test_missing_parameter_fails_loudly():
    with pytest.raises(PositionalSQLError, match=r"\$3"):
        convert_positional("a=$1 AND b=$3", ["x", "y"])


def test_zero_index_fails_loudly():
    with pytest.raises(PositionalSQLError, match=r"\$0"):
        convert_positional("a=$0", ["x"])


def test_markers_without_params_fails_loudly():
    with pytest.raises(PositionalSQLError):
        convert_positional("a=$1", None)


def test_no_markers_passthrough():
    assert convert_positional("SELECT 1", None) == ("SELECT 1", [])


def test_custom_marker_sqlite():
    sql, vals = convert_positional("a=$2 AND b=$1 AND c=$2", [1, 2], marker="?")
    assert sql == "a=? AND b=? AND c=?"
    assert vals == [2, 1, 2]


def test_sqlite_binds_correct_values():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (a TEXT, b TEXT)")
    conn.execute("INSERT INTO t VALUES ('one', 'two')")
    sql, vals = convert_positional(
        "SELECT a, b FROM t WHERE b=$2 AND a=$1 AND '$1'='$1' AND b=$2", ["one", "two"], marker="?"
    )
    assert conn.execute(sql, vals).fetchall() == [("one", "two")]


# --- real Postgres -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_conn():
    """psycopg2 connection to a real Postgres, or skip when none is reachable."""
    psycopg2 = pytest.importorskip("psycopg2")
    url = os.environ.get("TEST_DATABASE_URL")
    container = None
    if not url:
        if not shutil.which("docker"):
            pytest.skip("no TEST_DATABASE_URL and no docker CLI")
        with closing(socket.socket()) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        name = f"flask-core-pos-{uuid.uuid4().hex[:8]}"
        out = subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=pw",
             "-p", f"127.0.0.1:{port}:5432", "postgres:17"],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            pytest.skip("docker could not start postgres")
        container = name
        url = f"postgresql://postgres:pw@127.0.0.1:{port}/postgres"
    conn = None
    try:
        for _ in range(90):
            try:
                conn = psycopg2.connect(url)
                break
            except psycopg2.OperationalError:
                time.sleep(1)
        if conn is None:
            pytest.fail("postgres never became ready")
        conn.autocommit = True
        yield conn
    finally:
        if conn is not None:
            conn.close()
        if container:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def _pg(conn, sql, params):
    native, vals = convert_positional(sql, params)
    with conn.cursor() as cur:
        cur.execute(native, vals)
        return cur.fetchall()


def test_pg_repeated_and_out_of_order(pg_conn):
    assert _pg(pg_conn, "SELECT $2::text, $1::text, $1::text", ["a", "b"]) == [("b", "a", "a")]


def test_pg_one_and_ten(pg_conn):
    params = [str(i) for i in range(1, 11)]
    assert _pg(pg_conn, "SELECT $10::text, $1::text, $2::text", params) == [("10", "1", "2")]


def test_pg_literal_and_comment_markers_untouched(pg_conn):
    assert _pg(pg_conn, "SELECT '$1'::text, $1::text -- $2\n", ["real"]) == [("$1", "real")]


def test_pg_dollar_quoted_body(pg_conn):
    assert _pg(pg_conn, "SELECT $q$ $1 $q$::text, $1::text", ["real"]) == [(" $1 ", "real")]


async def test_asyncdal_execute_binds_correctly_on_sqlite(tmp_path):
    """AsyncDAL.execute must route through the tokenizer (repeated, out-of-order, literal)."""
    from flask_core.database import AsyncDAL

    adal = AsyncDAL(f"sqlite://{tmp_path}/t.db", pool_size=1, folder=str(tmp_path))
    adal.dal.executesql("CREATE TABLE t (a TEXT, b TEXT)")
    await adal.execute("INSERT INTO t (a, b) VALUES ($1, $2)", ["one", "two"])
    rows = await adal.execute(
        "SELECT a, b FROM t WHERE b=$2 AND a=$1 AND '$1'='$1' AND a=$1", ["one", "two"]
    )
    assert rows == [{"a": "one", "b": "two"}]
