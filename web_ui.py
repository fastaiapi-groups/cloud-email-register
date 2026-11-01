#!/usr/bin/env python3
"""iCloud HME Web UI — 多账号聚合管理平台 — Flask single-page app."""
import sys, os, json, time, queue, secrets, threading
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path: sys.path.insert(0, str(HERE))

from flask import Flask, Response, request, jsonify, render_template_string
from icloud_hme import ICloudHME, ICloudMailSessionExpired, extract_chrome_cookies
from account_manager import AccountManager
from inbox_links import load_links, export_lines, is_ready
from creation_jobs import CreationJobs
from auto_registration import AutoRegistration
from storage import Storage, database_for
from fastai_sync import FastAISync
from email_delete import EmailDeleteJobs
from registration_rotation import RegistrationJobs
from plus_addressing import PlusJobs
from auto_plus import PlusCreationJobs, AutoPlusAddresses
import email_catalog
from inbox_clear import InboxClearJobs
from session_maintenance import SessionMaintenance

# ---- config ----
RESULTS_DIR = HERE / "results"
LOGS_DIR = HERE / "logs"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
from auth_web import install_auth
install_auth(app, HERE)
_log_queue = queue.Queue()
_today_key = datetime.now().strftime("%Y%m%d")
_global_state = {"running":False,"creating":False,"round_status":"","total_created":0,"today_created":0,"current_round_created":0,"next_trigger":None,"last_error":None,"cookies_ok":False,"alias_count":0,"alias_active":0}
_lock = threading.Lock()
_stop_event = threading.Event()
_account_mgr = AccountManager()
_plus_jobs = PlusJobs(_account_mgr)
_plus_creation_jobs = PlusCreationJobs(_account_mgr)
_fastai_sync = FastAISync(_account_mgr.storage)
_auto_plus = AutoPlusAddresses(_plus_creation_jobs, _account_mgr, RESULTS_DIR / "auto_plus.json", fastai_sync=_fastai_sync)
_creation_jobs = CreationJobs(_account_mgr, on_result=lambda result: _increment_state(today_created=1, total_created=1) if result.get("alias_created") else None)
_registration_jobs = RegistrationJobs(_account_mgr, on_result=_creation_jobs.on_result)
_auto_registration = AutoRegistration(_registration_jobs, _account_mgr, RESULTS_DIR / "auto_registration.json", fastai_sync=_fastai_sync)
_inbox_clear_jobs = InboxClearJobs(_account_mgr)
_email_delete_jobs = EmailDeleteJobs(_account_mgr)
_registration_jobs.busy = lambda: any(j.active for j in (_creation_jobs, _plus_creation_jobs, _plus_jobs, _inbox_clear_jobs, _email_delete_jobs))
_email_bulk_lock = threading.RLock()

_RATE_LIMIT_KW = ["limit","exceeded","maximum","quota","429","too many","try again","unavailable","上限","超过","过多","频繁","rate limit","throttle","blocked"]

def _is_limit_error(err: str) -> bool: return any(kw in err.lower() for kw in _RATE_LIMIT_KW)

_time_offset = 0.0
def _sync_time():
    global _time_offset
    for url in ["https://www.baidu.com","https://www.cloudflare.com","https://www.microsoft.com"]:
        try:
            import requests as _r
            resp = _r.head(url, timeout=5)
            date_str = resp.headers.get("Date","")
            if date_str:
                from email.utils import parsedate_to_datetime
                net_time = parsedate_to_datetime(date_str)
                _time_offset = (net_time - datetime.now()).total_seconds()
                return _time_offset
        except: continue
    return 0.0

def _now() -> datetime: return datetime.now() + timedelta(seconds=_time_offset)

def _emit_log(level, msg): _log_queue.put({"time":_now().strftime("%H:%M:%S"),"level":level,"msg":msg})

def _update_state(**kw):
    global _today_key
    with _lock:
        today = _now().strftime("%Y%m%d")
        if today != _today_key: _global_state["today_created"] = 0; _today_key = today
        _global_state.update(kw)

def _increment_state(**kw):
    global _today_key
    with _lock:
        today = _now().strftime("%Y%m%d")
        if today != _today_key: _global_state["today_created"] = 0; _today_key = today
        for k, delta in kw.items(): _global_state[k] = _global_state.get(k,0) + delta

def _health_loop():
    SessionMaintenance(_account_mgr, _emit_log).run(_stop_event)

# ----- HTML -----
UI_HTML = r"""<!DOCTYPE html><html lang="zh-CN"><head><link rel="stylesheet" href="/static/email_list.css?v=20261027-dialog2"><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>iCloud HME — 多账号管理</title><style>:root{--paper:#f3efe4;--paper-dim:#e8e2d4;--ink:#0f0e0c;--ink-soft:#5c564e;--ink-faint:#9a938a;--rule:rgba(15,14,12,.12);--rule-strong:rgba(15,14,12,.22);--red:#b7392d;--green:#1f8b4c;--mono:"SF Mono","Fira Code","Cascadia Code",Consolas,monospace;--sans:"PingFang SC","Microsoft YaHei","Noto Sans SC",system-ui,sans-serif}*{margin:0;padding:0;box-sizing:border-box}html{min-width:1040px;background:var(--paper);font-size:16px}body{color:var(--ink);font-family:var(--sans);min-height:100vh;display:flex;background:radial-gradient(circle at 10% 8%,rgba(183,57,45,.03),transparent 26%),radial-gradient(circle at 78% 42%,rgba(15,14,12,.025),transparent 30%),linear-gradient(90deg,rgba(15,14,12,.018) 1px,transparent 1px),linear-gradient(rgba(15,14,12,.018) 1px,transparent 1px),var(--paper);background-size:auto,auto,64px 64px,64px 64px,auto}.sidebar{width:260px;background:var(--paper);border-right:1px solid var(--rule-strong);padding:28px 22px;display:flex;flex-direction:column;gap:3px;flex-shrink:0;overflow-y:auto}.sidebar .logo{font-family:var(--mono);font-size:15px;letter-spacing:.28em;text-transform:uppercase;color:var(--ink-faint);margin-bottom:24px;display:flex;align-items:center;gap:14px}.sidebar .logo .icon{width:16px;height:16px;background:var(--red);transform:rotate(45deg);flex-shrink:0}.sidebar .nav-item{padding:10px 0;color:var(--ink-soft);font-size:15px;cursor:pointer;user-select:none;display:flex;align-items:center;gap:10px;border-bottom:1px solid transparent;transition:border-color .2s,color .2s;font-family:var(--mono);letter-spacing:.03em}.sidebar .nav-item:hover{color:var(--ink);border-bottom-color:var(--rule)}.sidebar .nav-item.active{color:var(--ink);border-bottom-color:var(--red);font-weight:600}.sidebar .section-label{font-family:var(--mono);font-size:11px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.3em;padding:22px 0 10px}.sidebar .account-item{padding:9px 0;font-size:13px;cursor:pointer;display:flex;align-items:center;gap:10px;border-left:2px solid transparent;padding-left:10px;transition:all .15s;font-family:var(--mono)}.sidebar .account-item:hover{color:var(--ink)}.sidebar .account-item.selected{border-left-color:var(--red);font-weight:600}.sidebar .account-item .acc-dot{width:7px;height:7px;transform:rotate(45deg);flex-shrink:0}.sidebar .account-item .acc-dot.active{background:var(--green)}.sidebar .account-item .acc-dot.error{background:var(--red)}.sidebar .account-item .acc-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.sidebar .account-item .acc-del{opacity:0;color:var(--red);cursor:pointer;font-size:16px;line-height:1}.sidebar .account-item:hover .acc-del{opacity:.5}.sidebar .account-item .acc-del:hover{opacity:1}#sidebarAccounts{max-height:340px;overflow-y:auto}.status-dot{display:inline-block;width:7px;height:7px;transform:rotate(45deg);margin-right:8px;vertical-align:middle}.status-dot.online{background:var(--green)}.status-dot.offline{background:var(--ink-faint)}.main{flex:1;padding:32px 44px;overflow-y:auto;display:flex;flex-direction:column;gap:24px}.header{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:16px}.header h1{font-family:var(--mono);font-size:14px;color:var(--ink-faint);letter-spacing:.28em;text-transform:uppercase;font-weight:400}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1px;background:var(--rule-strong);border:1px solid var(--rule-strong)}.card{background:var(--paper);padding:22px 24px;transition:background .15s}.card:hover{background:var(--paper-dim)}.card .label{font-family:var(--mono);font-size:11px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.3em;margin-bottom:10px}.card .value{font-size:38px;font-weight:800;letter-spacing:-1px;font-family:var(--mono)}.card .value.accent{color:var(--red)}.card .value.green{color:var(--green)}.card .value.orange{color:var(--ink-soft)}.card .value.blue{color:var(--ink)}.card .sub{font-size:13px;color:var(--ink-faint);margin-top:6px;font-family:var(--mono)}.acc-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1px;background:var(--rule-strong);border:1px solid var(--rule-strong);margin-top:2px}.acc-card{background:var(--paper);padding:22px 24px;transition:background .15s}.acc-card:hover{background:var(--paper-dim)}.acc-card .acc-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px}.acc-card .acc-title{font-weight:700;font-size:16px;font-family:var(--mono)}.acc-card .acc-email{font-size:13px;color:var(--ink-faint);font-family:var(--mono);margin-top:4px}.acc-card .acc-stats{display:flex;gap:24px;margin-top:12px}.acc-card .acc-stat{font-size:13px;font-family:var(--mono);color:var(--ink-soft)}.acc-card .acc-stat .n{font-weight:700;color:var(--ink)}.acc-card .acc-actions{margin-top:14px;display:flex;gap:8px}.acc-card .status-badge{font-family:var(--mono);font-size:11px;padding:2px 0;letter-spacing:.08em;text-transform:uppercase}.acc-card .status-badge.ok{color:var(--green);border-bottom:1px solid var(--green)}.acc-card .status-badge.err{color:var(--red);border-bottom:1px solid var(--red)}.panel{background:var(--paper);border:1px solid var(--rule-strong);overflow:hidden}.panel-header{padding:14px 20px;border-bottom:1px solid var(--rule);display:flex;justify-content:space-between;align-items:center;font-family:var(--mono);font-size:12px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.16em}.panel-body{padding:0}.btn{padding:9px 22px;font-size:13px;cursor:pointer;border:none;font-family:var(--mono);transition:all .15s;letter-spacing:.03em;background:var(--ink);color:var(--paper)}.btn:hover{opacity:.78}.btn:disabled{opacity:.28;cursor:not-allowed}.btn-primary{background:var(--ink);color:var(--paper)}.btn-outline{background:transparent;border:1px solid var(--rule-strong);color:var(--ink)}.btn-outline:hover{background:var(--ink);color:var(--paper);border-color:var(--ink);opacity:1}.btn-danger{background:transparent;color:var(--red);border:1px solid var(--red)}.btn-danger:hover{background:var(--red);color:var(--paper);opacity:1}.btn-sm{padding:5px 14px;font-size:12px}.btn-xs{padding:3px 10px;font-size:11px}.btn-group{display:flex;gap:10px}.chk-group{display:flex;flex-wrap:wrap;gap:10px;padding:10px 0}.chk-item{display:flex;align-items:center;gap:8px;font-size:14px;cursor:pointer;font-family:var(--mono)}.chk-item input{margin:0;accent-color:var(--red);width:16px;height:16px}#aliasTableContainer{overflow-x:auto;max-width:100%}#view-emails .panel-header>div{flex-wrap:wrap}.email-table td:nth-child(3){max-width:220px;overflow-wrap:anywhere}.email-table{width:100%;border-collapse:collapse;font-family:var(--mono)}.email-table th{text-align:left;padding:10px 18px;font-size:11px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.3em;border-bottom:1px solid var(--rule-strong);font-weight:400}.email-table td{padding:12px 18px;font-size:14px;border-bottom:1px solid var(--rule)}.email-table tr:hover td{background:var(--paper-dim)}.email-item:hover{background:var(--paper-dim)}.email-table .copy-btn{background:none;border:none;color:var(--ink-faint);cursor:pointer;font-size:15px;padding:3px 8px}.email-table .copy-btn:hover{color:var(--red)}.filter-bar{display:flex;gap:12px;align-items:center;padding:10px 18px;border-bottom:1px solid var(--rule)}.filter-bar select{padding:6px 10px;border:1px solid var(--rule-strong);font-family:var(--mono);font-size:13px;background:var(--paper);color:var(--ink)}.filter-bar select:focus{outline:none;border-color:var(--red)}.copy-toast{position:fixed;top:24px;right:24px;background:var(--ink);color:var(--paper);padding:12px 24px;font-family:var(--mono);font-size:13px;letter-spacing:.03em;opacity:0;transform:translateY(-8px);transition:all .2s;pointer-events:none;z-index:999}.copy-toast.show{opacity:1;transform:translateY(0)}.log-feed{max-height:320px;overflow-y:auto;padding:14px 20px;font-family:var(--mono);font-size:13px;line-height:1.8}.log-feed .log-line{white-space:pre-wrap;word-break:break-all}.log-line.info{color:var(--ink-soft)}.log-line.success{color:var(--green)}.log-line.warn{color:var(--red)}.log-line.error{color:var(--red);font-weight:600}.log-time{color:var(--ink-faint);margin-right:10px}.empty{text-align:center;padding:56px 20px;color:var(--ink-faint);font-family:var(--mono);font-size:13px;letter-spacing:.03em}.empty .icon{font-size:42px;margin-bottom:14px;opacity:.5}.modal-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(15,14,12,.7);z-index:999;display:flex;align-items:center;justify-content:center}.modal-box{background:var(--paper);border:1px solid var(--ink);padding:32px;width:90%;max-width:560px;box-shadow:8px 8px 0 rgba(15,14,12,.12)}.modal-box h3{font-family:var(--mono);font-size:15px;letter-spacing:.16em;text-transform:uppercase;margin-bottom:10px;font-weight:400}.modal-box p{font-size:14px;color:var(--ink-soft);margin-bottom:16px;line-height:1.6}.modal-box input,.modal-box textarea{width:100%;background:var(--paper);color:var(--ink);border:1px solid var(--rule-strong);padding:12px 14px;font-family:var(--mono);font-size:14px;margin-bottom:14px}.modal-box textarea{height:130px;font-size:13px;resize:vertical}.modal-box input:focus,.modal-box textarea:focus{outline:none;border-color:var(--ink)}.modal-actions{display:flex;gap:12px;margin-top:16px;justify-content:flex-end}.modal-msg{margin-top:12px;font-family:var(--mono);font-size:13px}.diamond{display:inline-block;width:12px;height:12px;background:var(--red);transform:rotate(45deg);vertical-align:-2px;margin-right:4px}code{font-family:var(--mono);font-size:12px;background:var(--paper-dim);padding:1px 6px}.progress-bar{height:3px;background:var(--rule);margin-top:10px;overflow:hidden}.progress-bar .fill{height:100%;background:var(--ink);transition:width .3s}select,input[type=text],input[type=number],input[type=password]{font-family:var(--mono);font-size:13px;padding:6px 10px;border:1px solid var(--rule-strong);background:var(--paper);color:var(--ink)}select:focus,input:focus{outline:none;border-color:var(--ink)}@media(max-width:768px){body{flex-direction:column}.sidebar{width:100%;flex-direction:row;flex-wrap:wrap;padding:14px 18px;gap:6px}.sidebar .logo{margin-bottom:0;margin-right:auto}.main{padding:16px}.cards{grid-template-columns:repeat(2,1fr)}.acc-cards{grid-template-columns:1fr}}#aliasTableContainer{overflow-x:auto}.email-table{min-width:1000px}.email-table th,.email-table td{white-space:nowrap}.email-sort-header{display:block;width:100%;padding:0;border:0;background:transparent;font:inherit;color:inherit;cursor:pointer;text-align:left;white-space:nowrap}.email-sort-header:hover{color:var(--green)}.email-sort-header:focus-visible{outline:2px solid var(--green);outline-offset:4px}.email-time{font:12px var(--mono);color:var(--ink-soft)}.inbox-address-cell{min-width:200px}.inbox-address{display:block;max-width:320px;padding:4px 0;border:0;background:transparent;color:var(--green);font:12px var(--mono);white-space:nowrap;cursor:pointer;text-align:left}.inbox-address:hover{text-decoration:underline}.inbox-address:focus-visible{outline:2px solid var(--green);outline-offset:3px}</style><style>html.auto-route{min-width:0}html.auto-route body{flex-direction:row;height:100vh;overflow:hidden}html.auto-route .main{min-width:0;padding:24px;gap:18px;height:100vh;overflow:hidden}html.auto-route .sidebar{width:220px;flex-direction:column;flex-wrap:nowrap;height:100vh;overflow-y:auto;gap:3px}html.auto-route .sidebar .logo{margin:0 0 24px}html.auto-route #autoFrame,html.auto-route #fastaiFrame{height:calc(100vh - 130px)!important;min-height:0!important}@media(max-width:900px){html.auto-route .sidebar{width:180px;padding:24px 16px}html.auto-route .main{padding:18px 14px}}html.auto-route #view-auto,html.auto-route #view-fastai,html.auto-route #view-autoplus,html.auto-route #view-plus{min-width:0}</style><link rel="stylesheet" href="/static/fastai_sync.css"></head><body><aside class="sidebar"><div class="logo"><div class="icon"></div>iCloud HME</div><a class="nav-item active" data-tab="dashboard">仪表盘</a><a class="nav-item" data-tab="emails">邮箱列表</a><a class="nav-item" data-tab="batch">批量创建</a><a class="nav-item" data-tab="auto">自动注册</a><a class="nav-item" data-tab="autoplus">自动分裂</a><a class="nav-item" data-tab="fastai">发送账号</a><a class="nav-item" data-tab="inbox">收件箱</a><a class="nav-item" data-tab="plus" href="/plus">分裂地址验证</a><a class="nav-item" data-tab="docs">API 文档</a><a class="nav-item" data-tab="logs">运行日志</a><div class="section-label">账号列表</div><div id="sidebarAccounts"></div><button class="btn btn-outline btn-sm" onclick="showAddAccountModal()" style="margin:8px 0">+ 添加账号</button><div style="margin-top:auto;padding-top:14px;border-top:1px solid var(--rule-strong);font-family:var(--mono);font-size:12px;color:var(--ink-faint)"><div style="margin-bottom:6px"><span class="status-dot" id="schedDot"></span><span id="schedLabel">调度器: 就绪</span></div><button class="btn btn-sm" id="btnSched" onclick="toggleScheduler()" style="width:100%;margin-top:6px">启动调度器</button></div></aside><main class="main"><div class="header"><h1 id="tabTitle">仪表盘</h1><div class="btn-group"><button class="btn btn-outline btn-sm" onclick="refreshAll()">刷新</button><button class="btn btn-primary btn-sm" onclick="showAddAccountModal()">+ 添加账号</button></div></div><div id="view-dashboard"><div class="cards" id="summaryCards"></div><div class="acc-cards" id="accCards"></div></div><div id="view-emails" style="display:none"><div class="panel"><div class="panel-header"><span>隐私邮箱列表</span><div style="display:flex;gap:8px;align-items:center"><span style="font-size:11px;color:var(--ink-faint)" id="emailCount">0</span><button class="btn btn-outline btn-sm" onclick="refreshEmails().then(renderAliasTable)">刷新</button><button class="btn btn-outline btn-sm" onclick="refreshAliases()" title="从 iCloud 云端同步标签和状态">云端同步</button><button class="btn btn-primary btn-sm" id="btnFastaiSync" onclick="startFastaiSync()">一键同步所有未同步</button><button class="btn btn-outline btn-sm" onclick="openFastaiConfig()">同步配置</button><button class="btn btn-outline btn-sm" onclick="openFastaiLatest()">查看同步结果</button><button class="btn btn-outline btn-sm" onclick="copyInboxLinks()">复制本页邮箱+链接</button><button class="btn btn-outline btn-sm" onclick="exportCSV()">本页 CSV</button><button class="btn btn-outline btn-sm" onclick="exportInboxLinks()">导出 TXT（每行一条）</button><button class="btn btn-outline btn-sm" id="btnRepairInbox" style="display:none" onclick="repairInboxLinks()">一键补齐收件地址</button></div></div><div class="filter-bar"><span style="font-size:11px;color:var(--ink-faint)">筛选账号:</span><select id="aliasFilter" onchange="changeEmailFilters()"><option value="all">全部账号</option></select><span style="font-size:11px;color:var(--ink-faint)">类型:</span><select id="addressTypeFilter" aria-label="地址类型" onchange="changeEmailFilters()"><option value="all">全部类型</option><option value="source">源地址</option><option value="split">分裂地址</option></select></div><div class="filter-bar" style="gap:10px;flex-wrap:wrap"><label>邮箱状态 <select id="emailStatusFilter" onchange="changeEmailFilters()"><option value="all">全部状态</option><option value="ready">收件已配置</option><option value="pending">待补全收件</option><option value="verified">已实收验证</option><option value="unverified">已配置待实测</option><option value="delete_failed">删除失败</option><option value="local_deleted">待远端删除（旧记录）</option><option value="source_deleted">源邮箱已删除</option></select></label><label>同步状态 <select id="emailSyncFilter" onchange="changeEmailFilters()"><option value="all">全部同步状态</option><option value="pending">待同步</option><option value="running">同步中</option><option value="synced">已同步</option><option value="failed">同步失败</option></select></label><input id="emailKeyword" type="search" placeholder="关键词筛选邮箱" aria-label="关键词筛选邮箱"><button id="emailSelectAll" class="btn btn-outline btn-sm" onclick="selectAllEmails()">全选筛选结果（跨页）</button><button id="emailUnselect" class="btn btn-outline btn-sm" onclick="unselectEmails()">取消选择</button><span id="emailSelectedCount">已选 0 个</span><button id="emailBulkCancel" class="btn btn-outline btn-sm" onclick="cancelEmailBulkJob()" hidden>停止批量任务</button><button data-email-bulk class="btn btn-outline btn-sm" onclick="bulkEmailAction('split')" disabled>批量分裂（每源邮箱 1 次）</button><button data-email-bulk class="btn btn-outline btn-sm" onclick="bulkEmailAction('repair')" disabled>批量补全收件地址</button><button data-email-bulk class="btn btn-outline btn-sm" onclick="bulkEmailAction('clear')" disabled>批量清空源收件箱</button><button class="btn btn-outline btn-sm" data-email-bulk onclick="bulkEmailAction('sync')" disabled>批量同步 FastAI</button><button class="btn btn-outline btn-sm" onclick="showEmailTaskHistory()">批量任务记录</button><button data-email-bulk class="btn btn-danger btn-sm" onclick="bulkEmailAction('delete')" disabled>批量删除邮箱及规则</button></div><div id="emailBulkStatus" role="status" style="padding:8px 16px;white-space:pre-wrap;overflow-wrap:anywhere"></div><div class="panel-body"><div id="aliasTableContainer" class="empty"><div class="icon"></div>暂无创建记录 — 请先通过仪表盘或批量创建生成邮箱</div></div><div class="filter-bar" style="justify-content:space-between"><span id="emailPageInfo">第 1 页</span><label>每页 <select id="emailPageSize"><option>25</option><option selected>50</option><option>100</option><option>200</option></select></label><span><button id="emailPrev" class="btn btn-outline btn-sm" onclick="emailGoPage(-1)">上一页</button> <button id="emailNext" class="btn btn-outline btn-sm" onclick="emailGoPage(1)">下一页</button></span></div></div></div><div id="view-batch" style="display:none"><div class="panel"><div class="panel-header"><span>跨账号批量创建</span><span style="font-size:11px;color:var(--ink-faint)" id="batchAccCount">0 个可用账号</span></div><div class="panel-body" style="padding:14px"><p style="font-size:12px;color:var(--ink-faint);margin-bottom:10px">勾选目标账号，设置每个账号的创建数量，一键创建 iCloud 别名、独立域名收件箱和转发规则，完成后输出每行「邮箱----收件链接」。</p><div class="chk-group" id="batchChkGroup"></div><div style="display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap"><label style="font-size:12px">每账号创建数量:</label><input type="number" id="batchCount" value="5" min="1" max="20" style="width:70px"><label style="font-size:13px;font-family:var(--mono)">标签前缀:</label><input type="text" id="batchLabel" placeholder="可选" style="width:150px"><button class="btn btn-primary" id="btnBatchExec" onclick="execBatchCreate()">一键创建并配置收件</button></div></div></div><div class="panel" style="margin-top:16px"><div class="panel-header"><span>一键创建结果 · 邮箱----收件链接</span><span id="batchProgress">等待创建</span></div><div style="padding:14px"><textarea id="batchOutput" readonly spellcheck="false" aria-label="邮箱和收件链接文本" style="width:100%;min-height:180px;resize:vertical;white-space:pre;font:12px var(--mono);padding:12px;background:var(--paper);color:var(--ink);border:1px solid var(--rule-strong)"></textarea><div class="btn-group" style="margin-top:10px"><button class="btn btn-outline btn-sm" onclick="copyBatchOutput()">复制本批文本</button><button class="btn btn-outline btn-sm" onclick="downloadBatchOutput()">导出本批 TXT</button></div><div id="batchErrors" style="font-size:12px;color:var(--red);white-space:pre-wrap;margin-top:10px"></div></div></div></div><div id="view-auto" style="display:none"><iframe id="autoFrame" title="自动注册配置和任务" loading="lazy" src="/auto-panel" style="width:100%;height:calc(100vh - 120px);min-height:780px;border:0"></iframe></div><div id="view-autoplus" style="display:none"><iframe id="autoPlusFrame" title="自动分裂配置和任务" loading="lazy" src="/auto-plus-panel" style="width:100%;height:calc(100vh - 120px);min-height:780px;border:0"></iframe></div><div id="view-plus" style="display:none"><iframe id="plusFrame" title="分裂地址验证" loading="lazy" src="/plus-panel" style="width:100%;height:calc(100vh - 130px);min-height:0;border:0"></iframe></div><div id="view-fastai" style="display:none"><iframe id="fastaiFrame" title="发送账号 配置和任务" loading="lazy" src="/fastai-sync-panel" style="width:100%;height:calc(100vh - 120px);min-height:780px;border:0"></iframe></div><div id="view-inbox" style="display:none"><div class="panel"><div class="panel-header"><span>收件箱检查</span><div style="display:flex;gap:8px;align-items:center"><select id="inboxAccount" onchange="refreshInbox()"></select><input type="number" id="inboxLimit" value="20" min="1" max="100" style="width:60px" title="邮件数量"><input type="text" id="aliasSearchInput" placeholder="指定邮箱查件..." style="width:200px" title="输入隐私邮箱地址查件"><button class="btn btn-outline btn-sm" onclick="refreshInbox()">刷新</button><button class="btn btn-outline btn-sm" onclick="refreshInbox(true)" title="跳过缓存，从 iCloud 重新拉取">强制刷新</button><button class="btn btn-outline btn-sm" onclick="searchAliasMail()" title="查询指定邮箱的收件">查件</button><button class="btn btn-outline btn-sm" onclick="checkAliasMail()" title="检查所有隐私别名的收件">全部</button><button class="btn btn-outline btn-sm" id="btnInboxSettings" onclick="openInboxSettings()" title="修改 iCloud 邮箱或应用密码">设置</button><span style="font-size:10px;color:var(--ink-faint);font-family:var(--mono)" id="cacheStatus"></span></div></div><div class="panel-body"><div id="inboxMsgs" class="empty"><div class="icon"></div>选择账号后点击刷新查看收件箱</div></div></div></div><div id="view-docs" style="display:none"><div class="panel" style="font-family:var(--mono);font-size:13px;line-height:1.8"><div class="panel-header"><span>API 文档</span></div><div class="panel-body" style="padding:20px 24px" id="docsContent"></div></div></div><div id="view-logs" style="display:none"><div class="panel"><div class="panel-header"><span>实时日志</span><button class="btn btn-outline btn-sm" onclick="clearLogs()">清屏</button></div><div class="panel-body"><div class="log-feed" id="logFeed"></div></div></div></div></main><div class="copy-toast" id="toast"></div><script src="/static/email_sort.js"></script><script src="/static/fastai_sync.js"></script><script>var E=function(id){return document.getElementById(id)};var state={running:false,creating:false,round_status:'',total_created:0,today_created:0,current_round_created:0,next_trigger:null};var accounts=[],emails=[],logs=[];var curTab='dashboard',sseConn=null;document.querySelectorAll('.nav-item[data-tab]').forEach(function(el){el.addEventListener('click',function(event){event.preventDefault();curTab=this.dataset.tab;document.documentElement.classList.toggle('auto-route',curTab==='auto'||curTab==='fastai'||curTab==='autoplus'||curTab==='plus');document.querySelectorAll('.nav-item').forEach(function(n){n.classList.remove('active')});this.classList.add('active');E('view-dashboard').style.display=curTab==='dashboard'?'block':'none';E('view-emails').style.display=curTab==='emails'?'block':'none';E('view-batch').style.display=curTab==='batch'?'block':'none';E('view-auto').style.display=curTab==='auto'?'block':'none';E('view-autoplus').style.display=curTab==='autoplus'?'block':'none';E('view-fastai').style.display=curTab==='fastai'?'block':'none';E('view-plus').style.display=curTab==='plus'?'block':'none';history.replaceState(null,'',curTab==='auto'?'/auto':curTab==='autoplus'?'/auto-plus':curTab==='fastai'?'/fastai-sync':curTab==='plus'?'/plus':'/');E('view-inbox').style.display=curTab==='inbox'?'block':'none';E('view-docs').style.display=curTab==='docs'?'block':'none';E('view-logs').style.display=curTab==='logs'?'block':'none';var titles={dashboard:'仪表盘',emails:'邮箱列表',batch:'批量创建',auto:'自动注册',autoplus:'自动分裂',fastai:'发送账号',plus:'分裂地址验证',inbox:'收件箱',docs:'API 文档',logs:'运行日志'};E('tabTitle').textContent=titles[curTab]||curTab;if(curTab==='emails'){refreshEmails();renderAliasTable();}if(curTab==='batch')renderBatchPanel();if(curTab==='inbox')updateInboxAccountSelect();if(curTab==='docs')renderDocs();if(curTab==='logs')renderLogs();});});async function api(path,opts){var timeout=(opts||{}).timeout||12000;if(opts)delete opts.timeout;var ctrl=new AbortController();var t=setTimeout(function(){ctrl.abort()},timeout);try{var r=await fetch(path,Object.assign({signal:ctrl.signal},opts||{}));clearTimeout(t);return r.json();}catch(e){clearTimeout(t);var msg=(e.name==='AbortError')?('请求超时 ('+(timeout/1000)+'s)'):(e.message||'网络错误');return{ok:false,error:msg};}}async function apiSlow(path,opts){return api(path,Object.assign({timeout:60000},opts||{}));}var _refreshBusy=false;async function refreshAll(){if(_refreshBusy)return;_refreshBusy=true;try{var _a=api('/api/accounts'),_s=api('/api/state');var a=await _a,s=await _s;accounts=a.accounts||[];state=s;renderSidebar();renderDashboard();if(curTab==='emails'){await refreshEmails();renderAliasTable();}if(curTab==='batch')renderBatchPanel();updateInboxAccountSelect();}finally{_refreshBusy=false;}}async function refreshLight(){if(_refreshBusy)return;var s=await api('/api/state');state=s;var sd=E('schedDot');var running=state.running;sd.className='status-dot '+(running?'online':'offline');E('schedLabel').textContent='调度器: '+(running?(state.creating?'创建中...':'等待下轮'):'已停止');E('btnSched').textContent=running?'停止调度器':'启动调度器';E('btnSched').className='btn btn-sm '+(running?'btn-danger':'btn-primary');}async function refreshEmails(){var d=await api('/api/emails');emails=d.emails||[];emails.forEach(function(e){var acc=accounts.find(function(a){return a.id===e.account_id});e.account_name=acc?(acc.name||acc.real_email||''):(e.account_id||'');e.account_email=acc?(acc.real_email||''):'';});E('emailCount').textContent=emails.length;updateEmailFilter();renderAliasTable();}async function refreshAliases(){var d=await api('/api/aliases');var apiAliases=d.aliases||[];if(apiAliases.length){var apiMap={};apiAliases.forEach(function(a){apiMap[a.email]=a;});emails.forEach(function(e){var apiData=apiMap[e.email];if(apiData){e.label=apiData.label||'';e.created_at=apiData.createdAt||e.created_at;e.active=apiData.active;e.anonymousId=apiData.anonymousId;e.account_name=apiData.account_name||e.account_name;e.account_email=apiData.account_email||e.account_email;}});}E('emailCount').textContent=emails.length;updateEmailFilter();renderAliasTable();}function renderSidebar(){var c=E('sidebarAccounts');if(!accounts.length){c.innerHTML='<div style="padding:8px 14px;font-size:11px;color:var(--ink-faint)">暂无账号</div>';}else{c.innerHTML=accounts.map(function(a,i){var cls=a.status==='active'?'active':'error';var nm=esc(a.name||'未命名');return'<div class="account-item" data-accid="'+escAttr(a.id)+'"><span class="acc-dot '+cls+'"></span><span class="acc-name" title="'+(escAttr(a.real_email)||'')+'">'+nm+'</span><span class="acc-del" title="删除" onclick="event.stopPropagation();removeAccount(\''+escAttr(a.id)+'\')">&times;</span></div>';}).join('');}var sd=E('schedDot');sd.className='status-dot '+(state.running?'online':'offline');var sm=state.running?(state.creating?'创建中...':'等待下轮'):'已停止';E('schedLabel').textContent='调度器: '+sm;var bs=E('btnSched');bs.textContent=state.running?'停止调度器':'启动调度器';bs.className='btn btn-sm '+(state.running?'btn-danger':'btn-primary');}function renderDashboard(){var summary={account_count:accounts.length,active_accounts:0,error_accounts:0,total_aliases:0,total_active_aliases:0};accounts.forEach(function(a){if(a.status==='active')summary.active_accounts++;else if(a.status==='error')summary.error_accounts++;summary.total_aliases+=(a.alias_total||0);summary.total_active_aliases+=(a.alias_active||0);});E('summaryCards').innerHTML='<div class="card"><div class="label">账号总数</div><div class="value blue">'+summary.account_count+'</div><div class="sub">活跃 '+summary.active_accounts+' / 异常 '+summary.error_accounts+'</div></div><div class="card"><div class="label">隐私邮箱总数</div><div class="value accent">'+summary.total_aliases+'</div><div class="sub">活跃 '+summary.total_active_aliases+'</div></div><div class="card"><div class="label">累计创建</div><div class="value">'+(state.total_created||0)+'</div><div class="sub">历史总计</div></div><div class="card"><div class="label">今日创建</div><div class="value green">'+(state.today_created||0)+'</div><div class="sub" id="schedInfo">'+esc(state.round_status||'--')+'</div></div>';if(!accounts.length){E('accCards').innerHTML='<div class="empty"><div class="icon"></div>还没有添加账号<br><span style="font-size:12px">点击右上角 "+ 添加账号" 开始</span></div>';}else{E('accCards').innerHTML=accounts.map(function(a){var stCls=a.status==='active'?'ok':'err';var stText=a.status==='active'?'正常':(a.last_error||'异常');var email=a.real_email||'?';return'<div class="acc-card"><div class="acc-header"><div><div class="acc-title">'+esc(a.name||'未命名')+'</div><div class="acc-email">'+esc(email)+'</div></div><span class="status-badge '+stCls+'">'+esc(stText.substring(0,20))+'</span></div><div class="acc-stats"><div class="acc-stat">别名: <span class="n">'+(a.alias_total||0)+'</span></div><div class="acc-stat">活跃: <span class="n" style="color:var(--green)">'+(a.alias_active||0)+'</span></div></div><div class="acc-actions"><button class="btn btn-outline btn-xs" onclick="createForAccount(\''+escAttr(a.id)+'\',1)">创建 1 个</button><button class="btn btn-outline btn-xs" onclick="createForAccount(\''+escAttr(a.id)+'\',5)">创建 5 个</button><button class="btn btn-outline btn-xs" onclick="validateAccount(\''+escAttr(a.id)+'\')">校验</button><button class="btn btn-outline btn-xs" onclick="refreshAccountCookie(\''+escAttr(a.id)+'\')">更新 Cookie</button></div></div>';}).join('');}}function updateEmailFilter(){var sel=E('aliasFilter'),old=sel.value;sel.innerHTML='<option value="all">全部账号 ('+emails.length+')</option>';var byAcc={};emails.forEach(function(e){var ak=e.account_id||'?';byAcc[ak]=(byAcc[ak]||0)+1;});Object.keys(byAcc).forEach(function(ak){var acc=accounts.find(function(x){return x.id===ak});var label=acc?(acc.name||acc.real_email||ak):ak;sel.innerHTML+='<option value="'+escAttr(ak)+'">'+esc(label)+' ('+byAcc[ak]+')</option>';});sel.value=old||'all';}function filteredEmailRecords(){var account=E('aliasFilter').value,type=E('addressTypeFilter').value;return emails.filter(function(e){return (account==='all'||e.account_id===account)&&(type==='all'||e.address_type===type)});}function renderAliasTable(){updateEmailFilter();var filtered=filteredEmailRecords();E('emailCount').textContent=filtered.length+' / '+emails.length;var pending=filtered.filter(function(e){return !e.inbox_ready}).length;E('btnRepairInbox').textContent='一键补齐收件地址（'+pending+'）';E('btnRepairInbox').disabled=!pending||_jobPolling;var c=E('aliasTableContainer');if(!filtered.length){c.innerHTML='<div class="empty"><div class="icon"></div>暂无创建记录 — 请先通过仪表盘或批量创建生成邮箱</div>';return;}var h='<table class="email-table"><thead><tr><th><input type="checkbox" id="emailPageSelect" aria-label="全选本页"></th><th>邮箱地址</th><th>类型 / 源地址</th><th title="累计成功生成的分裂地址数，删除子地址不扣减">分裂次数</th><th>所属账号</th>'+emailSortHeader('created_at','注册时间')+emailSortHeader('fastai_synced_at','同步时间')+'<th>状态</th><th></th><th>同步 FastAI</th><th>收件地址</th></tr></thead><tbody>';filtered.forEach(function(e,i){var accName=e.account_name||e.account_email||e.account_id||'--';var statusHtml=e.hasOwnProperty('active')?(e.active?'<span style="color:var(--green)">活跃</span>':'<span style="color:var(--red)">停用</span>'):'<span style="color:var(--ink-faint)">--</span>';statusHtml=e.inbox_ready?'<span style="color:var(--green)">'+(e.inbox_verified?'已实收':'收件已配置 · 待实测')+'</span>':'<span style="color:var(--red)" title="'+escAttr(e.setup_error||'可点击一键补齐收件地址')+'">待补全收件</span>'+(e.setup_error?'<small style="display:block;max-width:300px;color:var(--red)">'+esc(e.setup_error)+'</small>':'');if(e.local_deleted||e.deletion_error||e.setup_stage==='deleting'||e.setup_stage==='source_deleted'){statusHtml='<span style="color:var(--red)">'+(e.deletion_error?'删除失败':e.local_deleted?'待远端删除':e.setup_stage==='source_deleted'?'源邮箱已删除':'删除处理中')+'</span>'+(e.setup_error?'<small style="display:block;max-width:300px;color:var(--red)">'+esc(e.setup_error)+'</small>':'');}h+='<tr><td style="color:var(--ink-faint);width:40px">'+'<input type="checkbox" data-email-select value="'+escAttr(e.email)+'" aria-label="选择邮箱">'+'</td><td class="mono">'+esc(e.email||'')+'</td><td><span>'+esc(e.address_type_label||'源地址')+'</span>'+(e.address_type==='split'?'<small style="display:block;color:var(--ink-faint);font-size:10px">'+esc(e.source_email||'')+'</small>':'')+'</td><td class="mono">'+(e.address_type==='split'?'—':Number(e.split_count||0))+'</td><td style="font-size:11px">'+esc(accName)+'</td><td class="email-time">'+esc(emailTimeText(e.created_at))+'</td><td class="email-time">'+esc(emailTimeText(e.fastai_synced_at))+'</td><td>'+statusHtml+'</td><td style="width:40px"><button class="copy-btn" onclick="copyOne(\''+escAttr(e.email)+'\')" title="复制"></button></td><td>'+fastaiStatusHtml(e)+' </td><td class="inbox-address-cell">'+(e.inbox_ready&&e.inbox_url?'<button type="button" class="inbox-address" data-address="'+escAttr(e.inbox_url)+'" title="'+escAttr(e.inbox_url)+'" aria-label="复制收件地址" onclick="copyInboxAddress(this)">'+esc(shortInboxAddress(e.inbox_url))+'</button>':'<span style="color:var(--ink-faint)">待配置</span>')+'</td></tr>';});h+='</tbody></table>';c.innerHTML=h;}function emailTimeText(value){if(!value)return '—';var numeric=typeof value==='number'||/^\d+(\.\d+)?$/.test(String(value));var date=new Date(numeric?(Number(value)>100000000000?Number(value):Number(value)*1000):value);if(Number.isNaN(date.getTime()))return '—';return date.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});}function shortInboxAddress(address){return address.length>44?address.slice(0,30)+'…'+address.slice(-12):address;}async function copyInboxAddress(button){try{await navigator.clipboard.writeText(button.dataset.address);toast('已复制收件地址');}catch(error){toast('复制失败，请重试',true);}}function renderBatchPanel(){var activeAccs=accounts.filter(function(a){return a.status==='active'});E('batchAccCount').textContent=activeAccs.length+' 个可用账号';var g=E('batchChkGroup');var previous=Array.from(g.querySelectorAll('input'));var chosen=previous.filter(function(input){return input.checked}).map(function(input){return input.value});if(!activeAccs.length){g.innerHTML='<span style="font-size:12px;color:var(--ink-faint)">没有活跃账号，请先添加</span>';E('btnBatchExec').disabled=true;}else{g.innerHTML=activeAccs.map(function(a){var email=a.real_email||a.name||a.id;return'<label class="chk-item"><input type="checkbox" value="'+escAttr(a.id)+'" '+(!previous.length||chosen.indexOf(a.id)>=0?'checked':'')+'> <span title="'+escAttr(email)+'">'+esc(a.name||email.substring(0,20))+'</span></label>';}).join('');E('btnBatchExec').disabled=_jobPolling;}}async function execBatchCreate(){var ids=Array.from(document.querySelectorAll('#batchChkGroup input:checked')).map(function(c){return c.value});await runCreationJob(ids,Number(E('batchCount').value),E('batchLabel').value.trim());}var _inboxBusy=false;var _inboxSse=null;var _inboxStreamMsgs=[];function refreshInbox(force){if(_inboxBusy)return;var accId=E('inboxAccount').value;if(!accId){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>请先选择账号</div>';return}if(force){_inboxBusy=true;var limit=parseInt(E('inboxLimit').value)||20;E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>强制刷新中...</div>';apiSlow('/api/accounts/'+encodeURIComponent(accId)+'/inbox?limit='+limit+'&force=1').then(function(d){_inboxBusy=false;if(d.error){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error||'连接失败')+'</div>';return;}renderInboxMsgs(d.emails||[],'收件箱 ('+(d.count||0)+' 封)');updateCacheStatus(d.cached);});return;}startInboxStream(accId);}function startInboxStream(accId){if(_inboxSse){_inboxSse.close();_inboxSse=null}_inboxBusy=true;_inboxStreamMsgs=[];E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>正在逐条拉取邮件...</div>';var limit=parseInt(E('inboxLimit').value)||20;_inboxSse=new EventSource('/api/accounts/'+encodeURIComponent(accId)+'/inbox-stream?limit='+limit);_inboxSse.onmessage=function(e){try{var d=JSON.parse(e.data);if(d.type==='start'){}else if(d.type==='email'){_inboxStreamMsgs.push(d.email);renderInboxMsgs(_inboxStreamMsgs,'收件箱 ('+d.count+' 封, 加载中...)');}else if(d.type==='done'){_inboxSse.close();_inboxSse=null;_inboxBusy=false;renderInboxMsgs(_inboxStreamMsgs,'收件箱 ('+d.count+' 封)');}else if(d.type==='error'){_inboxSse.close();_inboxSse=null;_inboxBusy=false;E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error||'连接失败')+'</div>';}}catch(_){}};_inboxSse.onerror=function(){if(_inboxSse){_inboxSse.close();_inboxSse=null;}_inboxBusy=false;if(_inboxStreamMsgs.length){renderInboxMsgs(_inboxStreamMsgs,'收件箱 ('+_inboxStreamMsgs.length+' 封, 连接中断)');}else{E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>连接失败</div>';}};}async function searchAliasMail(){if(_inboxBusy)return;_inboxBusy=true;try{var accId=E('inboxAccount').value;var alias=E('aliasSearchInput').value.trim();if(!accId){toast('请先选择账号',true);return}if(!alias){toast('请输入隐私邮箱地址',true);return}E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>查询 '+esc(alias)+' ...</div>';var d=await apiSlow('/api/accounts/'+encodeURIComponent(accId)+'/mail/'+encodeURIComponent(alias)+'?limit=30');if(d.error){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error)+'</div>';return;}renderInboxMsgs(d.emails||[],esc(alias)+' ('+(d.count||0)+' 封)');}finally{_inboxBusy=false;}}async function checkAliasMail(){if(_inboxBusy)return;_inboxBusy=true;try{var accId=E('inboxAccount').value;if(!accId){_inboxBusy=false;E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>请先选择账号</div>';return}E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>正在检查各别名的收件...</div>';var d=await apiSlow('/api/accounts/'+encodeURIComponent(accId)+'/alias-mail');if(d.error){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error||'查询失败')+'</div>';return;}var byAlias=d.by_alias||{};var total=0;var aliasKeys=Object.keys(byAlias);if(!aliasKeys.length){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>所有隐私邮箱暂无收件</div>';return;}var h='';aliasKeys.forEach(function(alias){var msgs=byAlias[alias]||[];total+=msgs.length;h+='<div style="padding:8px 14px;border-bottom:1px solid var(--rule);font-weight:600;font-size:13px;background:var(--paper-dim)">'+esc(alias)+' ('+msgs.length+' 封)</div>';msgs.forEach(function(m){h+='<div style="padding:6px 20px;border-bottom:1px solid var(--rule);font-size:12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px"><span><strong>'+esc(m.subject||'(无主题)')+'</strong></span><span style="color:var(--ink-soft)">'+esc(m.from||'').substring(0,30)+'</span><span style="color:var(--ink-faint);font-size:11px">'+(m.date||'').substring(0,19)+'</span></div>';});});E('inboxMsgs').innerHTML='<div style="font-size:11px;color:var(--ink-faint);padding:8px 14px;border-bottom:1px solid var(--rule)">共 '+aliasKeys.length+' 个别名收到 '+total+' 封邮件</div>'+h;}finally{_inboxBusy=false;}}function renderInboxMsgs(msgs,title){if(!msgs.length){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>收件箱为空</div>';return;}var h='<div style="font-size:11px;color:var(--ink-faint);padding:8px 16px;border-bottom:1px solid var(--rule)">'+esc(title)+'</div>';msgs.forEach(function(m,i){var mid=m.id||'m'+i;h+='<div class="email-item" style="border-bottom:1px solid var(--rule);cursor:pointer" onclick="toggleEmail(\''+escAttr(mid)+'\',\''+escAttr(m.id||'')+'\')"><div style="padding:12px 16px;display:flex;justify-content:space-between;align-items:flex-start;gap:12px"><div style="flex:1;min-width:0"><div style="font-weight:600;font-size:14px;margin-bottom:4px">'+esc(m.subject||'(无主题)')+'</div><div style="font-size:12px;color:var(--ink-soft)">'+esc(m.from||'')+'</div><div style="font-size:11px;color:var(--ink-faint);margin-top:2px">To: '+esc((m.to||'').substring(0,50))+'</div></div><div style="font-size:11px;color:var(--ink-faint);white-space:nowrap;text-align:right">'+(m.date||'').substring(0,19)+'</div></div><div id="'+escAttr(mid)+'_body" style="display:none;padding:0 16px 16px;font-size:13px;line-height:1.7;color:var(--ink-soft);white-space:pre-wrap;word-break:break-word;max-height:400px;overflow-y:auto;border-top:1px solid var(--rule)"></div></div>';});E('inboxMsgs').innerHTML=h;}var _expandedEmail=null;async function toggleEmail(domId,msgId){var bodyEl=E(domId+'_body');if(!bodyEl)return;if(_expandedEmail&&_expandedEmail!==domId){var prev=E(_expandedEmail+'_body');if(prev)prev.style.display='none';}if(bodyEl.style.display==='block'){bodyEl.style.display='none';_expandedEmail=null;return;}bodyEl.style.display='block';_expandedEmail=domId;if(bodyEl.textContent.trim()&&bodyEl.textContent!=='加载中...')return;bodyEl.textContent='加载中...';if(!msgId){bodyEl.textContent='(无法获取邮件正文)';return;}var accId=E('inboxAccount').value;if(!accId){bodyEl.textContent='(请先选择账号)';return;}var d=await apiSlow('/api/accounts/'+encodeURIComponent(accId)+'/message/'+encodeURIComponent(msgId));if(!d.ok||!d.message){bodyEl.textContent='(获取失败: '+(d.error||'未知')+')';return;}bodyEl.textContent=d.message.body||'(无正文内容)';}function updateCacheStatus(cached){if(!cached)return;var age=cached.cache_age_sec||0;var txt=age<300?'缓存 '+(age<60?Math.round(age)+'s':Math.round(age/60)+'m')+' 前':'';E('cacheStatus').textContent=cached.inbox_cached?' | '+cached.inbox_cached+' 封已缓存 '+txt:'';}function openInboxSettings(){var accId=E('inboxAccount').value;if(!accId){toast('请先选择账号',true);return}showAppPwdModal(accId);}function showAppPwdModal(accId){var acc=accounts.find(function(a){return a.id===accId});var name=acc?(acc.name||acc.real_email||accId):accId;var icloudEmail='';if(acc&&acc.icloud_email&&(acc.icloud_email.indexOf('@icloud.com')>=0||acc.icloud_email.indexOf('@me.com')>=0||acc.icloud_email.indexOf('@mac.com')>=0)){icloudEmail=acc.icloud_email;}else if(acc&&acc.real_email&&(acc.real_email.indexOf('@icloud.com')>=0||acc.real_email.indexOf('@me.com')>=0)){icloudEmail=acc.real_email;}var hasPwd=acc&&acc.has_app_password;var h='<div class="modal-overlay" id="appPwdModal" onclick="if(event.target===this)closeAppPwdModal()"><div class="modal-box"><h3><i class="diamond"></i> '+(hasPwd?'修改':'设置')+' iCloud 邮箱和应用密码</h3><p>账号: <b>'+esc(name)+'</b> (Apple ID: '+esc(acc?acc.real_email:'')+')<br>在 <a href="appleid.apple.com">appleid.apple.com</a> → 登录与安全 → App 专用密码 生成。</p><label style="font-family:var(--mono);font-size:11px;color:var(--ink-faint);letter-spacing:.2em;text-transform:uppercase">iCloud 邮箱 (IMAP 登录用)</label><input type="text" id="icloudEmailInput" value="'+escAttr(icloudEmail)+'" placeholder="xxx@icloud.com"><label style="font-family:var(--mono);font-size:11px;color:var(--ink-faint);letter-spacing:.2em;text-transform:uppercase">App 专用密码'+ (hasPwd?' (重新输入以更新)':'') +'</label><input type="password" id="appPwdInput" placeholder="xxxx-xxxx-xxxx-xxxx"><div class="modal-actions"><button class="btn btn-outline" onclick="closeAppPwdModal()">取消</button><button class="btn btn-primary" id="btnSetPwd" onclick="setAppPassword(\''+escAttr(accId)+'\')">保存并测试</button></div><div class="modal-msg" id="appPwdMsg"></div></div></div>';document.body.insertAdjacentHTML('beforeend',h);}function closeAppPwdModal(){var m=E('appPwdModal');if(m)m.remove()}async function setAppPassword(accId){var pwd=E('appPwdInput').value.trim();var email=E('icloudEmailInput').value.trim();if(!email){E('appPwdMsg').innerHTML='<span style="color:var(--red)">请输入 iCloud 邮箱</span>';return}if(!pwd){E('appPwdMsg').innerHTML='<span style="color:var(--red)">请输入密码</span>';return}var btn=E('btnSetPwd');btn.disabled=true;btn.textContent='测试中...';var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/app-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({app_password:pwd,icloud_email:email})});btn.disabled=false;btn.textContent='保存并测试';if(d.ok){E('appPwdMsg').innerHTML='<span style="color:var(--green)">连接成功! 收件箱 '+d.inbox_count+' 封</span>';var acc=accounts.find(function(a){return a.id===accId});if(acc){acc.has_app_password=true;acc.icloud_email=email;}setTimeout(closeAppPwdModal,1500);updateInboxAccountSelect();}else{E('appPwdMsg').innerHTML='<span style="color:var(--red)">'+esc(d.error||'连接失败')+'</span>';}}async function createForAccount(accId,count){await runCreationJob([accId],count,'');}async function validateAccount(accId){var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/validate',{method:'POST'});if(d.ok)toast('校验通过: '+d.real_email);else toast('校验失败: '+(d.error||'?'),true);refreshAll();}async function removeAccount(accId){if(!confirm('确认删除该账号？'))return;var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/remove',{method:'POST'});if(d.ok)toast('已删除');refreshAll();}async function toggleScheduler(){var act=state.running?'stop':'start';var d=await api('/api/scheduler/'+act,{method:'POST'});if(d.ok)toast(state.running?'调度器已停止':'调度器已启动');refreshAll();}function copyOne(email){var record=emails.find(function(e){return e.email===email});var text=record&&record.inbox_ready?email+'----'+record.inbox_url:email;navigator.clipboard.writeText(text).then(function(){toast(record&&record.inbox_ready?'已复制邮箱+收件链接':'已复制邮箱，收件配置尚未完成')});}function copyAll(){var filter=E('aliasFilter').value;var filtered=filter==='all'?emails:emails.filter(function(e){return e.account_id===filter});navigator.clipboard.writeText(filtered.map(function(e){return e.email}).join('\n')).then(function(){toast('已复制 '+filtered.length+' 个')});}async function copyInboxLinks(){var selected=filteredEmailRecords();var lines=selected.filter(function(e){return e.inbox_ready&&e.inbox_url}).map(function(e){return e.email+'----'+e.inbox_url});if(!lines.length){toast('暂无已配置完成的收件链接',true);return}try{await navigator.clipboard.writeText(lines.join('\n'));toast('已复制 '+lines.length+' 条邮箱+收件链接')}catch(err){toast('复制失败，请使用导出收件链接',true)}}function exportCSV(){var selected=filteredEmailRecords();var columns=[['邮箱地址','类型','源地址','所属账号','收件链接']].concat(selected.map(function(e){return [e.email,e.address_type_label,e.source_email,e.account_name||e.account_id||'',e.inbox_url||'']}));var csv=columns.map(function(row){return row.map(function(value){return '"'+String(value||'').replace(/"/g,'""')+'"'}).join(',')}).join('\n');var url=URL.createObjectURL(new Blob(['\uFEFF'+csv],{type:'text/csv'}));var a=document.createElement('a');a.href=url;a.download='icloud_aliases.csv';a.click();setTimeout(function(){URL.revokeObjectURL(url)},1000);}function clearLogs(){logs=[];E('logFeed').innerHTML=''}function toast(msg,isErr){var t=E('toast');t.textContent=msg;t.style.background=isErr?'var(--red)':'var(--ink)';t.style.color='var(--paper)';t.classList.add('show');setTimeout(function(){t.classList.remove('show')},2200);}function connectSSE(){if(sseConn){sseConn.close();sseConn=null}sseConn=new EventSource('/api/log-stream');sseConn.onmessage=function(e){try{var entry=JSON.parse(e.data);logs.push(entry);if(logs.length>500)logs=logs.slice(-500);if(curTab==='logs')renderLogs();if(entry.msg&&entry.msg.indexOf('创建')>=0)refreshLight();}catch(_){}};sseConn.onerror=function(){sseConn.close();sseConn=null;setTimeout(connectSSE,5000)};}function renderLogs(){var f=E('logFeed');f.innerHTML=logs.map(function(l){return'<div class="log-line '+l.level+'"><span class="log-time">'+esc(l.time)+'</span>'+esc(l.msg)+'</div>';}).join('\n');f.scrollTop=f.scrollHeight;}function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}function escAttr(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}function showAddAccountModal(){var h='<div class="modal-overlay" id="addAccModal" onclick="if(event.target===this)closeAddAccModal()"><div class="modal-box"><h3><i class="diamond"></i> 导入 iCloud Cookie</h3><p>Chrome 安装 <b>Cookie Editor</b> 扩展 → 登录 icloud.com → 导出 <b>Header String</b> 粘贴即可。<br>也支持 JSON 格式: <code>{"name1":"value1"}</code></p><input type="text" id="accNameInput" placeholder="账号名称 (如: 主号)"><textarea id="cookieInput" placeholder="粘贴 Cookie，支持 Header String 或 JSON 格式"></textarea><div class="modal-actions"><button class="btn btn-outline" onclick="closeAddAccModal()">取消</button><button class="btn btn-primary" id="btnAddAccount" onclick="addAccount()">添加并校验</button></div><div class="modal-msg" id="addAccMsg"></div></div></div>';document.body.insertAdjacentHTML('beforeend',h);}var cookieRefreshAccountId=null;function refreshAccountCookie(id){closeAddAccModal();showAddAccountModal();cookieRefreshAccountId=id;E('accNameInput').style.display='none';E('addAccModal').querySelector('h3').textContent='更新账号 Cookie';E('btnAddAccount').textContent='更新并校验';}function closeAddAccModal(){var m=E('addAccModal');if(m)m.remove();cookieRefreshAccountId=null;}async function addAccount(){var name=E('accNameInput').value.trim()||'未命名账号';var cookies=E('cookieInput').value.trim();if(!cookies){E('addAccMsg').innerHTML='<span style="color:var(--red)">请粘贴 Cookie</span>';return}var btn=E('btnAddAccount');btn.disabled=true;btn.textContent='校验中...';var replacing=!!cookieRefreshAccountId;var d=await api(replacing?'/api/accounts/'+encodeURIComponent(cookieRefreshAccountId)+'/cookies':'/api/accounts/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,cookie_input:cookies})});btn.disabled=false;btn.textContent=replacing?'更新并校验':'添加并校验';if(d.ok){E('addAccMsg').innerHTML='<span style="color:var(--green)">会话已保存: '+esc(d.real_email||'')+' ('+(d.alias_total||0)+' 别名)</span>';setTimeout(closeAddAccModal,1500);refreshAll();}else{E('addAccMsg').innerHTML='<span style="color:var(--red)">'+esc(d.error||'失败')+'</span>';}}function renderDocs(){var h='<div style="max-width:900px"><p style="color:var(--ink-soft);margin-bottom:18px">所有接口返回 JSON。Base URL: <code>http://127.0.0.1:2224</code></p>';var sections=[{title:'账号管理',items:[{method:'GET',path:'/api/accounts',desc:'列出所有账号（脱敏，不含 cookie）'},{method:'POST',path:'/api/accounts/add',desc:'添加账号',body:'{"name":"账号名","cookie_input":"name1=value1; name2=value2"}'},{method:'POST',path:'/api/accounts/{id}/remove',desc:'删除账号'},{method:'POST',path:'/api/accounts/{id}/validate',desc:'重新校验账号会话'}]},{title:'状态',items:[{method:'GET',path:'/api/state',desc:'全局状态 + 账号汇总'}]},{title:'别名 / 邮箱',items:[{method:'GET',path:'/api/aliases',desc:'所有账号的别名列表（iCloud API 实时拉取）'},{method:'GET',path:'/api/emails',desc:'本地创建记录（latest_emails.txt，永远可用）'},{method:'POST',path:'/api/accounts/{id}/create',desc:'为指定账号创建别名',body:'{"count":5,"label":"可选标签"}'},{method:'POST',path:'/api/create-jobs',desc:'后台一键创建别名、独立收件箱和转发规则，返回 job_id',body:'{"account_ids":["id1"],"count_per_account":2}'},{method:'GET',path:'/api/create-jobs/{job_id}',desc:'查看进度与纯文本 output，每行邮箱----收件链接'},{method:'GET',path:'/api/create-jobs/{job_id}/export',desc:'导出本批成功记录为 TXT'},{method:'GET',path:'/api/inbox-links/export',desc:'导出全部已配置记录；支持 account_id 筛选'},{method:'POST',path:'/api/create-batch',desc:'跨账号批量创建',body:'{"account_ids":["id1","id2"],"count_per_account":5}'}]},{title:'收件箱 (IMAP)',items:[{method:'GET',path:'/api/accounts/{id}/inbox?limit=20&force=1',desc:'查收件箱。force=1 跳过缓存强制从 IMAP 拉取'},{method:'GET',path:'/api/accounts/{id}/alias-mail?force=1',desc:'查所有隐私别名的收件情况'},{method:'GET',path:'/api/accounts/{id}/mail/{别名邮箱}',desc:'查指定隐私邮箱的收件'},{method:'POST',path:'/api/accounts/{id}/app-password',desc:'设置 App 专用密码并测试 IMAP',body:'{"app_password":"xxxx-xxxx-xxxx-xxxx","icloud_email":"xxx@icloud.com"}'}]},{title:'快捷入口',items:[{method:'GET',path:'/api/mail?email=user@icloud.com',desc:'按主邮箱查所有别名收件'},{method:'GET',path:'/api/mail?email=...&alias=xxx@icloud.com',desc:'按主邮箱查指定别名收件'}]},{title:'调度器',items:[{method:'POST',path:'/api/scheduler/start',desc:'启动定时调度器'},{method:'POST',path:'/api/scheduler/stop',desc:'停止调度器'}]},{title:'实时日志',items:[{method:'GET',path:'/api/log-stream',desc:'SSE 实时日志流（EventSource）'}]}];sections.forEach(function(sec){h+='<div style="margin-bottom:24px"><div style="font-size:12px;color:var(--ink-faint);letter-spacing:.2em;text-transform:uppercase;margin-bottom:10px;border-bottom:1px solid var(--rule);padding-bottom:4px">'+esc(sec.title)+'</div>';sec.items.forEach(function(item){var methodColor=item.method==='GET'?'var(--green)':item.method==='POST'?'var(--red)':'var(--ink-soft)';h+='<div style="margin-bottom:10px;padding:10px 14px;background:var(--paper-dim)"><span style="font-weight:700;color:'+methodColor+';margin-right:12px;font-size:11px">'+item.method+'</span><code style="font-size:12px">'+esc(item.path)+'</code><div style="color:var(--ink-soft);font-size:12px;margin-top:4px">'+esc(item.desc)+'</div>';if(item.body){h+='<div style="margin-top:6px"><code style="font-size:11px;color:var(--ink-faint);background:var(--paper);padding:3px 8px;display:inline-block">'+esc(item.body)+'</code></div>';}h+='</div>';});h+='</div>';});h+='<div style="margin-top:32px;padding-top:16px;border-top:1px solid var(--rule-strong);font-size:12px;color:var(--ink-faint)">缓存策略：收件箱接口默认 5 分钟内读本地缓存 (<code>icloud_hme.sqlite3</code>)，首次拉取后终身存储。传 <code>?force=1</code> 跳过缓存从 IMAP 增量拉取。<br>Cookie 导入：支持 Header String (<code>name=value; ...</code>) 和 JSON (<code>{"name":"value"}</code>) 两种格式。</div></div>';E('docsContent').innerHTML=h;}function updateInboxAccountSelect(){var sel=E('inboxAccount'),old=sel.value;sel.innerHTML='<option value="">-- 选择账号 --</option>';accounts.forEach(function(a){var hasPwd=a.has_app_password?' [已设]':' [未设密码]';var imapEmail=a.icloud_email||a.real_email||'';sel.innerHTML+='<option value="'+escAttr(a.id)+'">'+esc((a.name||a.real_email||a.id).substring(0,20))+' | '+esc(imapEmail.substring(0,25))+' '+hasPwd+'</option>';});sel.value=old||'';}var _jobPolling=false;
function exportInboxLinks(){var params=new URLSearchParams();if(E('emailKeyword'))params.set('q',E('emailKeyword').value.trim());if(E('aliasFilter').value!=='all')params.set('account_id',E('aliasFilter').value);params.set('address_type',E('addressTypeFilter').value);var a=document.createElement('a');a.href='/api/inbox-links/export?'+params.toString();a.click();}
async function copyBatchOutput(){try{await navigator.clipboard.writeText(E('batchOutput').value);toast('已复制本批文本')}catch(error){toast('请选中文本框内容复制',true)}}
function downloadBatchOutput(){var text=E('batchOutput').value;if(!text){toast('本批尚无配置完成的邮箱',true);return}var url=URL.createObjectURL(new Blob([text.trimEnd()+'\n'],{type:'text/plain;charset=utf-8'}));var a=document.createElement('a');a.href=url;a.download='icloud_inbox_links.txt';a.click();setTimeout(function(){URL.revokeObjectURL(url)},1000);}
async function repairInboxLinks(){if(_jobPolling){toast('已有任务运行中',true);return}var btn=E('btnRepairInbox');btn.disabled=true;try{var d=await api('/api/inbox-links/repair',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_id:E('aliasFilter').value,address_type:E('addressTypeFilter').value})});if(!d.job_id){toast(d.error||'没有待补齐的收件地址',!!d.error);return}sessionStorage.setItem('creationJob',d.job_id);document.querySelector('[data-tab="batch"]').click();await pollCreationJob(d.job_id);}finally{renderAliasTable();}}

async function runCreationJob(ids,count,label,mode){if(_jobPolling){toast('已有任务运行中',true);return}if(!ids.length){toast('请选择账号',true);return}if(!Number.isInteger(count)||count<1||count>20){toast('每账号数量必须为 1 到 20',true);return}var d=await api('/api/create-jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_ids:ids,count_per_account:count,label:label,mode:mode||'create'})});if(!d.job_id){toast(d.error||'无法开始任务',true);return}sessionStorage.setItem('creationJob',d.job_id);document.querySelector('[data-tab="batch"]').click();await pollCreationJob(d.job_id);}
async function pollCreationJob(id){if(_jobPolling)return;_jobPolling=true;var failures=0;try{while(true){var d=await api('/api/create-jobs/'+encodeURIComponent(id));if(!d.status){if(++failures>=5){E('batchProgress').textContent=d.error||'连接中断，请刷新后继续查看';return}await new Promise(function(resolve){setTimeout(resolve,3000)});continue}failures=0;E('batchProgress').textContent=d.progress||d.error||d.status;E('batchOutput').value=(d.lines||[]).join('\n');var errors=[];Object.values(d.results||{}).forEach(function(rows){rows.filter(function(r){return !r.ok}).forEach(function(r){errors.push((r.email||r.account_id||'')+'：'+r.error)})});E('batchErrors').textContent=errors.join('\n');E('btnBatchExec').disabled=d.status==='running';if(d.status!=='running'){sessionStorage.removeItem('creationJob');toast('配置完成 '+(d.total_created||0)+' 个，失败 '+(d.total_errors||0)+' 个',d.total_errors>0);return}await new Promise(function(resolve){setTimeout(resolve,1000)});}}finally{_jobPolling=false;E('btnBatchExec').disabled=false;refreshAll();}}
var _resumeJob=sessionStorage.getItem('creationJob');if(_resumeJob){document.querySelector('[data-tab="batch"]').click();pollCreationJob(_resumeJob);}
window.addEventListener('message',async function(event){var frame=E('autoFrame'),data=event.data;if(!frame||event.source!==frame.contentWindow&&event.source!==E('autoPlusFrame').contentWindow||event.origin!==location.origin||!data||data.type!=='icloud-copy-results'||typeof data.text!=='string'||data.text.length>2000000)return;try{await navigator.clipboard.writeText(data.text);event.source.postMessage({type:'icloud-copy-result',id:data.id,ok:true},event.origin)}catch(error){event.source.postMessage({type:'icloud-copy-result',id:data.id,ok:false},event.origin)}});if(location.pathname==='/fastai-sync')document.querySelector('[data-tab="fastai"]').click();if(location.pathname==='/auto-plus')document.querySelector('[data-tab="autoplus"]').click();if(location.pathname==='/auto')document.querySelector('[data-tab="auto"]').click();if(location.pathname==='/plus')document.querySelector('[data-tab="plus"]').click();refreshAll();connectSSE();setInterval(refreshLight,10000);setInterval(refreshAll,30000);</script><script src="/static/email_list.js?v=20261027-count"></script></body></html>"""

# ----- Flask Routes -----

@app.route("/plus")
def plus_page():
    return render_template_string(UI_HTML)


@app.route("/plus-panel")
def plus_panel():
    return app.send_static_file("plus_addressing.html")


@app.route("/api/plus-addresses")
def api_plus_addresses():
    response = jsonify(candidates=_plus_jobs.candidates(),
                       accounts=[{"id": a["id"], "name": a.get("name") or a["id"],
                                  "status": a.get("status")} for a in _account_mgr.list_accounts()])
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/plus-jobs", methods=["POST"])
def api_start_plus_job():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="请求必须是 JSON 对象"), 400
    try:
        job_id = _plus_jobs.start(data.get("sender_id"), data.get("bases"), data.get("tags"),
                                  data.get("timeout", 120))
        return jsonify(job_id=job_id), 202
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except RuntimeError as error:
        return jsonify(error=str(error)), 409


@app.route("/api/plus-jobs/<job_id>")
def api_plus_job(job_id):
    job = _plus_jobs.get(job_id)
    return (jsonify(job), 200, {"Cache-Control": "no-store"}) if job else (jsonify(error="任务不存在"), 404)


@app.route("/api/plus-jobs/<job_id>/cancel", methods=["POST"])
def api_cancel_plus_job(job_id):
    return jsonify(ok=_plus_jobs.cancel(job_id))


@app.route("/api/plus-jobs/<job_id>/export")
def api_export_plus_job(job_id):
    job = _plus_jobs.get(job_id)
    return _text_export(job.get("lines", [])) if job else (jsonify(error="任务不存在"), 404)

@app.route("/")
@app.route("/index.html")
def index(): return render_template_string(UI_HTML)

@app.route("/fastai-sync")
@app.route("/auto")
@app.route("/auto-plus")
def auto_registration_page():
    return render_template_string(UI_HTML)

@app.route("/fastai-sync-panel")
def fastai_sync_panel():
    return app.send_static_file("fastai_sync_panel.html")

@app.route("/auto-panel")
def auto_registration_panel():
    return app.send_static_file("auto_registration.html")

@app.route("/auto-plus-panel")
def auto_plus_panel():
    return app.send_static_file("auto_plus.html")


@app.route("/api/auto-plus/sources")
def api_auto_plus_sources():
    return jsonify(sources=[{"email": email, "account_id": row["account_id"]}
                            for email, row in _plus_creation_jobs.sources().items()])


@app.route("/api/auto-plus", methods=["GET", "POST"])
def api_auto_plus():
    if request.method == "GET":
        return jsonify(ok=True, **_auto_plus.snapshot())
    try:
        return jsonify(ok=True, **_auto_plus.configure(request.get_json(silent=True)))
    except ValueError as error:
        return jsonify(ok=False, error=str(error)), 400


@app.route("/api/auto-plus/run", methods=["POST"])
def api_auto_plus_run():
    try:
        data = request.get_json(silent=True)
        if data is not None and not isinstance(data, dict):
            raise ValueError("任务配置必须是对象")
        return jsonify(ok=True, job_id=_auto_plus.run_now(data)), 202
    except (ValueError, RuntimeError) as error:
        return jsonify(ok=False, error=str(error)), 409


@app.route("/api/auto-plus/tasks/<job_id>")
def api_auto_plus_task(job_id):
    try:
        return jsonify(ok=True, task=_auto_plus.get_task(job_id))
    except KeyError:
        return jsonify(ok=False, error="任务不存在"), 404


@app.route("/api/auto-plus/tasks/<job_id>/export")
def api_auto_plus_export(job_id):
    try:
        return _text_export(_auto_plus.download_task(job_id)["lines"])
    except KeyError:
        return jsonify(ok=False, error="任务不存在"), 404
    except ValueError as error:
        return jsonify(ok=False, error=str(error)), 409

@app.after_request
def auto_registration_no_cache(response):
    if request.path.startswith(("/api/auto-registration", "/api/fastai-sync", "/api/auto-plus")):
        response.headers["Cache-Control"] = "no-store"
    return response

@app.route("/api/auto-registration", methods=["GET", "POST"])
def api_auto_registration():
    if request.method == "GET":
        return jsonify(ok=True, **_auto_registration.snapshot())
    try:
        data = request.get_json(silent=True)
        snapshot = _auto_registration.configure(data)
        return jsonify(ok=True, **snapshot)
    except ValueError as error:
        return jsonify(ok=False, error=str(error)), 400

@app.route("/api/auto-registration/run", methods=["POST"])
def api_auto_registration_run():
    try:
        data = request.get_json(silent=True)
        if data is not None and not isinstance(data, dict):
            raise ValueError("任务配置必须是对象")
        job_id = _auto_registration.run_now(data)
        return jsonify(ok=True, job_id=job_id), 202
    except (ValueError, RuntimeError) as error:
        return jsonify(ok=False, error=str(error)), 409

@app.route("/api/auto-registration/export")
def api_auto_registration_export():
    snapshot = _auto_registration.snapshot()
    job = snapshot["current"] or next(iter(snapshot["history"]), {})
    if not job: return _text_export([])
    return api_auto_registration_task_export(job["job_id"])

@app.route("/api/auto-registration/tasks/<job_id>")
def api_auto_registration_task(job_id):
    try:
        return jsonify(ok=True, task=_auto_registration.get_task(job_id))
    except KeyError:
        return jsonify(ok=False, error="任务不存在或已超出保留范围"), 404

@app.route("/api/auto-registration/tasks/<job_id>/export")
def api_auto_registration_task_export(job_id):
    try:
        task = _auto_registration.download_task(job_id)
        response = _text_export(task["lines"])
        response.headers["Content-Disposition"] = f'attachment; filename="icloud_{job_id[:8]}.txt"'
        return response
    except KeyError:
        return jsonify(ok=False, error="任务不存在或已超出保留范围"), 404
    except ValueError as error:
        return jsonify(ok=False, error=str(error)), 409

@app.route("/api/state")
def api_state():
    summary = _account_mgr.get_summary()
    with _lock:
        state = dict(_global_state); state.update(summary)
        state["creating"] = state["creating"] or _creation_jobs.active or _registration_jobs.active
        state["cookies_ok"] = summary["active_accounts"] > 0
        state["alias_count"] = summary["total_aliases"]
        state["alias_active"] = summary["total_active_aliases"]
    auto = _auto_registration.snapshot()
    state.update(running=auto["config"]["enabled"] or bool(auto["current"]),
                 next_trigger=auto["next_run"], round_status="创建中" if auto["current"] else "等待下轮" if auto["config"]["enabled"] else "已停止")
    return jsonify(state)

@app.route("/api/accounts")
def api_accounts():
    accounts = _account_mgr.list_accounts()
    safe = []
    for a in accounts:
        ac = {k:v for k,v in a.items() if k not in ("cookies", "app_password")}
        ac["has_cookies"] = bool(a.get("cookies"))
        ac["has_app_password"] = bool(a.get("app_password"))
        safe.append(ac)
    return jsonify({"accounts":safe,"count":len(safe)})

@app.route("/api/accounts/add", methods=["POST"])
def api_add_account():
    data = request.get_json() or {}
    name = data.get("name","未命名账号")
    cookie_input = data.get("cookie_input","")
    if not cookie_input: return jsonify({"ok":False,"error":"请提供 cookie_input"})
    try:
        account = _account_mgr.add_account(name, cookie_input)
        _emit_log("info",f"添加账号: {account.get('name','')} ({account.get('real_email','?')})")
        return jsonify({"ok":True,"id":account["id"],"name":account["name"],"real_email":account.get("real_email",""),"alias_total":account.get("alias_total",0),"alias_active":account.get("alias_active",0),"status":account.get("status","")})
    except ValueError as e: return jsonify({"ok":False,"error":str(e)})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts/<acc_id>/remove", methods=["POST"])
def api_remove_account(acc_id):
    ok = _account_mgr.remove_account(acc_id)
    return jsonify({"ok":ok})

@app.route("/api/accounts/<acc_id>/validate", methods=["POST"])
def api_validate_account(acc_id):
    try:
        account = _account_mgr.validate_account(acc_id)
        if account.get("status") != "active":
            return jsonify(ok=False, error=account.get("last_error") or "会话校验失败"), 400
        return jsonify({"ok":True,"real_email":account.get("real_email",""),"alias_total":account.get("alias_total",0)})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})


@app.route('/api/accounts/<acc_id>/cookies', methods=['POST'])
def api_replace_account_cookies(acc_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('cookie_input'), str):
        return jsonify(ok=False, error='请提供 Cookie 文本'), 400
    if not data['cookie_input'].strip() or len(data['cookie_input']) > 65536:
        return jsonify(ok=False, error='Cookie 文本为空或过长'), 400
    try:
        account = _account_mgr.replace_session(acc_id, data['cookie_input'])
        return jsonify(ok=True, real_email=account['real_email'], status=account['status'],
                       alias_total=account.get('alias_total', 0))
    except KeyError:
        return jsonify(ok=False, error='账号不存在'), 404
    except ValueError:
        return jsonify(ok=False, error='Cookie 格式错误或所属账号不匹配，未修改原账号'), 400
    except ICloudMailSessionExpired as error:
        return jsonify(ok=False, error=str(error)), 400
    except Exception:
        return jsonify(ok=False, error='新会话校验失败，请重新登录 iCloud 后导出 Cookie'), 400

@app.route("/api/accounts/<acc_id>/create", methods=["POST"])
def api_create_for_account(acc_id):
    data = request.get_json() or {}
    count = data.get("count",1)
    if type(count) is not int or not 1 <= count <= 50: return jsonify({"ok":False,"error":"数量必须为 1 到 50 的整数"}), 400
    label = data.get("label","")
    _update_state(creating=True)
    _emit_log("info",f"手动创建: 账号 {acc_id} x{count}")
    try:
        results = _account_mgr.create_aliases_for_account(acc_id, count, label)
        created = [r["email"] for r in results if r.get("ok")]
        errors = [r["error"] for r in results if not r.get("ok")]
        _update_state(creating=False)
        _increment_state(today_created=len(created), total_created=len(created))
        if created: _emit_log("success",f"创建完成: {len(created)} 个")
        return jsonify({"ok":len(created)>0,"emails":created,"created":len(created),"errors":len(errors),"error":errors[0] if errors else None,"lines":[r["line"] for r in results if r.get("ok")],"output":"\n".join(r["line"] for r in results if r.get("ok")),"results":results})
    except Exception as e:
        _update_state(creating=False)
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/create-batch", methods=["POST"])
def api_create_batch():
    data = request.get_json() or {}
    account_ids = data.get("account_ids",[])
    count = data.get("count_per_account",5)
    if type(count) is not int or not 1 <= count <= 20: return jsonify({"ok":False,"error":"每账号数量必须为 1 到 20 的整数"}), 400
    label = data.get("label","")
    interval = float(data.get("interval",3.0))
    if not account_ids: return jsonify({"ok":False,"error":"请选择至少一个账号"})
    _update_state(creating=True)
    _emit_log("info",f"批量创建: {len(account_ids)} 个账号 x{count}")
    try:
        all_results = _account_mgr.create_aliases_batch(account_ids, count, interval, label)
        total_created = sum(sum(1 for r in results if r.get("ok")) for results in all_results.values())
        total_errors = sum(sum(1 for r in results if not r.get("ok")) for results in all_results.values())
        _update_state(creating=False)
        _increment_state(today_created=total_created, total_created=total_created)
        _emit_log("success",f"批量完成: {total_created} 成功 / {total_errors} 失败")
        return jsonify({"ok":True,"total_created":total_created,"total_errors":total_errors,"results":{acc_id:[{"email":r.get("email"),"ok":r.get("ok"),"error":r.get("error"),"inbox_url":r.get("inbox_url"),"line":r.get("line")} for r in results] for acc_id,results in all_results.items()},"lines":[r["line"] for results in all_results.values() for r in results if r.get("ok")],"output":"\n".join(r["line"] for results in all_results.values() for r in results if r.get("ok"))})
    except Exception as e:
        _update_state(creating=False)
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts/<acc_id>/app-password", methods=["POST"])
def api_set_app_password(acc_id):
    data = request.get_json() or {}
    pwd = data.get("app_password","").strip()
    icloud_email = data.get("icloud_email","").strip()
    if not pwd: return jsonify({"ok":False,"error":"密码不能为空"})
    try:
        _account_mgr.set_app_password(acc_id, pwd)
        if icloud_email: _account_mgr.update_account(acc_id, icloud_email=icloud_email)
        result = _account_mgr.test_imap_connection(acc_id)
        return jsonify(result)
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts/<acc_id>/inbox")
def api_inbox(acc_id):
    limit = request.args.get("limit",50,type=int)
    force = request.args.get("force","0")=="1"
    try:
        emails = _account_mgr.check_inbox(acc_id, limit=limit, force=force)
        stats = _account_mgr._cache.get_stats(acc_id)
        return jsonify({"emails":emails,"count":len(emails),"cached":stats})
    except Exception as e: return jsonify({"emails":[],"count":0,"error":str(e)})

@app.route("/api/accounts/<acc_id>/inbox-stream")
def api_inbox_stream(acc_id):
    limit = request.args.get("limit",50,type=int)
    days = request.args.get("days",7,type=int)
    def generate():
        yield f"data: {json.dumps({'type':'start'})}\n\n"
        try: mail = _account_mgr.get_mail_client(acc_id)
        except Exception as e: yield f"data: {json.dumps({'type':'error','error':str(e)[:200]})}\n\n"; return
        try:
            count = 0
            for msg in mail.stream_inbox(limit=limit, days=days):
                count += 1
                yield f"data: {json.dumps({'type':'email','count':count,'email':msg},ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type':'done','count':count})}\n\n"
        except GeneratorExit: pass
        except Exception as e: yield f"data: {json.dumps({'type':'error','error':str(e)[:200]})}\n\n"
        finally:
            try: mail.disconnect()
            except: pass
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/api/accounts/<acc_id>/message/<msg_id>")
def api_message_body(acc_id, msg_id):
    try:
        mail = _account_mgr.get_mail_client(acc_id)
        try:
            full = mail.fetch_full(msg_id.encode() if isinstance(msg_id,str) else msg_id)
            return jsonify({"ok":True,"message":full})
        finally: mail.disconnect()
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts/<acc_id>/mail/<alias_email>")
def api_specific_alias_mail(acc_id, alias_email):
    limit = request.args.get("limit",20,type=int)
    days = request.args.get("days",30,type=int)
    try:
        msgs = _account_mgr.check_alias_mail(acc_id, alias_email, limit=limit, days=days)
        return jsonify({"emails":msgs,"count":len(msgs),"alias":alias_email})
    except Exception as e: return jsonify({"emails":[],"count":0,"error":str(e)})

@app.route("/api/public/mail")
@app.route("/api/mail")
def api_mail_by_email():
    email = request.args.get("email","").strip().lower()
    alias = request.args.get("alias","").strip().lower()
    limit = request.args.get("limit",20,type=int)
    days = request.args.get("days",30,type=int)
    if not email: return jsonify({"error":"请提供 email 参数"})
    acc_id = None
    for a in _account_mgr.list_accounts():
        if a.get("icloud_email","").lower()==email or a.get("real_email","").lower()==email: acc_id=a["id"]; break
    if not acc_id: return jsonify({"error":f"未找到邮箱对应的账号: {email}"})
    try:
        if alias:
            msgs = _account_mgr.check_alias_mail(acc_id, alias, limit=limit, days=days)
            return jsonify({"emails":msgs,"count":len(msgs),"alias":alias,"account":email})
        else:
            by_alias = _account_mgr.check_all_aliases_mail(acc_id, limit_per=limit, days=days)
            total = sum(len(v) for v in by_alias.values())
            return jsonify({"by_alias":by_alias,"total":total,"account":email})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/accounts/<acc_id>/alias-mail")
def api_alias_mail(acc_id):
    force = request.args.get("force","0")=="1"
    try:
        by_alias = _account_mgr.check_all_aliases_mail(acc_id, force=force)
        total = sum(len(v) for v in by_alias.values())
        stats = _account_mgr._cache.get_stats(acc_id)
        return jsonify({"by_alias":by_alias,"total":total,"cached":stats})
    except Exception as e: return jsonify({"by_alias":{},"total":0,"error":str(e)})

@app.route("/api/aliases")
def api_aliases():
    try:
        aliases = _account_mgr.get_all_aliases()
        return jsonify({"aliases":aliases,"count":len(aliases)})
    except Exception as e: return jsonify({"aliases":[],"count":0,"error":str(e)})

def email_storage():
    source = RESULTS_DIR / "latest_emails.txt"
    store = Storage(database_for(source))
    store.import_emails(source)
    email_catalog.restore_legacy_deletions(store)
    return store


@app.route("/api/fastai-sync", methods=["GET", "POST"])
def api_fastai_sync():
    if request.method == "GET":
        return jsonify(ok=True, **_fastai_sync.snapshot())
    try:
        return jsonify(ok=True, **_fastai_sync.configure(request.get_json(silent=True)))
    except ValueError as error:
        return jsonify(ok=False,error=str(error)),400


@app.route("/api/fastai-sync/run", methods=["POST"])
def api_fastai_sync_run():
    data = request.get_json(silent=True)
    if data is not None and (not isinstance(data,dict) or set(data)-{'emails','all'}):
        return jsonify(ok=False,error="同步请求字段无效"),400
    try:
        job = _fastai_sync.start(emails=data.get('emails') if data else None, all_pending=data.get('all',False) if data else False)
        return jsonify(ok=True,job=job),202
    except ValueError as error:
        return jsonify(ok=False,error=str(error)),400


@app.route("/api/fastai-sync/jobs/<job_id>")
def api_fastai_sync_job(job_id):
    job = _fastai_sync.get_job(job_id)
    if job is None: return jsonify(ok=False,error="同步任务不存在"),404
    return jsonify(ok=True,job=job)


@app.route("/api/public/emails")
@app.route("/api/emails")
def api_emails():
    if 'page' in request.args or 'page_size' in request.args:
        try:
            data = email_catalog.page(email_storage(),request.args,_fastai_sync.config['api_url'])
        except ValueError as error:
            return jsonify(ok=False,error=str(error)),400
        rows = data.pop('rows')
        for item in rows:
            link = json.loads(item.pop('link_data') or '{}')
            status = item.pop('sync_status')
            item['fastai_synced'] = status == 'synced'
            item['fastai_sync_status'] = status or 'pending'
            item['fastai_synced_at'] = item.pop('synced_at') if item['fastai_synced'] else None
            item.pop('synced_at',None)
            item['fastai_sync_error'] = item.pop('sync_error') or ''
            item['address_type'] = _address_type(item['email'])
            item['address_type_label'] = '分裂地址' if item['address_type']=='split' else '源地址'
            item['source_email'] = link.get('base_email') or item['email'].split('+')[0].split('@')[0]+'@'+item['email'].split('@')[-1]
            item['inbox_ready'] = is_ready(link)
            item['inbox_verified'] = bool(link.get('verified'))
            item['setup_error'] = link.get('deletion_error') or link.get('last_error') or ''
            item['setup_stage'] = link.get('stage') or 'pending'
            item['deletion_error'] = link.get('deletion_error') or ''
            item['local_deleted'] = bool(link.get('deleted'))
            item['shared_inbox'] = bool(link.get('shared_inbox'))
            if item['inbox_ready']: item['inbox_url'] = link['inbox_url']
        response = jsonify(emails=rows,**data)
        response.headers['Cache-Control']='no-store'
        return response
    limit = request.args.get("limit",0,type=int)
    emails = email_storage().emails()
    if limit > 0:
        emails = emails[-limit:]
    emails.reverse()
    links = load_links()
    sync_states = _fastai_sync.records()
    split_counts = email_storage().split_counts()
    for item in emails:
        link = links.get(item["email"].lower(), {})
        sync = sync_states.get(item["email"].lower(), {})
        same_link = sync.get("inbox_url") == link.get("inbox_url")
        item["fastai_synced"] = bool(same_link and sync.get("status") == "synced")
        item["fastai_sync_status"] = sync.get("status", "pending") if same_link else "pending"
        item["fastai_synced_at"] = sync.get("synced_at") if item["fastai_synced"] else None
        item["fastai_sync_error"] = sync.get("error", "") if same_link else ""
        item["inbox_verified"] = bool(link.get("verified"))
        item["address_type"] = _address_type(item["email"])
        item["address_type_label"] = "分裂地址" if item["address_type"] == "split" else "源地址"
        item["split_count"] = split_counts.get(item["email"].lower(), 0) if item["address_type"] == "source" else None
        item["source_email"] = (link.get("base_email") or item["email"].split("+", 1)[0] + "@" + item["email"].split("@")[-1]) if item["address_type"] == "split" else item["email"]
        item["shared_inbox"] = bool(link.get("shared_inbox"))
        item["inbox_ready"] = is_ready(link)
        item["setup_stage"] = link.get("stage", "ready" if item["inbox_ready"] else "pending")
        item["setup_error"] = link.get("last_error", "")
        if item["inbox_ready"]:
            item["inbox_url"] = link.get("inbox_url", "")
    response = jsonify({"emails":emails,"count":len(emails)})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/emails/bulk',methods=['POST'])
def api_bulk_emails():
    data = request.get_json(silent=True)
    if not isinstance(data,dict) or data.get('action') not in ('delete','split','repair','clear','sync'):
        return jsonify(ok=False,error='批量操作无效'),400
    try:
        with _email_bulk_lock:
            if _registration_jobs.active: raise RuntimeError('自动注册轮换正在执行，请等待完成后操作邮箱')
            store = email_storage()
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                rows = email_catalog.selected(db,data.get('selection'),_fastai_sync.config['api_url'])
                if not rows: raise ValueError('所选邮箱已不存在或未选择邮箱')
            if data['action']=='delete':
                if any(j.active for j in (_creation_jobs,_plus_creation_jobs,_inbox_clear_jobs,_plus_jobs,_email_delete_jobs)) or _fastai_sync.active:
                    raise RuntimeError('有邮箱任务执行中，请等待完成后删除')
                job_id = _email_delete_jobs.start_emails(rows)
                return _bulk_started(job_id,'delete',len(rows))
            if _email_delete_jobs.active:
                raise RuntimeError('删除任务执行中，请等待完成')
            if data['action']=='sync':
                links = load_links()
                eligible = [r['email'] for r in rows if is_ready(links.get(r['email'],{}))]
                if not eligible: return _bulk_skipped('sync',len(rows))
                job = _fastai_sync.start(emails=eligible,selected_only=True)
                return _bulk_started(job['id'],'sync',job['total'],len(rows)-job['total'])
            if data['action']=='split':
                sources = _plus_creation_jobs.sources()
                if any(row['email'] not in sources for row in rows):
                    raise ValueError('批量分裂仅支持已配置收件的源邮箱；请筛选源地址后选择')
                job_id = _plus_creation_jobs.start([row['email'] for row in rows],count=1,label='manual',item_interval=0)
                return _bulk_started(job_id,'split',len(rows))
            links = load_links()
            if data['action']=='clear':
                records, destinations = [],set()
                source_count = 0
                for row in rows:
                    if _address_type(row['email'])!='source': continue
                    source_count += 1
                    record = links.get(row['email'],{})
                    if not is_ready(record) or not record.get('destination'):
                        raise ValueError('所选源邮箱存在未配置收件的地址，请先补齐配置')
                    if record['destination'] in destinations: continue
                    destinations.add(record['destination'])
                    records.append({**record,'email':row['email']})
                skipped = len(rows)-len(records)
                if not records: return _bulk_skipped('clear',skipped)
                job_id = _inbox_clear_jobs.start_inboxes(records)
                return _bulk_started(job_id,'clear',len(records),skipped)
            repair = {}
            for row in rows:
                record = links.get(row['email'],{})
                if any(record.get(key) for key in ('deleted','deleting','parent_deleted')): continue
                if not is_ready(record):
                    if not _account_mgr.get_account(row['account_id']): raise ValueError('邮箱所属账号不存在')
                    repair.setdefault(row['account_id'],[]).append(row['email'])
            total = sum(map(len,repair.values()))
            if not total: return _bulk_skipped('repair',len(rows))
            job_id = _creation_jobs.start(list(repair),repair=repair)
            return _bulk_started(job_id,'repair',total,len(rows)-total)
    except ValueError as error:
        return jsonify(ok=False,error=str(error)),400
    except RuntimeError as error:
        return jsonify(ok=False,error=str(error)),409



def _bulk_started(job_id, kind, total, skipped=0):
    import time
    entry = {'id':job_id,'kind':kind,'total':total,'skipped':skipped,'started_at':time.time()}
    email_storage().write_document('email_bulk_task',entry,job_id)
    return jsonify(ok=True,job_id=job_id,selected=total,skipped=skipped,kind=kind),202


def _bulk_skipped(kind, skipped):
    job_id = secrets.token_hex(12)
    email_storage().write_document('email_bulk_noop',{'id':job_id,'status':'completed',
        'total_created':0,'total_errors':0,'events':[],'progress':'没有需要处理的邮箱'},job_id)
    return _bulk_started(job_id,kind,0,skipped)


def _bulk_job(job_id):
    entry = email_storage().read_document('email_bulk_task',job_id)
    noop = email_storage().read_document('email_bulk_noop',job_id)
    if noop:
        job = noop
    elif entry and entry['kind']=='sync':
        job = _fastai_sync.get_job(job_id)
    else:
        job = next((job for manager in (_creation_jobs,_plus_creation_jobs,_inbox_clear_jobs,_email_delete_jobs,_registration_jobs)
                    if (job:=manager.get(job_id))),None)
    if job and entry: job = {**job,'kind':entry['kind'],'skipped':entry['skipped'],'total':entry['total']}
    return job


@app.route('/api/emails/bulk-jobs')
def api_email_bulk_history():
    with email_storage().connect() as db:
        entries = [json.loads(row[0]) for row in db.execute(
            "SELECT data FROM documents WHERE kind='email_bulk_task' ORDER BY rowid DESC LIMIT 50")]
    history = []
    for entry in entries:
        job = _bulk_job(entry['id']) or {}
        history.append({**entry,'status':job.get('status','interrupted'),
                        'succeeded':job.get('succeeded',job.get('total_created',0)),
                        'failed':job.get('failed',job.get('total_errors',0)),
                        'skipped':entry.get('skipped',0)+job.get('total_skipped',0)})
    return jsonify(ok=True,history=history),200,{'Cache-Control':'no-store'}


@app.route('/api/emails/bulk-jobs/<job_id>')
def api_email_bulk_job(job_id):
    job = _bulk_job(job_id)
    if not job: return jsonify(ok=False,error='任务不存在'),404
    return jsonify(ok=True,job=job),200,{'Cache-Control':'no-store'}


@app.route('/api/emails/bulk-jobs/<job_id>/cancel',methods=['POST'])
def api_cancel_email_bulk_job(job_id):
    entry = email_storage().read_document('email_bulk_task',job_id)
    if entry and entry['kind']=='sync': return jsonify(ok=_fastai_sync.cancel(job_id))
    for jobs in (_creation_jobs,_plus_creation_jobs,_inbox_clear_jobs,_email_delete_jobs,_registration_jobs):
        if jobs.get(job_id): return jsonify(ok=jobs.cancel(job_id))
    return jsonify(ok=False,error='任务不存在'),404


def _address_type(email):
    return "split" if "+" in email.split("@", 1)[0] else "source"

def _text_export(lines):
    return Response("\n".join(lines) + ("\n" if lines else ""), mimetype="text/plain", headers={
        "Content-Disposition": 'attachment; filename="icloud_inbox_links.txt"',
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    })

def pending_inbox_repairs(account_ids, address_type="all"):
    links = load_links()
    repair = {acc_id: [] for acc_id in account_ids}
    for row in email_storage().emails():
        email, account_id = row["email"].lower(), row["account_id"]
        if account_id not in repair or (address_type != "all" and _address_type(email) != address_type):
            continue
        if not is_ready(links.get(email, {})) and email not in repair[account_id]:
            repair[account_id].append(email)
    return {key: values for key, values in repair.items() if values}


@app.route("/api/inbox-links/repair", methods=["POST"])
def api_repair_inbox_links():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error="请求必须是 JSON 对象"), 400
    account_id, address_type = data.get("account_id", "all"), data.get("address_type", "all")
    if address_type not in ("all", "source", "split"):
        return jsonify(ok=False, error="地址类型无效"), 400
    ids = [a["id"] for a in _account_mgr.list_accounts()] if account_id == "all" else [account_id]
    if any(not isinstance(x, str) or not _account_mgr.get_account(x) for x in ids):
        return jsonify(ok=False, error="账号不存在"), 400
    repair = pending_inbox_repairs(ids, address_type)
    total = sum(map(len, repair.values()))
    if not total:
        return jsonify(ok=True, pending=0)
    try:
        job_id = _creation_jobs.start(list(repair), repair=repair)
        return jsonify(ok=True, job_id=job_id, pending=total), 202
    except RuntimeError as error:
        return jsonify(ok=False, error=str(error)), 409


@app.route("/api/create-jobs", methods=["POST"])
def api_start_creation_job():
    data = request.get_json(silent=True) or {}
    ids = data.get("account_ids", [])
    count = data.get("count_per_account", 1)
    if not isinstance(ids, list) or not ids or any(not isinstance(x, str) for x in ids):
        return jsonify(ok=False, error="请选择账号"), 400
    ids = list(dict.fromkeys(ids))
    if any(not _account_mgr.get_account(x) or _account_mgr.get_account(x).get("status") != "active" for x in ids):
        return jsonify(ok=False, error="账号不存在或未通过校验"), 400
    if type(count) is not int or not 1 <= count <= 20:
        return jsonify(ok=False, error="每账号数量必须为 1 到 20 的整数"), 400
    repair = None
    if data.get("mode") == "repair":
        repair = pending_inbox_repairs(ids)
    try:
        job_id = _creation_jobs.start(ids, count, str(data.get("label", ""))[:100], repair=repair)
        return jsonify(ok=True, job_id=job_id), 202
    except RuntimeError as error:
        return jsonify(ok=False, error=str(error)), 409

@app.route("/api/create-jobs/<job_id>")
def api_creation_job(job_id):
    job = _creation_jobs.get(job_id)
    if not job: return jsonify(ok=False, error="任务不存在或服务已重启；请刷新列表，补全未完成的收件配置"), 404
    response = jsonify(job)
    response.headers["Cache-Control"] = "no-store"
    return response

@app.route("/api/create-jobs/<job_id>/export")
def api_export_creation_job(job_id):
    job = _creation_jobs.get(job_id)
    if not job: return jsonify(ok=False, error="任务不存在"), 404
    return _text_export(job["lines"])

@app.route("/api/public/inbox-links/export")
@app.route("/api/inbox-links/export")
def api_export_inbox_links():
    account_filter = request.args.get("account_id", "")
    address_filter = request.args.get("address_type", "all")
    keyword = request.args.get("q", "").strip().lower()
    if address_filter not in ("all", "source", "split"):
        return jsonify(error="地址类型无效"), 400
    emails = [row["email"] for row in email_storage().emails()
              if (not account_filter or row["account_id"] == account_filter)
              and (address_filter == "all" or _address_type(row["email"]) == address_filter)
              and (not keyword or keyword in row["email"].lower())]
    lines = export_lines(dict.fromkeys(emails))
    return _text_export(lines)

@app.route("/api/scheduler/start", methods=["POST"])
def api_scheduler_start():
    config = _auto_registration.snapshot()["config"]
    ids = config["account_ids"] or [a["id"] for a in _account_mgr.list_accounts() if a.get("status") == "active"]
    try:
        _auto_registration.configure({"enabled": True, "account_ids": ids})
        return jsonify(ok=True)
    except ValueError as error:
        return jsonify(ok=False, error=str(error)), 400

@app.route("/api/scheduler/stop", methods=["POST"])
def api_scheduler_stop():
    _auto_registration.configure({"enabled": False})
    return jsonify({"ok":True})

@app.route("/api/log-stream")
def api_log_stream():
    # Waitress 的固定工作线程不能长期等待日志；旧页面断开后也可能继续占用线程。
    # 每次只返回有限批次，EventSource 按现有重连逻辑继续读取，空队列立即结束。
    frames = ["retry: 5000\n\n"]
    for _ in range(100):
        try:
            entry = _log_queue.get_nowait()
        except queue.Empty:
            break
        frames.append(f"data: {json.dumps(entry,ensure_ascii=False)}\n\n")
    frames.append(": heartbeat\n\n")
    return Response("".join(frames), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

def main():
    import argparse, os, signal as _signal
    parser = argparse.ArgumentParser(description="iCloud HME Web UI")
    parser.add_argument("--port",type=int,default=int(os.environ.get("PORT",2224)))
    parser.add_argument("--host",type=str,default=os.environ.get("HOST","0.0.0.0"))
    parser.add_argument("--scheduler",action="store_true",help="启动时自动运行调度器")
    parser.add_argument("--no-sync",action="store_true",help="跳过时间校准")
    parser.add_argument("--disable-automation", action="store_true",
                        help="启动时关闭自动注册、自动分裂、自动同步及周期账号检查")
    parser.add_argument('--session-keepalive', action='store_true',
                        help='独立开启随机间隔 10–20 分钟的会话保活，可与 --disable-automation 同时使用')
    args = parser.parse_args()
    if args.disable_automation and args.scheduler:
        parser.error("--disable-automation 不能与 --scheduler 同时使用")
    if args.disable_automation:
        # Imported local data can contain enabled schedules and expired deadlines.
        # Persist the disabled state before starting any scheduler threads.
        for automation in (_auto_registration, _auto_plus, _fastai_sync):
            automation.configure({"enabled": False})
    if not args.no_sync:
        offset = _sync_time()
        if abs(offset)>0.5: print(f"[*] Time sync: offset {offset:.1f}s")
    if args.session_keepalive or not args.disable_automation:
        threading.Thread(target=_health_loop, daemon=True).start()
    accounts = _account_mgr.list_accounts()
    if accounts:
        print(f"[+] {len(accounts)} account(s) loaded")
        for a in accounts: print(f"    [OK] {a.get('name','?')} - {a.get('real_email','?')} ({a.get('alias_total',0)} aliases)")
    else: print("[*] No accounts yet")
    if args.scheduler:
        api_scheduler_config = _auto_registration.snapshot()["config"]
        ids = api_scheduler_config["account_ids"] or [a["id"] for a in accounts if a.get("status") == "active"]
        _auto_registration.configure({"enabled": True, "account_ids": ids})
    _auto_registration.start()
    _auto_plus.start()
    _fastai_sync.start_scheduler()
    def _shutdown(sig,frame): print("\n[*] Shutting down..."); _auto_registration.close(); _auto_plus.close(); _fastai_sync.close(); _stop_event.set(); os._exit(0)
    _signal.signal(_signal.SIGINT, _shutdown)
    _signal.signal(_signal.SIGTERM, _shutdown)
    try:
        from waitress import serve
        print(f"\n  Production → http://{args.host}:{args.port}\n")
        serve(app, host=args.host, port=args.port, threads=8)
    except ImportError:
        print(f"\n  Dev server → http://{args.host}:{args.port}\n")
        app.run(host=args.host, port=args.port, debug=False, threaded=True)

if __name__=="__main__": main()
