"""安全删除源地址的底层收件箱，分裂地址只撤销自己的短链接。"""
from urllib.parse import urlsplit, parse_qs
import requests


def delete_domain_inbox(record, storage, http=None):
    if '+' in record.get('email', '').split('@')[0]:
        raise ValueError('分裂地址不能删除共享收件箱')
    destination = record.get('destination')
    if not destination:
        if record.get('inbox_url'):
            raise ValueError('收件箱归属信息不完整')
        return
    # 其他源或分裂地址仍使用这个收件箱时，不得删除共享数据。
    for email, link in storage.load_links().items():
        if email != record['email'] and link.get('destination') == destination:
            raise RuntimeError('仍有其他邮箱关联该收件箱，未删除共享数据')
    parsed = urlsplit(record.get('forward_inbox_url') or record.get('inbox_url', ''))
    token = parse_qs(parsed.query).get('jwt', [''])[0]
    if parsed.scheme != 'https' or parsed.netloc != 'webmail.fastaiapi.cloud' or not token:
        raise RuntimeError('缺少可信的源收件箱凭据')
    http = http or requests.Session()
    headers = {'Authorization': 'Bearer ' + token}
    try:
        identity = http.get('https://mail-api.fastaiapi.cloud/api/settings', headers=headers,
                            timeout=25, allow_redirects=False)
        # 删除后重试时，旧 JWT 可能仍被服务接受；必须依赖明确的成功响应。
        if identity.status_code != 200 or identity.json().get('address', '').lower() != destination.lower():
            raise RuntimeError('收件箱归属校验失败，未确认删除')
        response = http.delete('https://mail-api.fastaiapi.cloud/api/delete_address', headers=headers,
                               timeout=25, allow_redirects=False)
        if response.status_code != 200 or response.json().get('success') is not True:
            raise RuntimeError('收件箱服务未确认删除，请重试')
    except requests.RequestException:
        raise RuntimeError('收件箱删除结果未确认，可重试') from None
