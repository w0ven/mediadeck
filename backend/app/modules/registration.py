"""Registration channels: who is allowed to create an account, and on whose word.

v0.14 removed codes entirely and made every account operator-created. That was
right for a server whose members already existed, and wrong the moment the bot
became the front door: a single global "registration_enabled" switch is either
open to everyone who finds the bot, or closed to everyone including the people
the operator actually wants in.

So the switch is replaced by three channels, each answering "who vouched for
this person":

- **admin**  — the operator pre-authorised this Telegram id. No credential to
  type, because the operator already named them.
- **invite** — an existing member spent one of their invite slots. The new
  account records that member as its inviter, which is what makes the invite
  tree (and cascade delete) mean anything.
- **redeem** — a card the operator generated, carrying its own group and
  duration, so a sold card can be worth more than the default plan.

The critical rule in this file is that **resolve() never consumes anything**.
Admission is decided before the Emby account exists; if creation then fails --
a taken username, Emby unreachable -- the credential must still be good. Only
consume() spends it, and it is called after the account is real. Getting this
backwards means a member pays for a card, hits a duplicate username, and loses
both the card and the account.
"""
from __future__ import annotations

import secrets
import sqlite3
import string
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.db import Database
from app.core.errors import ConfigError

# O/0 and I/1 are removed: these are read off a phone screen and typed back by
# hand, and every support message about a code that "does not work" costs more
# than the four characters of entropy.
ALPHABET = "".join(c for c in (string.ascii_uppercase + string.digits)
                   if c not in "O0I1")

INVITE_LENGTH = 8
REDEEM_LENGTH = 12

# Channels, in the order resolve() tries them.
CHANNELS = ("admin", "invite", "redeem")

# Every member row carries one of these. 'legacy' is the honest label for the
# accounts that predate the bot: pretending they arrived through a channel
# would make the registration-source breakdown a fiction.
REGISTER_SOURCES = ("admin", "invite", "redeem", "legacy")

MAX_BATCH = 500


def generate_code(length: int = INVITE_LENGTH) -> str:
    """Unbiased over the reduced alphabet; secrets, not random."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def normalise(raw: Any) -> str:
    """Codes are compared upper-case with separators stripped.

    Someone typing 'abcd-efgh' means the same code as 'ABCDEFGH', and refusing
    them over a hyphen they added themselves teaches nothing.
    """
    text = str(raw or "").strip().upper()
    return "".join(ch for ch in text if ch.isalnum())


def mask_code(raw: str) -> str:
    """First four and last four. Enough to find a card in a list, not to use it."""
    text = str(raw or "")
    if len(text) <= 8:
        return text[:2] + "*" * max(0, len(text) - 2)
    return f"{text[:4]}{'*' * 4}{text[-4:]}"


@dataclass
class Admission:
    """The verdict on one registration attempt. Carries no side effects."""

    allowed: bool = False
    via: str = ""
    reason: str = ""
    group_id: str = ""
    days: int = 0
    inviter_id: str = ""
    credential: str = ""
    tg_user_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed, "via": self.via, "reason": self.reason,
            "group_id": self.group_id, "days": self.days,
            "inviter_id": self.inviter_id, "tg_user_id": self.tg_user_id,
        }


class RegistrationService:
    """Issues, resolves and consumes the three kinds of admission."""

    def __init__(self, db: Database, groups: Any = None,
                 config_provider: Any = None) -> None:
        self._db = db
        self._groups = groups
        self._config = config_provider

    # -- config ---------------------------------------------------------------

    def _cfg(self) -> dict[str, Any]:
        if self._config is None:
            return {}
        try:
            return self._config() or {}
        except Exception:  # noqa: BLE001 - a broken config must not 500 the bot
            return {}

    def channel_enabled(self, channel: str) -> bool:
        cfg = self._cfg()
        if not cfg:
            return True
        return bool(cfg.get(f"allow_{channel}", True))

    def _default_group(self) -> str:
        configured = str(self._cfg().get("default_group_id") or "").strip()
        if configured:
            return configured
        if self._groups is not None:
            return str(self._groups.default_group_id() or "")
        return ""

    def _default_days(self) -> int:
        try:
            return max(0, int(self._cfg().get("register_days") or 0))
        except (TypeError, ValueError):
            return 0

    # -- invite codes ---------------------------------------------------------

    def issue_invite(self, owner_user_id: str, uses: int = 1,
                     ttl_days: int = 0, *, conn: Any = None) -> dict[str, Any]:
        """Mint a code owned by a member. ttl_days 0 means it never expires."""
        # `int(x or default)` would turn an explicit 0 into the default, which
        # is how "generate zero codes" quietly becomes one.
        uses = _as_int(uses, "邀请次数")
        if uses < 1 or uses > 100:
            raise ConfigError("邀请次数必须在 1–100 之间")
        ttl_days = _as_int(ttl_days, "有效期")
        if ttl_days < 0 or ttl_days > 3650:
            raise ConfigError("有效期必须在 0–3650 天之间")
        now = int(time.time())
        expires = now + ttl_days * 86400 if ttl_days else None
        execute = conn.execute if conn is not None else self._db.execute
        for _ in range(20):
            candidate = generate_code(INVITE_LENGTH)
            try:
                execute(
                    "INSERT INTO invite_codes"
                    "(code,owner_user_id,uses_left,expires_at,created_at,revoked)"
                    " VALUES(?,?,?,?,?,0)",
                    (candidate, str(owner_user_id or ""), uses, expires, now))
            except sqlite3.IntegrityError:
                continue  # astronomically unlikely; still cheaper than failing
            return self.get_invite(candidate) or {}
        raise ConfigError("生成邀请码失败，请重试")

    def get_invite(self, raw: str) -> dict[str, Any] | None:
        return self._db.one(
            "SELECT * FROM invite_codes WHERE code=?", (normalise(raw),))

    def list_invites(self, owner_user_id: str | None = None,
                     limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM invite_codes"
        params: list[Any] = []
        if owner_user_id is not None:
            sql += " WHERE owner_user_id=?"
            params.append(str(owner_user_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit or 200), 1000)))
        rows = self._db.query(sql, tuple(params))
        now = int(time.time())
        for row in rows:
            row["masked"] = mask_code(row["code"])
            row["usable"] = self._invite_usable(row, now) == ""
        return rows

    @staticmethod
    def _invite_usable(row: dict[str, Any], now: int) -> str:
        """'' if the code may be spent, otherwise the Chinese reason it may not."""
        if row.get("revoked"):
            return "这个邀请码已被作废。"
        if int(row.get("uses_left") or 0) <= 0:
            return "这个邀请码的次数已经用完了。"
        expires = row.get("expires_at")
        if expires and now >= int(expires):
            return "这个邀请码已经过期了。"
        return ""

    def revoke_invite(self, raw: str) -> dict[str, Any]:
        row = self.get_invite(raw)
        if not row:
            raise KeyError(raw)
        self._db.execute(
            "UPDATE invite_codes SET revoked=1 WHERE code=?", (row["code"],))
        return self.get_invite(row["code"]) or {}

    # -- member invite quota --------------------------------------------------

    def invite_quota(self, user_id: str) -> int:
        row = self._db.one(
            "SELECT invite_quota FROM members WHERE emby_user_id=?",
            (str(user_id),))
        return int((row or {}).get("invite_quota") or 0)

    def adjust_quota(self, user_id: str, delta: int) -> int:
        """Grant or claw back invite slots. Never goes below zero."""
        with self._db.write() as conn:
            changed = conn.execute(
                'UPDATE members SET invite_quota=MAX(0,COALESCE(invite_quota,0)+?),updated_at=? WHERE emby_user_id=?',
                (int(delta), int(time.time()), str(user_id))).rowcount
            if not changed:
                raise KeyError(user_id)
            return int(conn.execute('SELECT invite_quota FROM members WHERE emby_user_id=?',
                                    (str(user_id),)).fetchone()[0])

    def spend_quota_for_invite(self, owner_user_id: str,
                               ttl_days: int = 0) -> dict[str, Any]:
        """A member turns one of their slots into a single-use code.

        The slot is debited first: if minting somehow failed after the code was
        already handed out, the member would get an unlimited supply.
        """
        with self._db.write() as conn:
            changed = conn.execute(
                'UPDATE members SET invite_quota=invite_quota-1,updated_at=? WHERE emby_user_id=? AND invite_quota>0',
                (int(time.time()), str(owner_user_id))).rowcount
            if not changed:
                raise ConfigError('你没有可用的邀请名额。')
            return self.issue_invite(owner_user_id, uses=1, ttl_days=ttl_days, conn=conn)

    # -- redeem codes ---------------------------------------------------------

    def generate_redeem(self, group_id: str, days: int, count: int = 1,
                        batch: str = "", note: str = "") -> list[dict[str, Any]]:
        """Mint a batch of cards. Each carries its own group and duration."""
        count = _as_int(count, "数量")
        if count < 1 or count > MAX_BATCH:
            raise ConfigError(f"数量必须在 1–{MAX_BATCH} 之间")
        days = _as_int(days, "天数")
        if days < 0 or days > 3650:
            raise ConfigError("天数必须在 0–3650 之间")
        group_id = str(group_id or "").strip()
        if not group_id:
            raise ConfigError("必须选择一个用户组")
        if self._groups is not None and not self._groups.get(group_id):
            raise ConfigError(f"用户组不存在: {group_id}")

        now = int(time.time())
        batch = str(batch or "").strip()[:60] or time.strftime(
            "%Y%m%d-%H%M%S", time.localtime(now))
        note = str(note or "").strip()[:200]
        issued: list[dict[str, Any]] = []
        for _ in range(count):
            for _attempt in range(20):
                candidate = generate_code(REDEEM_LENGTH)
                try:
                    self._db.execute(
                        "INSERT INTO redeem_codes"
                        "(code,group_id,days,status,used_by,used_at,batch,note,"
                        "created_at) VALUES(?,?,?,'unused','',NULL,?,?,?)",
                        (candidate, group_id, days, batch, note, now))
                except sqlite3.IntegrityError:
                    continue
                issued.append(self.get_redeem(candidate) or {})
                break
            else:
                raise ConfigError("生成卡密失败，请重试")
        return issued

    def get_redeem(self, raw: str) -> dict[str, Any] | None:
        return self._db.one(
            "SELECT * FROM redeem_codes WHERE code=?", (normalise(raw),))

    def list_redeem(self, status: str | None = None, batch: str | None = None,
                    limit: int = 500) -> list[dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(str(status))
        if batch:
            clauses.append("batch=?")
            params.append(str(batch))
        sql = "SELECT * FROM redeem_codes"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit or 500), 5000)))
        rows = self._db.query(sql, tuple(params))
        for row in rows:
            row["masked"] = mask_code(row["code"])
            if self._groups is not None:
                group = self._groups.get(row.get("group_id") or "")
                row["group_name"] = group["name"] if group else "(已删除)"
        return rows

    def redeem_batches(self) -> list[str]:
        return [str(r["batch"]) for r in self._db.query(
            "SELECT DISTINCT batch FROM redeem_codes WHERE batch<>''"
            " ORDER BY batch DESC LIMIT 200")]

    def redeem_stats(self) -> dict[str, int]:
        rows = self._db.query(
            "SELECT status, COUNT(*) AS n FROM redeem_codes GROUP BY status")
        counts = {str(r["status"]): int(r["n"] or 0) for r in rows}
        return {
            "unused": counts.get("unused", 0),
            "used": counts.get("used", 0),
            "revoked": counts.get("revoked", 0),
            "total": sum(counts.values()),
        }

    def revoke_redeem(self, raw: str) -> dict[str, Any]:
        """Void an unused card. A spent one stays spent: rewriting history
        would erase the record of what a member was actually given."""
        row = self.get_redeem(raw)
        if not row:
            raise KeyError(raw)
        if row.get("status") == "used":
            raise ConfigError("这张卡密已经被使用，无法作废")
        self._db.execute(
            "UPDATE redeem_codes SET status='revoked' WHERE code=?",
            (row["code"],))
        return self.get_redeem(row["code"]) or {}

    # -- admin pre-authorisation ----------------------------------------------

    def grant_admin(self, tg_user_id: str, granted_by: str = "operator"
                    ) -> dict[str, Any]:
        tg_user_id = str(tg_user_id or "").strip()
        if not tg_user_id or not tg_user_id.lstrip("-").isdigit():
            raise ConfigError("Telegram 用户 ID 必须是数字")
        existing = self.get_grant(tg_user_id)
        if existing and not existing.get("used_at"):
            return existing
        now = int(time.time())
        if existing:
            # Re-granting a spent authorisation re-arms it rather than adding a
            # second row: UNIQUE(tg_user_id) is what keeps "granted" countable.
            self._db.execute(
                "UPDATE admin_grants SET used_at=NULL,granted_by=?,created_at=?,"
                "gift_code=NULL,gift_group_id=NULL,gift_days=NULL,origin_chat_id=NULL,"
                "origin_message_id=NULL,origin_thread_id=NULL,origin_bot_id=NULL WHERE tg_user_id=?",
                (str(granted_by)[:60], now, tg_user_id))
        else:
            self._db.execute(
                "INSERT INTO admin_grants(tg_user_id,granted_by,created_at,used_at)"
                " VALUES(?,?,?,NULL)", (tg_user_id, str(granted_by)[:60], now))
        return self.get_grant(tg_user_id) or {}

    def gift_terms(self, tg_user_id: str) -> tuple[str, int]:
        grant = self.get_grant(tg_user_id)
        if grant and grant.get("gift_code") and not grant.get("used_at"):
            return str(grant.get("gift_group_id") or ""), int(grant.get("gift_days") or 0)
        return self._default_group(), self._default_days()

    def issue_gift(self, tg_user_id: str, granted_by: str, *,
                   origin: dict[str, Any] | None = None) -> dict[str, Any]:
        """One recipient-bound registration qualification, not an Emby account.

        Repeated gifting of an unused qualification returns the same link.
        The link never grants anybody other than this Telegram id admission.
        """
        target = str(tg_user_id).strip()
        if not target.isdigit() or int(target) <= 0:
            raise ConfigError("请提供有效的 Telegram 用户数字 ID")
        if not self.channel_enabled("admin_grant"):
            raise ConfigError("管理员授权注册通道已关闭")
        group, days = self.gift_terms(target)
        if not group or (self._groups is not None and not self._groups.get(group)):
            raise ConfigError("默认用户组不存在，请先在面板设置")
        origin = origin or {}
        chat = str(origin.get('chat_id') or '')
        mid = origin.get('message_id')
        thread = origin.get('thread_id')
        bot_id = str(origin.get('bot_id') or '')
        if origin and (not chat.startswith('-') or not chat[1:].isdigit()
                       or not isinstance(mid, int) or mid <= 0 or not bot_id.isdigit()
                       or (thread is not None and (not isinstance(thread, int) or thread <= 0))):
            raise ConfigError('赠送来源群信息无效，未发放资格')
        source = (chat or None, mid, thread, bot_id or None)
        now = int(time.time())
        for _ in range(20):
            try:
                with self._db.write() as conn:
                    if conn.execute("SELECT 1 FROM members WHERE tg_user_id=?", (target,)).fetchone():
                        raise ConfigError("对方已有账号，请使用用户管理")
                    row = conn.execute("SELECT * FROM admin_grants WHERE tg_user_id=?", (target,)).fetchone()
                    if row and row["gift_code"] and not row["used_at"]:
                        # Reposting a link cannot redirect its original receipt.
                        if origin and not row['origin_chat_id']:
                            conn.execute('UPDATE admin_grants SET origin_chat_id=?,origin_message_id=?, '
                                         'origin_thread_id=?,origin_bot_id=? WHERE id=?', (*source, row['id']))
                            row = conn.execute('SELECT * FROM admin_grants WHERE id=?', (row['id'],)).fetchone()
                        return dict(row)
                    code = "GIFT" + generate_code(12)
                    conn.execute(
                        "INSERT INTO admin_grants(tg_user_id,granted_by,created_at,used_at,"
                        "gift_code,gift_group_id,gift_days,origin_chat_id,origin_message_id,origin_thread_id,origin_bot_id) "
                        "VALUES(?,?,?,NULL,?,?,?,?,?,?,?) "
                        "ON CONFLICT(tg_user_id) DO UPDATE SET granted_by=excluded.granted_by,"
                        "created_at=excluded.created_at,used_at=NULL,gift_code=excluded.gift_code,"
                        "gift_group_id=excluded.gift_group_id,gift_days=excluded.gift_days,"
                        "origin_chat_id=excluded.origin_chat_id,origin_message_id=excluded.origin_message_id,"
                        "origin_thread_id=excluded.origin_thread_id,origin_bot_id=excluded.origin_bot_id",
                        (target, str(granted_by)[:60], now, code, group, days, *source))
                return self.get_grant(target) or {}
            except sqlite3.IntegrityError:
                continue
        raise ConfigError("生成赠送资格失败，请重试")

    def get_grant(self, tg_user_id: str) -> dict[str, Any] | None:
        return self._db.one(
            "SELECT * FROM admin_grants WHERE tg_user_id=?",
            (str(tg_user_id or "").strip(),))

    def list_grants(self, limit: int = 500) -> list[dict[str, Any]]:
        return self._db.query(
            "SELECT * FROM admin_grants ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit or 500), 2000)),))

    def revoke_grant(self, tg_user_id: str) -> bool:
        tg_user_id = str(tg_user_id or "").strip()
        if not self.get_grant(tg_user_id):
            raise KeyError(tg_user_id)
        self._db.execute(
            "DELETE FROM admin_grants WHERE tg_user_id=?", (tg_user_id,))
        return True

    # -- the decision ---------------------------------------------------------

    def resolve(self, tg_user_id: str, credential: str | None = None
                ) -> Admission:
        """Decide whether this Telegram id may register, and on what terms.

        Order is admin -> invite -> redeem, and it matters: someone the
        operator pre-authorised should not be asked for a code they were never
        given, and a member holding both an invite and a card spends the
        cheaper one.

        Nothing here writes. See consume().
        """
        tg_user_id = str(tg_user_id or "").strip()
        cred = normalise(credential)

        # Explicit gift links must be checked before any other qualification:
        # an authorised user opening somebody else's link must not spend it.
        if cred.startswith("GIFT") and len(cred) == 16:
            gift = self._db.one("SELECT * FROM admin_grants WHERE gift_code=?", (cred,))
            if not self.channel_enabled("admin_grant"):
                return Admission(reason="管理员授权注册通道已关闭", tg_user_id=tg_user_id)
            if not gift or gift.get("used_at"):
                return Admission(reason="赠送资格已使用或已失效", tg_user_id=tg_user_id)
            if str(gift["tg_user_id"]) != tg_user_id:
                return Admission(reason="这份赠送资格不属于你的 Telegram 账号", tg_user_id=tg_user_id)

        if self.channel_enabled("admin_grant"):
            grant = self.get_grant(tg_user_id)
            if grant and not grant.get("used_at"):
                gifted = bool(grant.get("gift_code"))
                group = str(grant.get("gift_group_id") or "") if gifted else self._default_group()
                if gifted and self._groups is not None and not self._groups.get(group):
                    return Admission(reason="赠送的用户组已不存在，请联系管理员", tg_user_id=tg_user_id)
                return Admission(
                    allowed=True, via="admin", tg_user_id=tg_user_id,
                    group_id=group,
                    days=int(grant.get("gift_days") or 0) if gifted else self._default_days(),
                    credential=str(grant.get("gift_code") or ""),
                    reason="已获得管理员赠送的注册资格" if gifted else "管理员已预先授权")

        if not cred:
            return Admission(
                allowed=False, tg_user_id=tg_user_id,
                reason=self._no_credential_reason())

        now = int(time.time())

        invite = self.get_invite(cred) if self.channel_enabled("invite") else None
        if invite:
            problem = self._invite_usable(invite, now)
            if problem:
                return Admission(allowed=False, tg_user_id=tg_user_id,
                                 reason=problem)
            return Admission(
                allowed=True, via="invite", tg_user_id=tg_user_id,
                group_id=self._default_group(), days=self._default_days(),
                inviter_id=str(invite.get("owner_user_id") or ""),
                credential=invite["code"], reason="邀请码有效")

        card = self.get_redeem(cred) if self.channel_enabled("redeem") else None
        if card:
            state = str(card.get("status") or "")
            if state == "used":
                return Admission(allowed=False, tg_user_id=tg_user_id,
                                 reason="这张卡密已经被使用过了。")
            if state == "revoked":
                return Admission(allowed=False, tg_user_id=tg_user_id,
                                 reason="这张卡密已被作废。")
            return Admission(
                allowed=True, via="redeem", tg_user_id=tg_user_id,
                group_id=str(card.get("group_id") or "") or self._default_group(),
                days=int(card.get("days") or 0),
                credential=card["code"], reason="卡密有效")

        return Admission(allowed=False, tg_user_id=tg_user_id,
                         reason="邀请码或卡密无效，请检查后重新发送。")

    def _no_credential_reason(self) -> str:
        """Say which of the doors is actually open, rather than a generic no."""
        open_doors = []
        if self.channel_enabled("invite"):
            open_doors.append("邀请码")
        if self.channel_enabled("redeem"):
            open_doors.append("卡密")
        if not open_doors:
            return "当前暂停注册，请联系管理员。"
        return f"请发送{'或'.join(open_doors)}。"

    def consume(self, admission: Admission, new_user_id: str, *, conn: Any = None) -> bool:
        """Spend the credential. Called only after the account really exists.

        Returns whether anything was spent, so a caller can tell a no-op from a
        successful debit rather than assuming.
        """
        if not admission or not admission.allowed:
            return False
        def execute(sql: str, params: tuple) -> int:
            return conn.execute(sql, params).rowcount if conn is not None else self._db.execute(sql, params)
        now = int(time.time())
        user_id = str(new_user_id or "")

        if admission.via == "admin":
            sql = "UPDATE admin_grants SET used_at=? WHERE tg_user_id=? AND used_at IS NULL"
            params: tuple = (now, admission.tg_user_id)
            if admission.credential:
                sql += " AND gift_code=?"
                params += (admission.credential,)
            changed = execute(sql, params)
            return bool(changed)

        if admission.via == "invite":
            # Guarded in SQL, not by a read-then-write: two chats redeeming the
            # last use of the same code at once must not both succeed.
            changed = execute(
                "UPDATE invite_codes SET uses_left=uses_left-1 WHERE code=?"
                " AND uses_left > 0 AND revoked=0 AND (expires_at IS NULL OR expires_at>?)",
                (admission.credential, now))
            return bool(changed)

        if admission.via == "redeem":
            changed = execute(
                "UPDATE redeem_codes SET status='used',used_by=?,used_at=?"
                " WHERE code=? AND status='unused'",
                (user_id, now, admission.credential))
            return bool(changed)

        return False

    # -- reporting ------------------------------------------------------------

    def export_redeem_csv(self, batch: str | None = None,
                          status: str | None = None) -> str:
        """Plain CSV. The full code is present: this is the operator's own
        download, and a masked export would be useless for handing cards out."""
        rows = self.list_redeem(status=status, batch=batch, limit=5000)
        lines = ["code,group_id,group_name,days,status,batch,used_by,used_at,created_at"]
        for row in rows:
            used_at = row.get("used_at") or ""
            when = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(int(used_at))) if used_at else ""
            made = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(int(row.get("created_at") or 0)))
            cells = [
                row.get("code", ""), row.get("group_id", ""),
                row.get("group_name", ""), str(row.get("days") or 0),
                row.get("status", ""), row.get("batch", ""),
                row.get("used_by", ""), when, made,
            ]
            lines.append(",".join(_csv_cell(c) for c in cells))
        return "\n".join(lines) + "\n"


def _as_int(raw: Any, label: str) -> int:
    """Strict: None and '' are not zero, and zero is not a missing value."""
    if raw is None or raw == "":
        raise ConfigError(f"{label}不能为空")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{label}必须是整数") from None


def _csv_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    if any(ch in text for ch in ',"\n\r'):
        escaped = text.replace('"', '""')
        return f'"{escaped}"'
    return text
