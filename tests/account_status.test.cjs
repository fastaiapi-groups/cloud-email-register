const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function panel() {
  const input = {value:'a',checked:true};
  const status = {textContent:'会话正常',title:''};
  const row = {querySelector:selector=>selector==='input'?input:status};
  const fields = {};
  let now = 120000;
  let account = {id:'a',status:'error',last_error:'Apple 邮件规则会话未通过验证（HTTP 401）'};
  let offline = false;
  const calls = [];
  const context = vm.createContext({
    isPlus:false,requestBusy:false,pollBusy:false,connected:true,lastAccountStatusCheck:0,
    apiBase:'/api/auto-registration',Promise,Map,parent:{},location:{origin:'https://icloud.fastaiapi.cloud'},
    Date:{now:()=>now},render:()=>{},
    document:{querySelectorAll:selector=>selector==='#accounts label'?[row]:[status]},
    byId:id=>fields[id]??=( {textContent:''} ),
    api:async path=>{
      calls.push(path);
      if(path==='/api/accounts') {
        if(offline)throw Error('offline');
        return {accounts:[account]};
      }
      return {};
    }
  });
  const source=fs.readFileSync('static/auto_registration.js','utf8');
  const helpers=source.slice(source.indexOf('function accountStateText'),source.indexOf('function countdown'));
  const refresh=source.slice(source.indexOf('async function refresh()'),source.indexOf('async function save()'));
  vm.runInContext(helpers+refresh,context);
  return {context,input,status,calls,
    advance:(milliseconds=60001)=>{now+=milliseconds},
    active:()=>{account={id:'a',status:'active'}},
    offline:()=>{offline=true}};
}

test('Cookie 状态变化会同步到已打开的自动注册页，并保留未保存的勾选',async()=>{
  const p=panel();
  await p.context.refresh();
  assert.equal(p.status.textContent,'邮件规则需验证');
  assert.match(p.status.title,/HTTP 401/);
  assert.equal(p.input.checked,true);
  p.active();p.advance();
  await p.context.refresh();
  assert.equal(p.status.textContent,'会话正常');
  assert.equal(p.status.title,'');
  assert.equal(p.input.checked,true);
});

test('账号状态读取失败后不继续显示旧的正常标记，重试可以恢复',async()=>{
  const p=panel();p.offline();
  await p.context.refresh();
  assert.equal(p.status.textContent,'状态未更新');
  assert.equal(p.context.connected,false);
  assert.equal(p.context.pollBusy,false);
});

test('账号状态最多每六十秒读取一次，分裂页沿用原有数据请求',async()=>{
  const p=panel();
  await p.context.refresh();await p.context.refresh();
  assert.equal(p.calls.filter(path=>path==='/api/accounts').length,1);
  p.advance(59000);await p.context.refresh();
  assert.equal(p.calls.filter(path=>path==='/api/accounts').length,1);
  p.advance(1001);await p.context.refresh();
  assert.equal(p.calls.filter(path=>path==='/api/accounts').length,2);
  p.context.isPlus=true;p.advance();await p.context.refresh();
  assert.equal(p.calls.filter(path=>path==='/api/accounts').length,2);
});


test('手动校验或更新 Cookie 后立即同步，只接受同源父页面通知',async()=>{
  const p=panel();await p.context.refresh();p.active();
  await p.context.refresh();
  assert.equal(p.calls.filter(path=>path==='/api/accounts').length,1);
  await p.context.accountStatusChanged({origin:'https://evil.example',source:p.context.parent,data:{type:'icloud-account-status-changed'}});
  await p.context.accountStatusChanged({origin:p.context.location.origin,source:{},data:{type:'icloud-account-status-changed'}});
  assert.equal(p.calls.filter(path=>path==='/api/accounts').length,1);
  await p.context.accountStatusChanged({origin:p.context.location.origin,source:p.context.parent,data:{type:'icloud-account-status-changed'}});
  assert.equal(p.calls.filter(path=>path==='/api/accounts').length,2);
  assert.equal(p.status.textContent,'会话正常');
  assert.equal(p.input.checked,true);
});

test('管理员请求助手在账号校验结束后通知注册页，普通请求不会触发',async()=>{
  const messages=[];
  const location={href:'https://icloud.fastaiapi.cloud/',origin:'https://icloud.fastaiapi.cloud'};
  let responseStatus=200;
  const context=vm.createContext({URL,Request,Headers,location,
    document:{addEventListener:()=>{},getElementById:id=>id==='autoFrame'?{contentWindow:{postMessage:(...args)=>messages.push(args)}}:null},
    window:{top:{location:{assign:()=>{}}},fetch:async input=>({
      status:input==='/api/auth/session'?200:responseStatus,ok:responseStatus===200,
      json:async()=>({csrf_token:'fixture-csrf'})})}
  });
  vm.runInContext(fs.readFileSync('static/admin_session.js','utf8'),context);
  await context.window.fetch('/api/accounts/a/validate',{method:'POST'});
  assert.equal(messages.length,1);
  assert.equal(messages[0][0].type,'icloud-account-status-changed');
  assert.equal(messages[0][1],location.origin);
  responseStatus=400;
  await context.window.fetch('/api/accounts/a/cookies',{method:'POST'});
  assert.equal(messages.length,2);
  await context.window.fetch('/api/auto-registration',{method:'POST'});
  await context.window.fetch('https://other.example/api/accounts/a/validate',{method:'POST'});
  assert.equal(messages.length,2);
});
