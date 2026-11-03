# -*- coding: utf-8 -*-
"""登录：用 SMC（云听）系统的用户，逻辑跟它保持一致，同一个账号两边都能登录。

两张表都在源表库（src_database，没配就是 database），和 SMC 共用：
· src_opinion_sys_user          用户：password_hash = pbkdf2_sha256$迭代次数$盐$十六进制哈希；
                                 enabled=0 不能登录；连续输错 MAX_FAILED 次锁 LOCK_MINUTES 分钟；
                                 登录成功清零输错次数，记 last_login_at / last_login_ip
· src_opinion_sys_user_session  登录会话：库里只存令牌的 SHA-256；SESSION_HOURS 小时不用就过期，每次访问往后顺延。
                                 存在库里（不是进程内存），所以服务重启不用重新登录；用户在 SMC 里被停用 / 删除，
                                 这边的登录也跟着失效。

角色（跟 SMC 一样）：
    admin     管理员：全部功能
    operator  操作员：日常操作（任务、执行、修复、补推 …），不能改系统设置（推送设置、定时总开关）
    viewer    只读：只能看

本服务不增删用户、不改密码 —— 用户在 SMC 的「用户管理」里维护。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)

TABLE_USER = "src_opinion_sys_user"
TABLE_SESSION = "src_opinion_sys_user_session"
ALGO = "pbkdf2_sha256"
ITERATIONS = 240_000
MAX_FAILED = 5                   # 连续输错几次锁定（SMC auth.max_failed_logins）
LOCK_MINUTES = 10                # 锁多久（SMC auth.lock_minutes）
SESSION_HOURS = 72               # 多久不用要重新登录（SMC auth.session_hours）
CACHE_SECONDS = 10               # 认人结果在进程里缓存几秒，页面轮询不用每次查库
TOUCH_SECONDS = 60               # 距上次顺延超过这么久才写库顺延一次
COOKIE = "omx_session"           # 跟 SMC 的 smc_session 分开：两个系统在同一台机器上也互不干扰
ROLES = {"admin": "管理员", "operator": "操作员", "viewer": "只读"}

#: 用户名不存在时也算一遍哈希，别让「用户名对不对」能从响应快慢里看出来（同 SMC）
_DUMMY = f"{ALGO}${ITERATIONS}$00$00"

# SMC 的建表语句（schema.sql），会话表不存在时照它建（CREATE TABLE IF NOT EXISTS，已有就不动）
SESSION_DDL = """CREATE TABLE IF NOT EXISTS {table} (
  `token_hash`    CHAR(64)     NOT NULL                COMMENT '会话令牌的 SHA-256，库里不存明文',
  `user_id`       BIGINT       NOT NULL,
  `created_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_seen_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `expires_at`    DATETIME     NOT NULL                COMMENT '不用就过期；每次访问往后顺延',
  `ip`            VARCHAR(64)  NOT NULL DEFAULT '',
  `user_agent`    VARCHAR(300) NOT NULL DEFAULT '',
  PRIMARY KEY (`token_hash`),
  KEY `idx_session_user` (`user_id`),
  KEY `idx_session_expires` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='登录会话'"""


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- 密码哈希（同 SMC passwords.py）
def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    """本服务不改密码，测试 / 运维临时造账号用。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), int(iterations))
    return f"{ALGO}${int(iterations)}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, expected = (stored or _DUMMY).split("$", 3)
        if algo != ALGO:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt.encode(),
                                     int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 角色（同 SMC roles.py 的思路）
_READ = ("GET", "HEAD", "OPTIONS")
_SELF_SERVICE = ("/api/logout",)                  # 只读用户也能做的写操作：管自己的登录态
_ADMIN_ONLY = (("PUT", "/api/push/settings", "推送设置只有管理员能改"),
               ("PUT", "/api/scheduler", "定时总开关只有管理员能改"),
               ("PUT", "/api/notify/settings", "通知设置只有管理员能改"))


def denied_reason(role: str, method: str, path: str) -> str:
    """不允许返回原因，允许返回空串。按「方法 + 路径」统一判断，不在每个接口上各写一遍。"""
    if path.startswith(_SELF_SERVICE) or role == "admin":
        return ""
    if role == "viewer":
        return "" if method in _READ else "只读用户不能修改，请找管理员开操作权限"
    if role != "operator":
        return f"未知角色 {role}，请找管理员"
    for meth, prefix, why in _ADMIN_ONLY:
        if method == meth and path.startswith(prefix):
            return why
    return ""


# ---------------------------------------------------------------- 用户 / 会话表（同 SMC repo.py，同步版）
class UserRepository:
    _PUBLIC = "id, username, display_name, role, enabled, failed_count, locked_until, last_login_at"

    def __init__(self, db_factory: Callable[[], Any], src_db: str):
        self._db_factory = db_factory
        q = (lambda t: f"`{src_db}`.`{t}`") if src_db else (lambda t: f"`{t}`")
        self.table, self.session_table = q(TABLE_USER), q(TABLE_SESSION)
        self._session_ready = False

    def _run(self, fn):
        db = self._db_factory()
        try:
            return fn(db)
        except AuthError:
            raise
        except Exception as e:                       # noqa: BLE001
            msg = str(e)
            if "1146" in msg or "doesn't exist" in msg:
                raise AuthError(500, f"用户表 {self.table} 不存在：请确认 SMC 的用户表在源表库里") from e
            raise AuthError(500, f"连不上用户库，暂时无法登录：{msg[:200]}") from e
        finally:
            try:
                db.close()
            except Exception:                        # noqa: BLE001
                pass

    @staticmethod
    def _one(db, sql: str, params) -> Optional[Dict[str, Any]]:
        df = db.query_df(sql, list(params))
        return None if df.empty else df.to_dict("records")[0]

    def get_with_hash(self, username: str) -> Optional[Dict[str, Any]]:
        return self._run(lambda db: self._one(
            db, f"SELECT {self._PUBLIC}, password_hash, locked_until > NOW() AS is_locked "
                f"FROM {self.table} WHERE username = %s", [username]))

    def record_failure(self, user_id: int, max_failed: int, lock_minutes: int) -> bool:
        """记一次输错。返回这次是不是把账号锁上了（同 SMC：用库的时间判断）。"""
        def go(db):
            db.execute(f"UPDATE {self.table} SET failed_count = failed_count + 1, "
                       f"locked_until = CASE WHEN failed_count >= %s THEN NOW() + INTERVAL %s MINUTE "
                       f"ELSE locked_until END WHERE id = %s",
                       [max(1, max_failed), max(1, lock_minutes), user_id])
            row = self._one(db, f"SELECT locked_until > NOW() AS locked FROM {self.table} WHERE id = %s",
                            [user_id])
            return bool(row and row["locked"])
        return self._run(go)

    def record_success(self, user_id: int, ip: str) -> None:
        self._run(lambda db: db.execute(
            f"UPDATE {self.table} SET failed_count = 0, locked_until = NULL, "
            f"last_login_at = NOW(), last_login_ip = %s WHERE id = %s", [str(ip or "")[:64], user_id]))

    def _ensure_session_table(self, db) -> None:
        if not self._session_ready:
            db.execute(SESSION_DDL.format(table=self.session_table))
            self._session_ready = True

    def create_session(self, user_id: int, hours: int, ip: str, user_agent: str) -> str:
        token = secrets.token_urlsafe(32)

        def go(db):
            self._ensure_session_table(db)
            db.execute(f"INSERT INTO {self.session_table} (token_hash, user_id, expires_at, ip, user_agent) "
                       f"VALUES (%s, %s, NOW() + INTERVAL %s HOUR, %s, %s)",
                       [token_hash(token), user_id, max(1, hours), str(ip or "")[:64], str(user_agent or "")[:300]])
            db.execute(f"DELETE FROM {self.session_table} WHERE expires_at < NOW()")   # 顺手清掉过期的
        self._run(go)
        return token

    def session_user(self, token: str) -> Optional[Dict[str, Any]]:
        """令牌对应的用户（会话没过期、用户没停用）。"""
        return self._run(lambda db: self._one(
            db, f"SELECT u.id, u.username, u.display_name, u.role, s.created_at AS login_at "
                f"FROM {self.session_table} s JOIN {self.table} u ON u.id = s.user_id "
                f"WHERE s.token_hash = %s AND s.expires_at > NOW() AND u.enabled = 1", [token_hash(token)]))

    def touch_session(self, token: str, hours: int) -> None:
        self._run(lambda db: db.execute(
            f"UPDATE {self.session_table} SET last_seen_at = NOW(), expires_at = NOW() + INTERVAL %s HOUR "
            f"WHERE token_hash = %s", [max(1, hours), token_hash(token)]))

    def delete_session(self, token: str) -> None:
        self._run(lambda db: db.execute(f"DELETE FROM {self.session_table} WHERE token_hash = %s",
                                        [token_hash(token)]))


# ---------------------------------------------------------------- 登录 / 认人（同 SMC service.py）
def public_user(row: Dict[str, Any]) -> Dict[str, Any]:
    role = str(row.get("role") or "")
    name = str(row.get("display_name") or "").strip() or str(row.get("username"))
    at = row.get("login_at")                       # 这次登录的时间（会话创建时间）
    return {"id": int(row["id"]), "username": str(row["username"]), "display_name": name,
            "role": role, "role_text": ROLES.get(role, role), "readonly": role == "viewer",
            "login_at": "" if at is None or at != at else str(at)[:19]}


class Auth:
    def __init__(self, users: UserRepository):
        self.users = users
        self._cache: Dict[str, Tuple[Dict[str, Any], float]] = {}
        self._touched: Dict[str, float] = {}

    def login(self, username: str, password: str, ip: str = "", user_agent: str = "") -> Tuple[str, dict]:
        username = str(username or "").strip()
        if not username or not password:
            raise AuthError(400, "请输入用户名和密码")
        row = self.users.get_with_hash(username)
        # 用户名不存在也照样算一遍哈希：别让响应快慢泄露用户名对不对
        good = verify_password(password, row["password_hash"] if row else "")
        if not row:
            raise AuthError(401, "用户名或密码不对")
        if not int(row.get("enabled") or 0):
            raise AuthError(403, "这个用户已被停用，请联系管理员")
        if int(row.get("is_locked") or 0):
            until = str(row.get("locked_until") or "")[11:16]
            raise AuthError(423, f"密码输错次数太多，账号已锁定，{until} 后再试")
        if not good:
            locked = self.users.record_failure(row["id"], MAX_FAILED, LOCK_MINUTES)
            log.warning("登录失败：用户 %s，来自 %s%s", username, ip, "，已锁定" if locked else "")
            if locked:
                raise AuthError(423, f"密码输错次数太多，账号锁定 {LOCK_MINUTES} 分钟")
            raise AuthError(401, "用户名或密码不对")
        user = public_user(dict(row, login_at=time.strftime("%Y-%m-%d %H:%M:%S")))
        self.users.record_success(row["id"], ip)
        token = self.users.create_session(row["id"], SESSION_HOURS, ip, user_agent)
        log.info("登录：用户 %s，来自 %s", username, ip)
        return token, user

    def logout(self, token: str) -> None:
        if token:
            self._cache.pop(token, None)
            try:
                self.users.delete_session(token)
            except AuthError:
                pass

    def user_for(self, token: str) -> Optional[dict]:
        """每个请求认人：会话没过期、用户没停用才算。结果缓存 CACHE_SECONDS 秒。"""
        if not token:
            return None
        now = time.monotonic()
        hit = self._cache.get(token)
        if hit and now - hit[1] < CACHE_SECONDS:
            return hit[0]
        try:
            row = self.users.session_user(token)
        except AuthError:
            return hit[0] if hit else None              # 库临时连不上：已登录的人先不踢
        if not row:
            self._cache.pop(token, None)
            return None
        user = public_user(row)
        self._cache[token] = (user, now)
        if now - self._touched.get(token, 0) > TOUCH_SECONDS:
            self._touched[token] = now
            try:
                self.users.touch_session(token, SESSION_HOURS)
            except AuthError:
                pass
        if len(self._cache) > 5000:                    # 防止长期运行时无限长
            self._cache.clear()
            self._touched.clear()
        return user
