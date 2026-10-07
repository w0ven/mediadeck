"""Card-owned transitions of the existing authoritative whitelist group.

The member row is changed, never a route-specific allow list. Ownership uses a
revision, so even an administrator explicitly selecting the same group wins.
"""
from __future__ import annotations

import time

from app.modules.economy_rules import economy_write
from app.modules.groups import WHITELIST_GROUP_ID


def owned_grant(db, member):
    grant = db.one("SELECT * FROM whitelist_card_grants WHERE emby_user_id=?",
                   (member["emby_user_id"],))
    if (grant and grant["status"] == "active"
            and member.get("group_id") == WHITELIST_GROUP_ID
            and member.get("group_revision", 0) == grant["group_revision"]):
        return grant
    return None


def project_authoritative_group(db, member, now=None):
    """One effective member for all consumers, even before housekeeping runs.

    Expired card membership cannot authorize a request while the physical
    restoration or remote reconciliation is delayed. This is the same group
    resolver used by the rest of the system, not a whitelist route exception.
    """
    grant = owned_grant(db, member)
    if not grant:
        return
    member["preserve_account_expiry"] = bool(grant["enforce_account_expiry"])
    member["whitelist_card_expires_at"] = grant["expires_at"]
    if grant["expires_at"] is not None and grant["expires_at"] <= (time.time() if now is None else now):
        member["stored_group_id"] = member["group_id"]
        member["group_id"] = grant["previous_group_id"]
        if grant['previous_group_id'] and not db.one('SELECT 1 FROM groups WHERE id=?', (grant['previous_group_id'],)):
            member['card_group_unavailable'] = True


def _restore_local(conn, grant, now):
    current = conn.execute("SELECT * FROM members WHERE emby_user_id=?",
                           (grant["emby_user_id"],)).fetchone()
    if (not current or current["group_id"] != WHITELIST_GROUP_ID
            or current["group_revision"] != grant["group_revision"]):
        conn.execute("UPDATE whitelist_card_grants SET status='protected',synced_at=?,error='' WHERE emby_user_id=?",
                     (now, grant["emby_user_id"]))
        return False
    if grant['previous_group_id'] and not conn.execute('SELECT 1 FROM groups WHERE id=?', (grant['previous_group_id'],)).fetchone():
        raise ValueError('白名单卡原组不存在，恢复待重试/管理员处理')
    conn.execute(
        "UPDATE members SET group_id=?,group_revision=group_revision+1,updated_at=? "
        "WHERE emby_user_id=? AND group_revision=? AND group_id=?",
        (grant["previous_group_id"], now, grant["emby_user_id"], grant["group_revision"], WHITELIST_GROUP_ID),
    )
    conn.execute("UPDATE whitelist_card_grants SET status='restored',synced_at=NULL,error='' WHERE emby_user_id=?",
                 (grant["emby_user_id"],))
    conn.execute("INSERT INTO audit_log(ts,actor,action,subject,detail,ok) VALUES(?,?,?,?,?,1)",
                 (now, "system", "whitelist.card.expire", grant["emby_user_id"], "恢复卡前用户组，基础期限及覆盖不改"))
    return True


def activate(conn, db, members, user, card_id, days, now):
    grant_row = conn.execute("SELECT * FROM whitelist_card_grants WHERE emby_user_id=?", (user,)).fetchone()
    if grant_row:
        grant = dict(grant_row)
        if grant["status"] == "active" and grant["expires_at"] is not None and grant["expires_at"] <= now:
            _restore_local(conn, grant, now)
    member = members.get(user)
    if (not member or member.get("state") != "active" or member.get("emby_missing_since")):
        raise ValueError("账号当前不可用，未消耗白名单卡")
    grant = owned_grant(db, member)
    if member["group_id"] == WHITELIST_GROUP_ID:
        if not grant or grant["expires_at"] is None:
            raise ValueError("你已有永久白名单资格，未消耗道具")
        previous = grant["previous_group_id"]
        enforce_expiry = grant["enforce_account_expiry"]
        expires = max(now, grant["expires_at"]) + days * 86400 if days else None
    else:
        previous = member["group_id"]
        enforce_expiry = int(member.get("expires_at_effective") is not None)
        expires = now + days * 86400 if days else None
    if not members._groups.get(WHITELIST_GROUP_ID):
        raise ValueError("现有白名单用户组不可用，未消耗道具")
    conn.execute("UPDATE members SET group_id=?,group_revision=group_revision+1,updated_at=? WHERE emby_user_id=?",
                 (WHITELIST_GROUP_ID, now, user))
    revision = conn.execute("SELECT group_revision FROM members WHERE emby_user_id=?", (user,)).fetchone()[0]
    conn.execute(
        "INSERT INTO whitelist_card_grants(emby_user_id,previous_group_id,group_revision,enforce_account_expiry,"
        "expires_at,card_id,status,updated_at) VALUES(?,?,?,?,?,?,'active',?) "
        "ON CONFLICT(emby_user_id) DO UPDATE SET previous_group_id=excluded.previous_group_id,"
        "group_revision=excluded.group_revision,enforce_account_expiry=excluded.enforce_account_expiry,"
        "expires_at=excluded.expires_at,card_id=excluded.card_id,status='active',updated_at=excluded.updated_at,"
        "synced_at=NULL,retry_at=0,error=''",
        (user, previous, revision, enforce_expiry, expires, card_id, now),
    )
    return expires


async def reconcile(db, sync_effects):
    now = int(time.time())
    due = db.query(
        "SELECT emby_user_id FROM whitelist_card_grants WHERE retry_at<=? AND "
        "((status='active' AND expires_at IS NOT NULL AND expires_at<=?) "
        "OR (status='restored' AND synced_at IS NULL)) ORDER BY updated_at LIMIT 100", (now, now),
    )
    for record in due:
        user = record["emby_user_id"]
        grant = None
        try:
            with economy_write(db) as conn:
                grant = dict(conn.execute("SELECT * FROM whitelist_card_grants WHERE emby_user_id=?", (user,)).fetchone())
                if grant["status"] == "active":
                    if grant["expires_at"] is None or grant["expires_at"] > now:
                        continue
                    if not _restore_local(conn, grant, now):
                        continue
                elif grant["status"] != "restored" or grant["synced_at"] is not None:
                    continue
            result = await sync_effects(user, {"whitelist_card"})
            ok = result.get("ok") is not False and result.get("remote_ok") is not False
        except Exception:  # noqa: BLE001 - durable local/remote restoration retry
            ok = False
        if grant is None:
            continue
        with economy_write(db) as conn:
            # A concurrent renewal/replacement is never marked by this old attempt.
            conn.execute(
                "UPDATE whitelist_card_grants SET synced_at=?,retry_at=?,error=? "
                "WHERE emby_user_id=? AND group_revision=? AND status IN ('active','restored')",
                (now if ok else None, 0 if ok else now + 60,
                 "" if ok else "白名单卡到期恢复/同步未确认，稍后重试", user, grant["group_revision"]),
            )
