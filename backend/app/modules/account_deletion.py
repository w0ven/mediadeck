"""Serialize self-deletion admission with dice betting; never change settlement."""
from contextlib import contextmanager
from uuid import uuid4

from app.core.errors import ConfigError
from app.modules.dice_banker import BANKER_VERSIONS
from app.modules.economy_rules import economy_write


def deletion_key(user_id):
    return "account.self_delete:" + str(user_id)


def deletion_in_progress(conn, user_id):
    return conn.execute("SELECT 1 FROM meta WHERE key=?", (deletion_key(user_id),)).fetchone() is not None


@contextmanager
def self_deletion_guard(db, user_id):
    token = uuid4().hex
    key = deletion_key(user_id)
    with economy_write(db) as conn:
        if deletion_in_progress(conn, user_id):
            raise ConfigError("账号注销处理中或上次结果待核查，请联系管理员。")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dice_bets'").fetchone():
            pending = conn.execute(
                "SELECT 1 FROM dice_bets b LEFT JOIN dice_rounds r ON r.nonce=b.nonce "
                "WHERE b.user_id=? AND (r.state IS NULL OR r.state NOT IN ('settled','refunded','empty')) LIMIT 1",
                (str(user_id),),
            ).fetchone()
            bank = conn.execute(
                "SELECT 1 FROM dice_rounds WHERE actor_uid=? AND json_extract(rules,'$.version') IN (?,?) "
                "AND state NOT IN ('settled','refunded','empty') LIMIT 1", (str(user_id), *BANKER_VERSIONS),
            ).fetchone()
            if bank:
                raise ConfigError("你有未结算的骰局庄本，请等待结算后再注销；本次未删除账号。")
            if pending:
                raise ConfigError("你有未结算的骰局下注，请等待结算完成后再注销；本次未删除账号。")
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", (key, token))
    # Persist through remote I/O; a process crash deliberately fails closed.
    # Never hold a SQLite transaction while awaiting Emby.
    try:
        yield
    finally:
        with economy_write(db) as conn:
            conn.execute("DELETE FROM meta WHERE key=? AND value=?", (key, token))
