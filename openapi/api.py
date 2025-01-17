import os
import sqlite3
from pathlib import Path
from typing import Dict
from urllib.parse import ParseResult, parse_qsl, urlencode, urlparse, urlunparse

import click
import connexion
import requests
import yaml
from connexion import problem
from flask import current_app, request
from sqlalchemy import create_engine
from werkzeug.exceptions import NotFound


def initdb(table_name):
    import pandas as pd

    df = pd.read_csv(f"{table_name}.csv", index_col="key")
    engine = create_engine("sqlite:///datastore.db", echo=False)
    df.to_sql(f"{table_name}", con=engine, if_exists="replace")


def get_status():
    url = urlparse(request.url)
    path = os.path.join(*(url.path.split("/")[:-1] + ["ui"]))
    return problem(
        status=200,
        title="Ok",
        detail="Vocabularies API is working properly.",
        ext={
            "service_desc": ParseResult(
                **dict(url._asdict(), path=path, query=None)
            ).geturl()
        },
    )


def sql_execute(*args):
    return current_app.config["db"].execute(*args)


# @lru_cache(maxsize=128)
def list_tables():
    cur = sql_execute(
        """SELECT name FROM sqlite_master WHERE
           (type = 'table' OR type= 'view')
           AND name NOT LIKE 'sqlite_%'
           AND name LIKE '%#meta';"""
    )
    for (table_name,) in cur.fetchall():
        vocabulary = sql_execute(f"""SELECT * FROM '{table_name}'""")
        vocabulary.cursor.row_factory = sqlite3.Row
        yield dict(vocabulary.cursor.fetchone())


def last_version(vocabulary_id):
    vocabularies = sql_execute(
        f"""SELECT name FROM sqlite_master WHERE
           (type = 'table' OR type= 'view')
           AND name NOT LIKE 'sqlite_%'
           AND name LIKE '{vocabulary_id}#%#meta'
           ORDER BY name
           LIMIT 1;"""
    )
    vocabularies.cursor.row_factory = sqlite3.Row
    ret = dict(vocabularies.cursor.fetchone())

    vocabulary = sql_execute(
        f"""SELECT *
    FROM "{ret['name']}";"""
    )
    vocabulary.cursor.row_factory = sqlite3.Row
    return dict(vocabulary.cursor.fetchone(), **ret)


def list_vocabularies():
    vocabularies = list(list_tables())
    ret = {
        "entries": [
            {
                "href": os.path.join(request.url, vocabulary["name"]),
                "version": vocabulary["version"],
                "name": vocabulary["name"],
                "url": vocabulary["url"],
            }
            for vocabulary in vocabularies
        ]
    }

    return ret


def test_list_vocabularies():
    assert "entries" in list_vocabularies()


def update_url(start_url, query: Dict = None):

    request_url = urlparse(start_url)
    request_query = dict(parse_qsl(request_url.query))
    request_query.update(query)
    url = list(request_url)
    url[-2] = urlencode(request_query)  # replace query component.
    return urlunparse(url)


# @lru_cache(maxsize=128)
def list_entries(vocabulary_id, limit=100, cursor="", **params):
    vocabulary = last_version(vocabulary_id)
    if not vocabulary:
        raise NotFound(f"Vocabulary: {vocabulary_id}")
    table_name = vocabulary["name"][:-5]

    query = (
        f"""SELECT * FROM "{table_name}"
        WHERE key >= ?
        LIMIT {limit}""",
        (cursor,),
    )

    label_param = list(set(params) & {"label_it", "label_en"})[0:1]
    if label_param:
        label_param = label_param[0]
        query = (
            f"""SELECT * FROM "{table_name}"
        WHERE key >= ?
        AND {label_param} LIKE ?
        LIMIT {limit}""",
            (cursor, params[label_param]),
        )

    entries = sql_execute(*query)
    # Format entries as dictionaries.
    entries.cursor.row_factory = sqlite3.Row
    ret = entries.cursor.fetchall()
    ret = [dict(x) for x in ret] if ret else []

    last_cursor = next(iter(ret[-1].values())) if ret else ""

    url_next = update_url(request.url, {"cursor": last_cursor})
    ret = {
        "count": len(ret),
        "last": last_cursor,
        "url": url_next,
        "entries": ret,
        "version": vocabulary["version"],
    }

    headers = {"Content-Type": "application/json", "cache-control": "max-age=36000"}
    if request.headers.get("Accept") == "application/ld+json":
        ret["@context"] = yaml.load(vocabulary["context"])
        headers.update({"Content-Type": "application/ld+json"})

    return ret, 200, headers


def test_list_entries():
    ret = list_entries("countries", 10)
    assert len(ret["entries"]) == 10


def get_entry(vocabulary_id, entry_id):
    vocabulary = last_version(vocabulary_id)
    if not vocabulary:
        raise NotFound(f"Vocabulary: {vocabulary_id}")
    table_name = vocabulary["name"][:-5]

    entries = current_app.config["db"].execute(
        f"""SELECT * FROM '{table_name}' WHERE key = ?""", (entry_id,)
    )

    entries.cursor.row_factory = sqlite3.Row
    ret = entries.cursor.fetchone()
    if not ret:
        raise NotFound

    res = dict(ret)
    headers = {"Content-Type": "application/json", "cache-control": "max-age=36000"}
    if request.headers.get("Accept") == "application/ld+json":
        res["@context"] = yaml.load(vocabulary["context"])
        headers.update({"Content-Type": "application/ld+json"})
    # import pdb; pdb.set_trace()
    return res, 200, headers


def test_get_entry():
    ret = get_entry("countries", "ITA")
    assert ret.get("label_en") == "Italy"


@click.command()
@click.option("--dbpath", default="datastore", help="Path to sqlite datafile")
@click.option(
    "--dburl",
    help="Url to sqlite datafile",
    default=os.environ.get("NDC_RESTAPI_DATASTORE_URL"),
)
@click.option("--port", default=8080, help="The port.")
def main(dbpath, dburl, port):
    zapp = connexion.FlaskApp(__name__, server="tornado")

    if not Path(f"/tmp/{dbpath}.db").exists() and dburl:
        zapp.app.logger.info(f"Downloading database from {dburl}.")
        Path(f"/tmp/{dbpath}.db").write_bytes(requests.get(dburl).content)
        zapp.app.logger.warning(f"Database downloaded successfully from {dburl}.")

    # validate_db or die.

    zapp.add_api("vocabularies.yaml", validate_responses=False)
    zapp.app.config.update(
        {"db": create_engine(f"sqlite:////tmp/{dbpath}.db", echo=True)}
    )

    zapp.run(port=port)


if __name__ == "__main__":
    main()
