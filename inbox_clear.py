"""Clear selected source inboxes using their scoped mailbox credentials only."""
from urllib.parse import urlsplit,parse_qs
import requests
from creation_jobs import CreationJobs

API = 'https://mail-api.fastaiapi.cloud'


def clear_inbox(record, http=None):
    if '+' in record.get('email','').split('@',1)[0]:
        raise ValueError('清空操作只允许源邮箱，分裂邮箱不处理')
    http = http or requests.Session()
    parsed = urlsplit(record.get('forward_inbox_url') or record.get('inbox_url',''))
    token = parse_qs(parsed.query).get('jwt',[''])[0]
    if parsed.scheme!='https' or parsed.netloc!='webmail.fastaiapi.cloud' or not token:
        raise ValueError('缺少可信收件凭据，未清空')
    headers = {'Authorization':'Bearer '+token}
    def call(method,path):
        try:
            response = http.request(method,API+path,headers=headers,timeout=25,allow_redirects=False)
        except requests.RequestException:
            raise RuntimeError('收件服务连接失败；清空请求不会自动重发，请检查收件箱后重试') from None
        if response.status_code != 200:
            raise RuntimeError(f'收件服务返回 HTTP {response.status_code}，请检查收件服务权限')
        try: return response.json()
        except ValueError: raise RuntimeError('收件服务返回格式无效') from None
    identity = call('GET','/api/settings')
    if str(identity.get('address','')).lower()!=record.get('destination','').lower() or not record.get('destination'):
        raise RuntimeError('收件箱归属校验失败，未清空')
    result = call('DELETE','/api/clear_inbox')
    if result.get('success') is not True: raise RuntimeError('收件服务未确认清空成功，请检查后重试')
    return True


class InboxClearJobs(CreationJobs):
    document_kind = 'inbox_clear_job'
    target_label = '源邮箱'

    def start_inboxes(self, records):
        with self._lock:
            if self._active: raise RuntimeError('已有清空任务运行中')
            self.records = {record['email']:dict(record) for record in records}
            return super().start(list(self.records),count=1,interval=0)

    def create_item(self, target, label):
        clear_inbox(self.records[target])
        return {'ok':True,'email':target,'line':target+' 清空完成'}
