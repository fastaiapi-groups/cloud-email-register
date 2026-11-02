"""分裂地址配置与实收验证；发信成功不等同于收信成功。"""
import copy
import html
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import parse_qs, urlparse

import requests

from inbox_links import load_links, update_link, is_ready
from public_inbox import publish_inbox_link


def plus_address(base, tag):
    if not isinstance(base, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+@icloud\.com", base):
        raise ValueError("源地址必须是不含加号的 @icloud.com 地址")
    if not isinstance(tag, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,24}", tag):
        raise ValueError("后缀须为 1–24 位英文字母、数字、下划线或短横线")
    local, domain = base.lower().split("@")
    if local.startswith('.') or local.endswith('.') or '..' in local:
        raise ValueError("源地址的本地部分不能以点开头、结尾或包含连续的点")
    if len(local) + len(tag) + 1 > 64:
        raise ValueError("分裂地址的 @ 前部分不能超过 64 字符")
    return f"{local}+{tag.lower()}@{domain}"


def safe_error(error):
    from icloud_hme import ICloudSessionExpired
    if isinstance(error, ICloudSessionExpired):
        return 'Apple Cookie 已确认失效，请重新登录并更新 Cookie'
    status = re.search(r"HTTP (\d{3})", str(error))
    if status:
        code = status.group(1)
        return f"上游服务返回 HTTP {code}"
    if isinstance(error, ValueError):
        # 不向页面返回上游原始响应或敏感配置。
        return "配置或输入无效，请检查源地址、账号和收件配置"
    return "网络或服务请求失败；未自动重发，请检查配置后重试"


class WebMailSender:
    """复用 iCloud 网页登录会话发信；请求超时不重发，避免重复邮件。"""
    def __init__(self, client, account):
        self.client = client
        self.account = account

    def prepare(self):
        data = self.client.validate_session(max_attempts=1)
        service = data.get("webservices", {}).get("mail", {}).get("url", "")
        parsed = urlparse(service)
        suffix = ".icloud.com.cn" if self.client.host == "icloud.com.cn" else ".icloud.com"
        if (parsed.scheme != "https" or not (parsed.hostname or "").endswith(suffix)
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("iCloud 未返回可信邮件服务")
        self.url = service.rstrip("/") + "/wm/message"
        self.dsid = str(data.get("dsInfo", {}).get("dsid") or "")
        sender = self.account.get("icloud_email", "").lower()
        if not re.fullmatch(r"[0-9]{1,64}", self.dsid) or not re.fullmatch(r"[a-z0-9_.+-]+@(icloud|me|mac)\.com", sender):
            raise ValueError("账号未配置 iCloud 主邮箱")
        self.sender = sender
        self.client_id = secrets.token_hex(16)

    def send(self, recipient, marker):
        payload = {"jsonrpc": "2.0", "id": secrets.token_hex(12), "method": "send", "params": {
            "date": format_datetime(datetime.now(timezone.utc)), "from": self.sender,
            "to": recipient, "recents": recipient, "subject": marker,
            "textBody": "iCloud 分裂地址对照收信测试。标记：" + marker,
            "body": "<p>iCloud 分裂地址对照收信测试：" + html.escape(marker) + "</p>",
            "attachments": [], "webmailClientBuild": "2624Hotfix105", "isHME": False,
        }}
        try:
            response = self.client._request("POST", self.url + "?dsid=" + self.dsid + "&clientId=" + self.client_id,
                                            json_data=payload, timeout=20, max_attempts=1,
                                            client_build_number="2624Hotfix105", content_type="text/plain;charset=UTF-8")
        except Exception as error:
            return {"send_status": "unknown", "error": safe_error(error)}
        if response.get("error"):
            return {"send_status": "rejected", "error": "iCloud 拒绝发信请求"}
        if response.get("result") is None:
            return {"send_status": "unknown", "error": "iCloud 未确认发信结果"}
        return {"send_status": "accepted"}


def inbox_subjects(record):
    """只读取保存的目标收件箱主题，并限制上游域名与响应大小。"""
    legacy = record.get("forward_inbox_url") or ""
    parsed = urlparse(legacy)
    if parsed.scheme != "https" or parsed.netloc != "webmail.fastaiapi.cloud":
        raise ValueError("缺少可信收件凭据")
    token = parse_qs(parsed.query).get("jwt", [""])[0]
    if not token:
        raise ValueError("缺少收件凭据")
    response = requests.get("https://mail-api.fastaiapi.cloud/api/mails?limit=30&offset=0",
                            headers={"Authorization": "Bearer " + token}, timeout=15,
                            allow_redirects=False, stream=True)
    with response:
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        raw = response.raw.read(2_000_001, decode_content=True)
        if len(raw) > 2_000_000:
            raise ValueError("收件响应过大")
    import json
    from email import message_from_string
    from email.header import decode_header, make_header
    data = json.loads(raw)
    if not isinstance(data.get("results"), list):
        raise ValueError("收件响应无效")
    subjects = set()
    for item in data["results"][:30]:
        if str(item.get("address", "")).lower() != record["destination"].lower():
            continue
        message = message_from_string(str(item.get("raw", "")))
        subjects.add(str(make_header(decode_header(message.get("Subject", "")))))
    return subjects



def setup_shared_inbox(base, target):
    source = load_links()[base]
    existing = load_links().get(target, {})
    if existing and (existing.get("base_email") != base or existing.get("account_id") != source["account_id"]
                     or existing.get("destination") != source["destination"]
                     or existing.get("forward_inbox_url") != source["forward_inbox_url"]):
        raise ValueError("分裂地址已有不同的收件配置，未覆盖")
    if existing.get("public_registered"):
        return update_link(target, {"configured": True, "stage": "ready"})
    record = update_link(target, {
        "email": target, "account_id": source["account_id"], "base_email": base,
        "kind": "plus", "shared_inbox": True, "destination": source["destination"],
        "forward_inbox_url": source["forward_inbox_url"],
        "configured": False, "verified": False, "stage": "publishing_shared_inbox",
    })
    publish_inbox_link(record)
    return update_link(target, {"configured": True, "stage": "ready", "last_error": None})


class PlusJobs:
    def __init__(self, manager, sender_factory=WebMailSender, reader=inbox_subjects):
        self.manager = manager
        self.storage = manager.storage
        self.sender_factory = sender_factory
        self.reader = reader
        self.lock = threading.RLock()
        self.jobs = {}
        self.active = None
        self.cancel_event = threading.Event()

    def candidates(self):
        return [{"email": email, "account_id": row.get("account_id")}
                for email, row in load_links().items()
                if "+" not in email and is_ready(row) and row.get("account_id") in self.manager.accounts]

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                job = self.storage.read_document("plus_job", job_id)
                if job and job.get("status") == "running":
                    job.update(status="interrupted", error="服务已重启，未自动重发测试邮件")
                    self.storage.write_document("plus_job", job, job_id)
            return copy.deepcopy(job)

    def _save(self, job):
        with self.lock:
            self.jobs[job["id"]] = copy.deepcopy(job)
            self.storage.write_document("plus_job", job, job["id"])

    def start(self, sender_id, bases, tags, timeout=120):
        if not isinstance(sender_id, str) or sender_id not in self.manager.accounts:
            raise ValueError("请选择发件账号")
        if (not isinstance(bases, list) or not 1 <= len(bases) <= 3
                or any(not isinstance(x, str) for x in bases) or len(set(bases)) != len(bases)):
            raise ValueError("请选择 1–3 个不同的源地址")
        if not isinstance(tags, list) or not 1 <= len(tags) <= 3:
            raise ValueError("请填写 1–3 个后缀")
        variants = [plus_address(base, tag) for base in bases for tag in tags]
        if len(variants) != len(set(variants)):
            raise ValueError("后缀不能重复（不区分大小写）")
        if type(timeout) is not int or not 30 <= timeout <= 300:
            raise ValueError("等待时间必须为 30–300 秒的整数")
        records = load_links()
        for base in bases:
            row = records.get(base, {})
            if not is_ready(row) or row.get("account_id") not in self.manager.accounts:
                raise ValueError("源地址必须是本地已配置收件的隐藏地址")
        with self.lock:
            if self.active:
                raise RuntimeError("已有分裂收信测试正在运行")
            job_id = secrets.token_hex(12)
            job = {"id": job_id, "status": "running", "sender_id": sender_id,
                   "bases": bases, "tags": tags, "rows": [], "lines": [],
                   "progress": "检查发件和收件账号会话", "started_at": time.time()}
            self._save(job)
            self.active = job_id
            self.cancel_event.clear()
            threading.Thread(target=self._run, args=(job, timeout), daemon=True).start()
            return job_id

    def cancel(self, job_id):
        with self.lock:
            if self.active != job_id:
                return False
            self.cancel_event.set()
            return True


    def _run(self, job, timeout):
        try:
            sender = self.sender_factory(self.manager.get_client(job["sender_id"]),
                                         self.manager.accounts[job["sender_id"]])
            sender.prepare()  # 发件会话失效时，先停止，再配置地址。
            for base in job["bases"]:
                if self.cancel_event.is_set():
                    break
                targets = [(base, "base")] + [(plus_address(base, tag), "plus") for tag in job["tags"]]
                for target, kind in targets:
                    if self.cancel_event.is_set():
                        break
                    job["progress"] = "配置并发送：" + target
                    self._save(job)
                    row = {"email": target, "base": base, "kind": kind,
                           "marker": "HME-PROBE-" + secrets.token_hex(16), "received": False,
                           "receive_status": "waiting", "send_status": "not_sent"}
                    job["rows"].append(row)
                    self._save(job)
                    if kind == "plus":
                        setup_shared_inbox(base, target)
                    # 发信前保存发送意图；重启后不自动重发。
                    row["send_status"] = "sending"
                    self._save(job)
                    row.update(sender.send(target, row["marker"]))
                    self._save(job)
            deadline = time.monotonic() + timeout
            while job["rows"] and not self.cancel_event.is_set():
                job["progress"] = "等待测试邮件（发信确认不代表收到）"
                links = load_links()
                subjects = {}
                for target in dict.fromkeys([r["email"] for r in job["rows"]] + job["bases"]):
                    try:
                        subjects[target] = self.reader(links[target])
                    except Exception as error:
                        job["read_error"] = safe_error(error)
                for row in job["rows"]:
                    if row["received"] or row["send_status"] == "rejected":
                        continue
                    arrived_at = [address for address, values in subjects.items() if row["marker"] in values]
                    if arrived_at:
                        row.update(received=True, received_at=time.time(), arrived_at=arrived_at,
                                   receive_status="shared_inbox" if row["kind"] == "plus" and
                                   (row["email"] in arrived_at or row["base"] in arrived_at) else
                                   "own_inbox" if row["email"] in arrived_at else "other_inbox")
                        if row["kind"] == "plus" and row["receive_status"] == "shared_inbox":
                            update_link(row["email"], {"verified": True, "verified_at": time.time()})
                            existing = {r["email"] for r in self.storage.emails()}
                            if row["email"] not in existing:
                                self.storage.add_email(row["email"], links[row["email"]]["account_id"])
                            job["lines"].append(row["email"] + "----" + links[row["email"]]["inbox_url"])
                self._save(job)
                if all(r["received"] or r["send_status"] == "rejected" for r in job["rows"]) or time.monotonic() >= deadline:
                    break
                self.cancel_event.wait(min(5, max(0, deadline - time.monotonic())))
            for row in job["rows"]:
                if not row["received"]:
                    row["receive_status"] = "cancelled" if self.cancel_event.is_set() else "not_observed"
            job["status"] = "cancelled" if self.cancel_event.is_set() else "completed"
        except Exception as error:
            job.update(status="failed", error=safe_error(error))
        finally:
            job["finished_at"] = time.time()
            job["progress"] = {"failed": "测试未完成，请检查错误", "cancelled": "测试已停止", "completed": "测试已结束，请查看每封邮件结果"}.get(job["status"], job["status"])
            self._save(job)
            with self.lock:
                self.active = None
