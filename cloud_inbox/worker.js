import PostalMime from 'postal-mime';
import { renderPage } from './page.js';

const BASE_HEADERS = {
  'Cache-Control': 'no-store',
  'Referrer-Policy': 'no-referrer',
  'X-Robots-Tag': 'noindex, nofollow, noarchive',
  'X-Content-Type-Options': 'nosniff',
};

export async function hashToken(token) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

export function equalSecret(left, right) {
  if (typeof left !== 'string' || typeof right !== 'string' || left.length !== right.length) return false;
  let difference = 0;
  for (let i = 0; i < left.length; i++) difference |= left.charCodeAt(i) ^ right.charCodeAt(i);
  return difference === 0;
}

export function extractCode(text) {
  const context = /(?:verification\s+code|one[ -]time\s+(?:code|password)|验证码|驗證碼|安全码|code)[^\d]{0,90}(\d{4,8})(?!\d)/i.exec(text || '');
  if (context) return context[1];
  return /(?:^|\D)(\d{6})(?!\d)/.exec(text || '')?.[1] || '';
}

function json(value, status = 200) {
  return new Response(JSON.stringify(value), {status, headers: {...BASE_HEADERS, 'Content-Type': 'application/json; charset=utf-8'}});
}

export async function readMessages(record, fetcher = fetch) {
  const response = await fetcher('https://mail-api.fastaiapi.cloud/api/mails?limit=30&offset=0', {
    headers: {Authorization: 'Bearer ' + record.api_token}, signal: AbortSignal.timeout(20000),
  });
  if (!response.ok) throw new Error('上游收件服务暂不可用（HTTP ' + response.status + '），请稍后刷新');
  const data = await response.json();
  if (!Array.isArray(data.results)) throw new Error('上游收件服务返回了无效数据');
  const messages = [];
  for (const item of data.results.slice(0, 30)) {
    if (item.address && item.address.toLowerCase() !== record.destination.toLowerCase()) continue;
    const mail = await PostalMime.parse(String(item.raw || ''));
    const text = String(mail.text || '');
    messages.push({id: String(item.id), subject: mail.subject || '（无主题）',
      from: mail.from?.name ? `${mail.from.name} <${mail.from.address}>` : mail.from?.address || item.source || '',
      date: mail.date || item.created_at || '', text, html: text ? '' : String(mail.html || ''),
      code: extractCode((mail.subject || '') + '\n' + text + '\n' + String(mail.html || '').replace(/<[^>]*>/g, ' ')),
    });
  }
  return messages;
}

async function handle(request, env) {
  const url = new URL(request.url);
  if (url.pathname === '/m/health') return json({ok: true});
  const admin = /^\/m\/_admin\/mailboxes\/(mbx_\d{9})$/.exec(url.pathname);
  if (admin) {
    if (!env.ADMIN_TOKEN || !equalSecret(request.headers.get('Authorization'), 'Bearer ' + env.ADMIN_TOKEN)) return json({error: 'Unauthorized'}, 401);
    if (request.method === 'DELETE') {
      const data = await request.json();
      if (!/^[^\s@]+@icloud\.com$/.test(data.alias || '') || !/^[a-f0-9]{64}$/.test(data.token_hash || '')) return json({error: 'Invalid mailbox'}, 400);
      const existing = await env.DB.prepare('SELECT * FROM hme_public_inboxes WHERE id = ?').bind(admin[1]).first();
      // 删除同时校验邮箱和原短链接凭据，防止误撤销其他地址；已不存在时幂等成功。
      if (existing && (existing.alias !== data.alias || !equalSecret(existing.token_hash, data.token_hash))) return json({error: 'Mailbox mapping conflict'}, 409);
      if (existing) await env.DB.prepare('DELETE FROM hme_public_inboxes WHERE id = ? AND alias = ? AND token_hash = ?')
        .bind(admin[1], data.alias, data.token_hash).run();
      const remaining = await env.DB.prepare('SELECT * FROM hme_public_inboxes WHERE id = ?').bind(admin[1]).first();
      return remaining ? json({error: 'Deletion not confirmed'}, 502) : json({ok: true, id: admin[1], alias: data.alias});
    }
    if (request.method !== 'PUT') return json({error: 'Method not allowed'}, 405);
    const data = await request.json();
    if (!/^[^\s@]+@icloud\.com$/.test(data.alias || '') || !/^[a-f0-9]{64}$/.test(data.token_hash || '')
        || typeof data.api_token !== 'string' || data.api_token.length < 20
        || !/^[^\s@]+@fastaiapi\.cloud$/.test(data.destination || '')) return json({error: 'Invalid mailbox'}, 400);
    const existing = await env.DB.prepare('SELECT * FROM hme_public_inboxes WHERE id = ? OR alias = ?').bind(admin[1], data.alias).first();
    if (existing && (existing.id !== admin[1] || existing.alias !== data.alias || existing.token_hash !== data.token_hash)) return json({error: 'Mailbox mapping conflict'}, 409);
    await env.DB.prepare(`INSERT INTO hme_public_inboxes (id,alias,token_hash,api_token,destination) VALUES (?,?,?,?,?)
      ON CONFLICT(id) DO UPDATE SET api_token=excluded.api_token,destination=excluded.destination`)
      .bind(admin[1], data.alias, data.token_hash, data.api_token, data.destination).run();
    return json({ok: true, id: admin[1], alias: data.alias});
  }
  if (!['GET', 'HEAD'].includes(request.method)) return json({error: 'Method not allowed'}, 405);
  const match = /^\/m\/(mbx_\d{9})\.([A-Za-z0-9_-]{43})(\/api\/messages)?$/.exec(url.pathname);
  if (!match) return json({error: '收件链接无效'}, 404);
  const record = await env.DB.prepare('SELECT * FROM hme_public_inboxes WHERE id = ?').bind(match[1]).first();
  if (!record || !equalSecret(await hashToken(match[2]), record.token_hash)) return json({error: '收件链接无效或已撤销'}, 404);
  if (match[3]) {
    const messages = await readMessages(record);
    return json({email: record.alias, messages, count: messages.length, code: messages.find(message => message.code)?.code || '', synced_at: new Date().toISOString()});
  }
  const nonce = crypto.randomUUID().replaceAll('-', '');
  return new Response(request.method === 'HEAD' ? null : renderPage(record.alias, nonce), {
    headers: {...BASE_HEADERS, 'Content-Type': 'text/html; charset=utf-8',
      'Content-Security-Policy': `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`},
  });
}

export default {
  async fetch(request, env) {
    try { return await handle(request, env); }
    catch (error) { return json({error: error instanceof SyntaxError ? '请求格式无效' : '读取邮件失败，请稍后刷新'}, error instanceof SyntaxError ? 400 : 502); }
  },
};
