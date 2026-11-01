"""Provision one independent domain inbox and one exact-recipient Apple rule."""
import secrets
import re
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests

from inbox_links import REGISTRY, is_ready, load_links, save_link, update_link
from public_inbox import publish_inbox_link, load_config
from icloud_hme import ICloudSessionExpired, ICloudMailSessionExpired, ICloudMailServiceUnavailable
from registration_policy import MAX_SOURCE_ADDRESSES

API_BASE = "https://mail-api.fastaiapi.cloud"
WEBMAIL_BASE = "https://webmail.fastaiapi.cloud/"
MAIL_DOMAIN = "fastaiapi.cloud"
MAX_MAIL_RULES = MAX_SOURCE_ADDRESSES


class MailProvisioner:
    def __init__(self, client, registry=REGISTRY, http=None, publisher=None):
        self.client = client
        self.registry = registry
        self.http = http or requests.Session()
        self.rules_url = None
        self.publisher = publisher or (lambda record: publish_inbox_link(record, registry=self.registry))
        self.needs_public_config = publisher is None

    def prepare(self):
        if self.needs_public_config:
            load_config()
        self.prepare_rules()
        self.ensure_forward_target(allow_uninitialized=True)

    def prepare_rules(self, validation=None):
        """Authenticate rule access without changing forwarding settings."""
        if validation is None:
            validation = self.client.validate_session()
        mcc = validation.get("webservices", {}).get("mcc", {}).get("url", "")
        host = urlparse(mcc).hostname or ""
        if not host.endswith((".icloud.com", ".icloud.com.cn")):
            raise RuntimeError("iCloud 邮件规则服务不可用，请重新导入登录 Cookie")
        dsid = self.client.get_account_info().get("dsid")
        if not dsid:
            raise RuntimeError("无法识别 iCloud 账号")
        self.rules_url = f"{mcc.rstrip('/')}/cc/mail/v1/account/{quote(str(dsid), safe='')}/rules/"
        self.list_rules()  # Check access before creating any paid-account alias.

    def ensure_forward_target(self, allow_uninitialized=False):
        listing = self.client._request("GET", f"{self.client._service_url}/v2/hme/list", max_attempts=1)
        if not listing.get("success"):
            raise RuntimeError("无法读取隐藏地址的转发设置")
        result = listing.get("result", {})
        current = (result.get("selectedForwardTo") or "").lower()
        candidates = [x for x in result.get("forwardToEmails", [])
                      if x.lower().endswith(("@icloud.com", "@me.com", "@mac.com"))]
        if current not in [x.lower() for x in candidates]:
            if len(candidates) != 1:
                raise RuntimeError("请先在 iCloud+ 将隐藏邮件地址转发至本账号的 iCloud 邮箱")
            # Apple initializes the HME user record on the first reservation.
            # Before that, updateForwardTo returns -26115 even with a valid mailbox.
            if allow_uninitialized and not current and not result.get("hmeEmails"):
                return
            updated = self.client._request("POST", f"{self.client._service_url}/v1/hme/updateForwardTo",
                                           json_data={"forwardToEmail": candidates[0]}, max_attempts=1)
            if not updated.get("success"):
                raise RuntimeError("隐藏地址转发目标设置失败，已保留邮箱供补全配置")
            confirmed = self.client._request("GET", f"{self.client._service_url}/v2/hme/list", max_attempts=1)
            if confirmed.get("result", {}).get("selectedForwardTo", "").lower() != candidates[0].lower():
                raise RuntimeError("隐藏地址转发目标未确认，已停止创建")

    def list_rules(self):
        try:
            data = self.client._request("GET", self.rules_url, max_attempts=1)
        except ICloudSessionExpired as error:
            status = re.search(r'HTTP (401|403|421|450)\b', str(error))
            if error.error_code == 'INTERNAL_SERVER_ERROR':
                raise ICloudMailServiceUnavailable(error.http_status or (status.group(1) if status else '401')) from None
            raise ICloudMailSessionExpired(status.group(1) if status else '401') from None
        if data.get("requestStatus", "SUCCESS") != "SUCCESS" or not isinstance(data.get("mailRules"), list):
            raise RuntimeError("读取 iCloud 转发规则失败")
        return data["mailRules"]

    def check_rule_capacity(self, alias=None, destination=None):
        rules = self.list_rules()
        # Existing forwarding can still be repaired/published at the limit.
        if alias and any(self.matching_rule(r, alias, destination or "") for r in rules):
            return
        if len(rules) >= MAX_MAIL_RULES:
            raise RuntimeError(f"iCloud 转发规则已达 {MAX_MAIL_RULES} 条项目上限；请先释放规则容量，再一键补齐收件地址；未删除已有规则")

    def _domain_request(self, method, path, *, token=None, data=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = self.http.request(method, API_BASE + path, json=data, headers=headers, timeout=25)
        except requests.RequestException:
            raise RuntimeError("域名邮箱服务连接失败，已保存当前进度，可重试") from None
        if not response.ok:
            raise RuntimeError(f"域名邮箱服务返回 HTTP {response.status_code}，请检查服务设置或人机验证要求")
        try:
            return response.json()
        except ValueError:
            raise RuntimeError("域名邮箱服务返回了无效响应") from None

    @staticmethod
    def matching_rule(rule, alias, destination):
        return (rule.get("prefix") == "TO" and rule.get("body", "").lower() == alias.lower()
                and rule.get("action") == "FRWD" and rule.get("target", "").lower() == destination.lower())

    def ensure_rule(self, alias, destination):
        rules = self.list_rules()
        matching = next((r for r in rules if self.matching_rule(r, alias, destination)), None)
        if matching:
            return matching["ruleId"]
        # Do not silently override a user's existing rule for the same recipient.
        if any(r.get("prefix") in ("TO", "RECP") and r.get("body", "").lower() == alias.lower() for r in rules):
            raise RuntimeError("该别名已有不同的收件规则，请先检查冲突；未覆盖原规则")
        self.check_rule_capacity(alias, destination)
        rule = {"ruleName": f"HME {alias}"[:30], "prefix": "TO", "body": alias,
                "action": "FRWD", "target": destination,
                "ruleSequence": max((int(r.get("ruleSequence", 0)) for r in rules), default=0) + 10000}
        try:
            response = self.client._request("POST", self.rules_url.rstrip('/') + "/batch",
                                 json_data={"data": [{"operation": "CREATE", "data": rule}]}, max_attempts=1)
            operations = response.get("data", [])
            if any(item.get("data", {}).get("errorCode") == "RULE_MAXIMUM_REACHED" for item in operations):
                raise RuntimeError("iCloud 转发规则容量已满（Apple 返回 RULE_MAXIMUM_REACHED）；请释放容量后补齐")
            if response.get("requestStatus") == "FAILURE" or any(item.get("status") == "FAILURE" for item in operations):
                raise RuntimeError("iCloud 拒绝创建转发规则，已保存进度供补齐")
        except Exception as error:
            # A timed-out POST may have succeeded; read back before retrying it.
            confirmed = next((r for r in self.list_rules() if self.matching_rule(r, alias, destination)), None)
            if confirmed:
                return confirmed["ruleId"]
            if isinstance(error, RuntimeError) and str(error).startswith("iCloud "):
                raise
            status = re.search(r"HTTP \d{3}", str(error))
            detail = f"（{status.group(0)}）" if status else ""
            raise RuntimeError(f"转发规则创建失败{detail}，已保存邮箱；可补全配置而无需重新创建别名") from None
        confirmed = next((r for r in self.list_rules() if self.matching_rule(r, alias, destination)), None)
        if not confirmed:
            raise RuntimeError("未能确认转发规则已生效，已保留进度供重试")
        return confirmed["ruleId"]

    def provision(self, alias, account_id):
        alias = alias.lower()
        record = load_links(self.registry).get(alias, {})
        if record.get("account_id") not in (None, account_id):
            raise RuntimeError("收件记录所属账号不匹配")
        update_link(alias, {"account_id": account_id}, self.registry)
        if is_ready(record):
            return self.publisher(record)
        try:
            self.ensure_forward_target()
            self.check_rule_capacity(alias, record.get("destination"))
            if not record.get("inbox_url"):
                update_link(alias, {"stage": "creating_inbox"}, self.registry)
                created = self._domain_request("POST", "/api/new_address", data={
                    "name": "hme" + secrets.token_hex(8), "domain": MAIL_DOMAIN,
                    "enableRandomSubdomain": False,
                })
                destination, token = created.get("address", ""), created.get("jwt", "")
                if not destination.lower().endswith("@" + MAIL_DOMAIN) or not token:
                    raise RuntimeError("域名服务未返回有效的独立邮箱和凭据")
                url = WEBMAIL_BASE + "?" + urlencode({"jwt": token})
                record = save_link(alias, destination, url, path=self.registry)
                update_link(alias, {"stage": "inbox_created"}, self.registry)
            token = parse_qs(urlparse(record.get("forward_inbox_url") or record["inbox_url"]).query)["jwt"][0]
            identity = self._domain_request("GET", "/api/settings", token=token)
            if identity.get("address", "").lower() != record["destination"].lower():
                raise RuntimeError("独立收件箱凭据校验失败，未配置转发")
            rule_id = self.ensure_rule(alias, record["destination"])
            self.publisher(load_links(self.registry)[alias])
            return update_link(alias, {"configured": True, "stage": "ready", "rule_id": rule_id,
                                       "last_error": None}, self.registry)
        except Exception as error:
            update_link(alias, {"configured": False, "stage": "pending", "last_error": str(error)[:250]}, self.registry)
            raise
