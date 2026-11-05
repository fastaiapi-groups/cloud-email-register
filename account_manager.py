#!/usr/bin/env python3
"""
iCloud HME — 多账号管理器
===========================
管理多组 iCloud 账号及其隐私邮箱别名。

功能:
  - 账号 CRUD (增删改查)
  - Cookie 导入解析 (Header String / JSON)
  - 批量会话校验
  - 别名按账号归属索引
  - 跨账号并发/轮询创建

用法:
    from account_manager import AccountManager

    mgr = AccountManager()
    mgr.add_account("主号", cookie_header_string)
    mgr.create_aliases_batch(["acc_xxx", "acc_yyy"], count_per_account=5)
"""

import json
import copy
import time
import uuid
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

HERE = Path(__file__).resolve().parent
ACCOUNTS_FILE = HERE / "accounts.json"
OLD_COOKIES_FILE = HERE / "cookies.json"
RESULTS_DIR = HERE / "results"
LATEST_EMAILS = RESULTS_DIR / "latest_emails.txt"

from mail_cache import get_cache  # noqa: E402
from storage import Storage, database_for


class AccountManager:
    """多账号管理器"""

    def __init__(self):
        self.accounts: Dict[str, Dict] = {}
        self._lock = threading.RLock()
        self._workflow_locks = {}
        self._cache = get_cache()
        self._load()

    def _load(self):
        self.storage = Storage(database_for(ACCOUNTS_FILE))
        self.storage.import_accounts(ACCOUNTS_FILE, RESULTS_DIR / "account_sessions", OLD_COOKIES_FILE)
        self.storage.import_emails(LATEST_EMAILS)
        self.accounts = self.storage.load_accounts()
        self._persisted_accounts = copy.deepcopy(self.accounts)

    def _save(self):
        with self._lock:
            self.storage.save_accounts(self.accounts, self._persisted_accounts)
            self._persisted_accounts = copy.deepcopy(self.accounts)

    def _generate_id(self) -> str:
        return "acc_" + uuid.uuid4().hex[:8]

    @staticmethod
    def parse_cookie_input(raw: str) -> Dict[str, str]:
        raw = raw.strip()
        if not raw:
            raise ValueError("空白输入 — 请粘贴 Cookie Header String 或 JSON")

        if raw.startswith("{"):
            try:
                cookies = json.loads(raw)
                if isinstance(cookies, dict):
                    return {k: str(v) for k, v in cookies.items() if v}
            except json.JSONDecodeError:
                pass

        cookies: Dict[str, str] = {}
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                name, value = part.split("=", 1)
                name = name.strip()
                value = value.strip()
                if name:
                    cookies[name] = value

        if not cookies:
            raise ValueError(
                "无法解析 Cookie 输入。\n"
                "请提供 Header String 格式 (name=value; ...) 或 JSON 格式"
            )

        return cookies

    def add_account(
        self, name: str, cookie_input: str, host: str = "icloud.com"
    ) -> Dict:
        from icloud_hme import ICloudHME

        cookies = self.parse_cookie_input(cookie_input)
        acc_id = self._generate_id()

        account: Dict[str, Any] = {
            "id": acc_id,
            "name": name,
            "real_email": "",
            "icloud_email": "",
            "cookies": cookies,
            "host": host,
            "status": "active",
            "alias_total": 0,
            "alias_active": 0,
            "last_validated": None,
            "last_error": None,
            "created_at": datetime.now().isoformat(),
        }

        try:
            client = ICloudHME(cookies, host=host, verbose=False)
            client.validate_session()
            info = client.get_account_info()
            if info:
                account["real_email"] = (
                    info.get("appleId", "")
                    or info.get("primaryEmail", "")
                )
                account["icloud_email"] = self._derive_icloud_email(info)

            try:
                aliases = client.list_aliases()
                account["alias_total"] = len(aliases)
                account["alias_active"] = sum(
                    1 for a in aliases if a.get("active")
                )
            except Exception:
                pass

            account["last_validated"] = datetime.now().isoformat()
            account["last_error"] = None
        except Exception as e:
            account["status"] = "error"
            account["last_error"] = str(e)[:300]

        self.accounts[acc_id] = account
        self._save()
        return account

    def remove_account(self, acc_id: str) -> bool:
        if acc_id in self.accounts:
            del self.accounts[acc_id]
            self._save()
            return True
        return False

    def get_account(self, acc_id: str) -> Optional[Dict]:
        return self.accounts.get(acc_id)

    def list_accounts(self) -> List[Dict]:
        return sorted(
            self.accounts.values(),
            key=lambda a: (a.get("status") != "active", a.get("created_at", "")),
        )

    def update_account(self, acc_id: str, **kwargs) -> Optional[Dict]:
        with self._lock:
            if acc_id in self.accounts:
                self.accounts[acc_id].update(kwargs)
                if "cookies" in kwargs:
                    self.accounts[acc_id].pop("_session_unreadable", None)
                self._save()
                return dict(self.accounts[acc_id])
            return None

    @staticmethod
    def _derive_icloud_email(info: Dict) -> str:
        primary = str(info.get("primaryEmail", "") or "").strip()
        apple_id = str(info.get("appleId", "") or "").strip()

        if primary and ("@icloud.com" in primary or "@me.com" in primary or "@mac.com" in primary):
            return primary

        if apple_id and ("@icloud.com" in apple_id or "@me.com" in apple_id or "@mac.com" in apple_id):
            return apple_id

        if apple_id and "@" in apple_id:
            local = apple_id.split("@")[0]
            return f"{local}@icloud.com"

        return primary or apple_id

    def validate_account(self, acc_id: str) -> Dict:
        with self._workflow_lock(acc_id):
            return self._validate_account_locked(acc_id)

    def _validate_account_locked(self, acc_id: str) -> Dict:
        from icloud_hme import ICloudHME

        account = self.accounts.get(acc_id)
        if not account:
            raise KeyError(f"账号不存在: {acc_id}")

        try:
            client = self.get_client(acc_id, verbose=False)
            client.validate_session()
            info = client.get_account_info()
            if info:
                if account.get("real_email") and info.get("appleId", "").lower() != account["real_email"].lower():
                    raise RuntimeError("账号会话身份不匹配，拒绝覆盖原账号")
                account["real_email"] = (
                    info.get("appleId", "")
                    or info.get("primaryEmail", "")
                )
                existing = account.get("icloud_email", "")
                is_icloud = existing and any(
                    d in existing for d in ("@icloud.com", "@me.com", "@mac.com")
                )
                if not is_icloud:
                    account["icloud_email"] = self._derive_icloud_email(info)

            aliases = client.list_aliases()
            account["alias_total"] = len(aliases)
            account["alias_active"] = sum(
                1 for a in aliases if a.get("active")
            )
            account["status"] = "active"
            account["last_validated"] = datetime.now().isoformat()
            account["last_error"] = None
            account["session_requires_login"] = False
        except Exception as e:
            from icloud_hme import ICloudSessionExpired
            account["status"] = "error"
            account["last_error"] = str(e)[:300]
            account["session_requires_login"] = isinstance(e, ICloudSessionExpired)

        self._save()
        return account

    def keep_session_alive(self, acc_id: str) -> Dict:
        """只校验会话并保存轮换 Cookie，不创建邮箱或启动业务任务。"""
        from icloud_hme import ICloudSessionExpired
        with self._workflow_lock(acc_id):
            client = self.get_client(acc_id, verbose=False)
            try:
                client.validate_session(max_attempts=1)
                info = client.get_account_info() or {}
                account = self.accounts[acc_id]
                if not info.get('appleId') or info['appleId'].lower() != account.get('real_email', '').lower():
                    raise ICloudSessionExpired('Apple 会话身份不匹配，请更新该账号 Cookie')
                values = dict(status='active', last_error=None, session_requires_login=False,
                              session_checked_at=datetime.now().isoformat(),
                              last_validated=datetime.now().isoformat())
            except Exception as error:
                # 不把远端响应正文或凭据写入账号接口与日志。
                expired = isinstance(error, ICloudSessionExpired)
                values = dict(status='error', session_requires_login=expired,
                              session_checked_at=datetime.now().isoformat(),
                              last_error='Apple 会话失效，请重新登录 iCloud 并更新 Cookie' if expired
                              else '会话保活暂时失败，将自动重试')
            with self._lock:
                account = self.accounts.get(acc_id)
                # 手工替换期间发出的旧请求不能回写新会话的状态。
                if account and account.get('cookies') == client.cookies:
                    account.update(values)
                    self._save()
                return dict(account or {})

    def replace_session(self, acc_id: str, raw: str) -> Dict:
        """校验新 Cookie 身份后原位替换，保留账号 ID、邮箱与任务配置。"""
        from icloud_hme import ICloudHME
        with self._workflow_lock(acc_id):
            account = self.accounts.get(acc_id)
            if not account:
                raise KeyError('账号不存在')
            client = ICloudHME(self.parse_cookie_input(raw), host=account.get('host', 'icloud.com'), verbose=False)
            client.validate_session(max_attempts=1)
            info = client.get_account_info() or {}
            if not info.get('appleId') or info['appleId'].lower() != account.get('real_email', '').lower():
                raise ValueError('Cookie 所属账号不匹配，未修改原账号')
            return self.update_account(acc_id, cookies=client.cookies, status='active', last_error=None,
                                       session_requires_login=False,
                                       last_validated=datetime.now().isoformat())

    def validate_all(self) -> List[Dict]:
        results: List[Dict] = []
        for acc_id in list(self.accounts.keys()):
            try:
                account = self.validate_account(acc_id)
                results.append({
                    "id": acc_id,
                    "ok": account.get("status") == "active",
                    "email": account.get("real_email", ""),
                    "alias_total": account.get("alias_total", 0),
                })
            except Exception as e:
                results.append({
                    "id": acc_id,
                    "ok": False,
                    "error": str(e)[:200],
                })
        return results

    def get_client(self, acc_id: str, verbose: bool = False):
        from icloud_hme import ICloudHME

        account = self.accounts.get(acc_id)
        if not account:
            raise KeyError(f"账号不存在: {acc_id}")
        baseline = dict(account["cookies"])

        def save_updates(updates):
            with self._lock:
                current = self.accounts.get(acc_id)
                # A client opened before a session replacement must never overwrite it.
                if not current or current.get("cookies") != baseline:
                    return
                current["cookies"].update(updates)
                baseline.update(updates)
                self._save()

        return ICloudHME(
            dict(baseline),
            host=account.get("host", "icloud.com"),
            verbose=verbose,
            on_cookie_update=save_updates,
        )

    def set_app_password(self, acc_id: str, app_password: str):
        with self._lock:
            if acc_id not in self.accounts:
                raise KeyError(f"账号不存在: {acc_id}")
            self.accounts[acc_id]["app_password"] = app_password
            self._save()

    def get_mail_client(self, acc_id: str, verbose: bool = False):
        from icloud_mail import ICloudMail

        account = self.accounts.get(acc_id)
        if not account:
            raise KeyError(f"账号不存在: {acc_id}")
        app_pwd = account.get("app_password", "")
        imap_email = account.get("icloud_email", "")
        if not imap_email:
            real = account.get("real_email", "")
            if real and any(d in real for d in ("@icloud.com", "@me.com", "@mac.com")):
                imap_email = real
            else:
                raise ValueError(
                    "未设置 iCloud 邮箱。\n"
                    "Apple ID ({}) 不是 iCloud 地址，\n"
                    "请点击下方按钮输入你的 @icloud.com 邮箱".format(
                        account.get("real_email", "?")
                    )
                )
        if not app_pwd:
            raise ValueError(
                "未设置 App 专用密码。\n"
                "请点击下方按钮，输入 @icloud.com 邮箱和应用密码"
            )
        return ICloudMail(imap_email, app_pwd, verbose=verbose)

    def check_inbox(self, acc_id: str, limit: int = 50, days: int = 7,
                    force: bool = False) -> List[Dict]:
        cached = self._cache.get_inbox(acc_id)
        age = self._cache.cache_age_seconds(acc_id)

        if not force and cached and age < 300:
            return cached[-limit:]

        try:
            mail = self.get_mail_client(acc_id)
            new_msgs = mail.check_inbox(limit=50, days=days)
            mail.disconnect()
        except Exception:
            new_msgs = []

        if new_msgs:
            self._cache.set_inbox(acc_id, new_msgs)

        return self._cache.get_inbox(acc_id)[-limit:]

    def check_alias_mail(self, acc_id: str, alias_email: str,
                         limit: int = 20, days: int = 30,
                         force: bool = False) -> List[Dict]:
        cached = self._cache.get_alias_mail(acc_id, alias_email)
        age = self._cache.cache_age_seconds(acc_id)

        if not force and cached and age < 300:
            return cached[-limit:]

        try:
            mail = self.get_mail_client(acc_id)
            new_msgs = mail.find_by_recipient(alias_email, limit=20, days=days)
            mail.disconnect()
        except Exception:
            new_msgs = []

        if new_msgs:
            self._cache.set_alias_mail(acc_id, alias_email, new_msgs)

        return self._cache.get_alias_mail(acc_id, alias_email)[-limit:]

    def check_all_aliases_mail(self, acc_id: str, limit_per: int = 5,
                               days: int = 14,
                               force: bool = False) -> Dict[str, List[Dict]]:
        cached = self._cache.get_all_alias_mail(acc_id)
        age = self._cache.cache_age_seconds(acc_id)

        if not force and cached and age < 300:
            results = {}
            for alias, msgs in cached.items():
                results[alias] = msgs[-limit_per:]
            return results

        from icloud_hme import ICloudHME

        try:
            client = self.get_client(acc_id, verbose=False)
            aliases = client.list_aliases()
        except Exception:
            return cached if cached else {}

        alias_set = {a.get("email", "").lower() for a in aliases if a.get("email")}
        if not alias_set:
            return cached if cached else {}

        try:
            mail = self.get_mail_client(acc_id)
            all_inbox = mail.check_inbox(limit=100, days=days)
            mail.disconnect()
        except Exception:
            return cached if cached else {}

        results: Dict[str, List[Dict]] = {}
        for msg in all_inbox:
            to_field = msg.get("to", "").lower()
            for alias in alias_set:
                if alias and alias in to_field:
                    if alias not in results:
                        results[alias] = []
                    if len(results[alias]) < limit_per:
                        results[alias].append(msg)
                    break

        if results:
            self._cache.set_alias_mail_batch(acc_id, results)

        return results

    def test_imap_connection(self, acc_id: str) -> Dict:
        try:
            mail = self.get_mail_client(acc_id)
            result = mail.test_connection()
            mail.disconnect()
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def _workflow_lock(self, acc_id):
        with self._lock:
            return self._workflow_locks.setdefault(acc_id, threading.RLock())

    def setup_alias_inbox(self, acc_id: str, email: str) -> Dict:
        from mail_provisioning import MailProvisioner
        if "+" in email:
            from plus_addressing import setup_shared_inbox
            from inbox_links import load_links
            record = load_links().get(email.lower(), {})
            if record.get("kind") != "plus" or record.get("account_id") != acc_id:
                raise ValueError("分裂地址不存在或账号不匹配")
            with self._workflow_lock(acc_id):
                record = setup_shared_inbox(record["base_email"], email.lower())
                self.storage.add_email_once(email.lower(), acc_id)
                return {"email": email.lower(), "account_id": acc_id, "ok": True,
                        "inbox_url": record["inbox_url"], "line": email.lower() + "----" + record["inbox_url"]}
        with self._workflow_lock(acc_id):
            client = self.get_client(acc_id, verbose=False)
            provisioner = MailProvisioner(client)
            provisioner.prepare()
            aliases = client.list_aliases()
            if not any(a.get("email", "").lower() == email.lower() and a.get("active") for a in aliases):
                raise ValueError("该邮箱不是此账号的活跃隐藏地址")
            record = provisioner.provision(email, acc_id)
            return {"email": email, "account_id": acc_id, "ok": True,
                    "inbox_url": record["inbox_url"], "line": email + "----" + record["inbox_url"]}

    def create_aliases_for_account(
        self, acc_id: str, count: int = 1, label: str = ""
    ) -> List[Dict]:
        from icloud_hme import ICloudCreationLimitError
        from mail_provisioning import MailProvisioner
        if not isinstance(count, int) or not 1 <= count <= 50:
            raise ValueError("创建数量必须为 1 到 50")
        account = self.accounts.get(acc_id)
        if not account:
            raise KeyError(f"账号不存在: {acc_id}")
        with self._workflow_lock(acc_id):
            limit = self.storage.read_document('hme_creation_limit', acc_id) or {}
            if limit.get('retry_at', 0) > time.time():
                return [{'email': None, 'account_id': acc_id, 'ok': False,
                         'alias_created': False, 'creation_limited': True,
                         'retry_at': limit['retry_at'], 'error': limit['error']}]
            client = self.get_client(acc_id, verbose=False)
            provisioner = MailProvisioner(client)
            provisioner.prepare()
            results = []
            for i in range(count):
                email = None
                try:
                    # A paid alias must not be reserved when it cannot be routed.
                    provisioner.check_rule_capacity()
                    alias_label = label or f"{account.get('name', acc_id)} {datetime.now().strftime('%m%d%H%M')}-{i + 1}"
                    result = client.create_alias(label=alias_label, max_retries=1)
                    email = result.get("email", "")
                    if not email:
                        raise RuntimeError("iCloud 未返回有效邮箱")
                    # Persist the alias before any subsequent network operation.
                    with self._lock:
                        self.storage.add_email(email, acc_id)
                        account["alias_total"] = account.get("alias_total", 0) + 1
                        account["alias_active"] = account.get("alias_active", 0) + 1
                        self._save()
                    if limit:
                        self.storage.write_document('hme_creation_limit', {}, acc_id)
                        limit = {}
                    record = provisioner.provision(email, acc_id)
                    results.append({"email": email, "account_id": acc_id, "ok": True,
                                    "alias_created": True, "inbox_url": record["inbox_url"],
                                    "line": email + "----" + record["inbox_url"]})
                except Exception as error:
                    result = {"email": email, "account_id": acc_id, "ok": False,
                              "alias_created": bool(email), "error": str(error)[:250]}
                    if isinstance(error, ICloudCreationLimitError):
                        # Local one-hour backoff, not an Apple recovery deadline.
                        limit = {'error_code': '-41015', 'error': str(error),
                                 'retry_at': time.time() + 3600}
                        self.storage.write_document('hme_creation_limit', limit, acc_id)
                        result.update(creation_limited=True, retry_at=limit['retry_at'])
                    results.append(result)
                    # Stop on a partial setup failure so a batch cannot leave many orphan aliases.
                    break
            return results

    def create_aliases_batch(
        self,
        account_ids: List[str],
        count_per_account: int = 1,
        interval_sec: float = 3.0,
        label: str = "",
    ) -> Dict[str, List[Dict]]:
        all_results: Dict[str, List[Dict]] = {}
        for i, acc_id in enumerate(account_ids):
            if acc_id not in self.accounts:
                all_results[acc_id] = [{
                    "email": None, "account_id": acc_id,
                    "ok": False, "error": "账号不存在",
                }]
                continue
            if self.accounts[acc_id].get("status") != "active":
                all_results[acc_id] = [{
                    "email": None, "account_id": acc_id,
                    "ok": False, "error": "账号不可用",
                }]
                continue

            results = self.create_aliases_for_account(
                acc_id, count_per_account, label
            )
            all_results[acc_id] = results

            if i < len(account_ids) - 1 and interval_sec > 0:
                time.sleep(interval_sec)

        return all_results

    def get_aliases_for_account(self, acc_id: str) -> List[Dict]:
        try:
            client = self.get_client(acc_id, verbose=False)
            aliases = client.list_aliases()
            self.storage.backfill_creation_times(acc_id, aliases)
            return aliases
        except Exception:
            return []

    def get_all_aliases(self) -> List[Dict]:
        all_aliases: List[Dict] = []
        for acc_id, account in self.accounts.items():
            for alias in self.get_aliases_for_account(acc_id):
                alias["account_id"] = acc_id
                alias["account_name"] = account.get("name", "")
                alias["account_email"] = account.get("real_email", "")
                all_aliases.append(alias)
        return all_aliases

    def get_summary(self) -> Dict:
        total_aliases = sum(
            a.get("alias_total", 0) for a in self.accounts.values()
        )
        total_active = sum(
            a.get("alias_active", 0) for a in self.accounts.values()
        )
        active_accounts = sum(
            1 for a in self.accounts.values() if a.get("status") == "active"
        )
        error_accounts = sum(
            1 for a in self.accounts.values() if a.get("status") == "error"
        )
        return {
            "account_count": len(self.accounts),
            "active_accounts": active_accounts,
            "error_accounts": error_accounts,
            "total_aliases": total_aliases,
            "total_active_aliases": total_active,
        }


if __name__ == "__main__":
    print("AccountManager 自测")
    mgr = AccountManager()
    summary = mgr.get_summary()
    print(f"当前账号数: {summary['account_count']}")
    
    header = "X_APPLE_WEB_KB=abc123; SESSION_TOKEN=xyz789"
    parsed = mgr.parse_cookie_input(header)
    print(f"Header String → {len(parsed)} 个 cookie")
    
    json_in = '{"X_APPLE_WEB_KB":"abc123","SESSION_TOKEN":"xyz789"}'
    parsed2 = mgr.parse_cookie_input(json_in)
    print(f"JSON → {len(parsed2)} 个 cookie")
    
    print("自测完成 ✓")
