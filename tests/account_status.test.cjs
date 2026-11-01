const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function panel() {
  const input = {value:'a',checked:true};
  const status = {textContent:'会话正常',title:''};
  const row = {querySelector:selector=>selector==='input'?input:status};
  const fields = {};
  let now = 20000;
  let account = {id:'a',status:'error',last_error:'Apple 邮件规则会话未通过验证（HTTP 401）'};
  let offline = false;
  const calls = [];
  const context = vm.createContext({
    isPlus:false,requestBusy:false,pollBusy:false,connected:true,lastAccountStatusCheck:0,
    apiBase:'/api/auto-registration',Promise,Map,
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
    advance:()=>{now+=10001},
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

test('账号状态最多每十秒读取一次，分裂页沿用原有数据请求',async()=>{
  const p=panel();
  await p.context.refresh();await p.context.refresh();
  assert.equal(p.calls.filter(path=>path==='/api/accounts').length,1);
  p.context.isPlus=true;p.advance();await p.context.refresh();
  assert.equal(p.calls.filter(path=>path==='/api/accounts').length,1);
});
