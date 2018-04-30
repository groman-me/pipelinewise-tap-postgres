import tap_postgres.db as post_db
import psycopg2
import psycopg2.extras
from psycopg2.extensions import quote_ident
import json
import singer
from singer import utils
import copy
import singer.metrics as metrics

LOGGER = singer.get_logger()

UPDATE_BOOKMARK_PERIOD = 1000

def fetch_max_replication_key(conn_config, replication_key, schema_name, table_name):
    with post_db.open_connection(conn_config, False) as conn:
        with conn.cursor() as cur:
            max_key_sql = """SELECT max({})
                              FROM {}""".format(post_db.prepare_columns_sql(replication_key),
                                                post_db.fully_qualified_table_name(schema_name, table_name))
            LOGGER.info("determine max replication key value: %s", max_key_sql)
            cur.execute(max_key_sql)
            max_key = cur.fetchone()[0]
            LOGGER.info("max replication key value: %s", max_key)
            return max_key

def sync_table(conn_info, stream, state, desired_columns, md_map):
    time_extracted = utils.now()

    first_run = singer.get_bookmark(state, stream.tap_stream_id, 'version') is None
    stream_version = singer.get_bookmark(state, stream.tap_stream_id, 'version')
    if stream_version is None:
        stream_version = int(time.time() * 1000)

    state = singer.write_bookmark(state,
                                  stream.tap_stream_id,
                                  'version',
                                  stream_version)
    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

    schema_name = md_map.get(()).get('schema-name')

    escaped_columns = map(post_db.prepare_columns_sql, desired_columns)

    activate_version_message = singer.ActivateVersionMessage(
        stream=stream.stream,
        version=stream_version)

    if first_run:
        singer.write_message(activate_version_message)

    replication_key = md_map.get((), {}).get('replication-key')
    replication_key_value = singer.get_bookmark(state, stream.tap_stream_id, 'replication_key_value')
    replication_key_sql_datatype = md_map.get(('properties', replication_key)).get('sql-datatype')

    with metrics.record_counter(None) as counter:
        with post_db.open_connection(conn_info) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                LOGGER.info("Beginning new incremental replication sync %s", stream_version)
                select_sql = """SELECT {}
                                  FROM {}
                                  WHERE {} >= '{}'::{}
                                  ORDER BY {} ASC""".format(','.join(escaped_columns),
                                                            post_db.fully_qualified_table_name(schema_name, stream.table),
                                                            post_db.prepare_columns_sql(replication_key), replication_key_value, replication_key_sql_datatype,
                                                            post_db.prepare_columns_sql(replication_key))



                LOGGER.info("SELECT STATEMENT: %s", select_sql)
                cur.execute(select_sql)

                rows_saved = 0
                rec = cur.fetchone()
                while rec is not None:
                    record_message = post_db.selected_row_to_singer_message(stream, rec, stream_version, desired_columns, time_extracted, md_map)
                    singer.write_message(record_message)
                    rows_saved = rows_saved + 1

                    #Picking a replication_key with NULL values will result in it ALWAYS been synced which is not great
                    #event worse would be allowing the NULL value to enter into the state
                    if record_message.record[replication_key] is not None:
                        state = singer.write_bookmark(state,
                                                      stream.tap_stream_id,
                                                      'replication_key_value',
                                                      record_message.record[replication_key])


                    if rows_saved % UPDATE_BOOKMARK_PERIOD == 0:
                        singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

                    counter.increment()
                    rec = cur.fetchone()

    #always send the activate version whether first run or subsequent
    singer.write_message(activate_version_message)

    return state
