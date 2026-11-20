import {store} from '../stores/entities.js';
import {session} from '../stores/session.js';
import {quotaAPI} from '../api/entities.js';
import {table} from '../components/common/table.js';
import {esc} from '../utils/html.js';
import {message} from '../hooks/action.js';
import {QUOTA_MODES,TRANSFER_STATUSES} from '../constants/enums.js';

const key=prefix=>prefix+'-'+Date.now().toString(36)+Math.random().toString(36).slice(2,7);
const num=v=>Number(v||0);
const modeBadge=p=>`<span class="badge ${p.mode==='fenced'?'failed':p.mode==='offline'?'pending':''}">${esc(QUOTA_MODES[p.mode]||p.mode)}</span>`;

function summary(q){
 const s=q.summary;
 return `<section class="quota-summary">
  <div><span>总容量</span><b>${s.total_capacity}</b></div>
  <div><span>已预留</span><b>${s.total_reserved}</b></div>
  <div><span>在途转出</span><b>${s.total_in_transit}</b></div>
  <div><span>全局剩余</span><b>${s.total_remaining}</b></div>
  <div class="${s.invariant_ok?'':'error'}"><span>容量约束</span><b>${s.invariant_ok?'预留 + 在途 ≤ 总容量':'账本越界，已拒绝写入'}</b></div>
 </section>`;
}

function poolTable(q,isAdmin){
 return table(['节点','网络状态','总容量','已预留','在途转出','在途待入账','剩余额度','写入','模拟断网'],
  q.pools.map(p=>[esc(p.node),modeBadge(p),p.capacity,p.reserved,p.in_transit_out,p.in_transit_in,
   `<b>${p.remaining}</b>`,p.writable?'可接单':'停写',
   isAdmin?`<span class="row-actions"><button class="tiny" data-net="${p.node_id}" data-available="${p.available?'false':'true'}">${p.available?'断网':'恢复'}</button><button class="tiny secondary" data-net="${p.node_id}" data-fenced="${p.fenced?'false':'true'}">${p.fenced?'解除隔离':'隔离停写'}</button></span>`:'仅管理员']));
}

function transferTable(q){
 return table(['编号','转出 → 接收','金额','状态','卡住原因','操作'],q.transfers.map(t=>[
  '#'+t.id,`${esc(t.from_node||'—')} → ${esc(t.to_node||'—')}`,t.amount,
  `<span class="badge ${t.status}">${esc(TRANSFER_STATUSES[t.status]||t.status)}</span>`,
  esc(t.stuck_reason||t.detail||'—'),
  t.status==='pending'&&session.role!=='viewer'?`<span class="row-actions"><button class="tiny" data-credit="${t.id}">补领入账</button><button class="tiny secondary" data-rollback="${t.id}">回滚</button></span>`:'—']));
}

export function quotaPage(){
 const q=store.quota;
 if(!q)return '<p class="notice">额度账本尚未载入。</p>';
 const isAdmin=session.role==='admin';
 const canWrite=session.role!=='viewer';
 const poolOptions=q.pools.map(p=>`<option value="${p.id}">${esc(p.node)} · 剩余 ${p.remaining}</option>`).join('');
 return `${summary(q)}
 <div class="section-title"><h2>各库额度池</h2><button class="secondary" id="quota-refresh">刷新账本</button></div>
 ${poolTable(q,isAdmin)}
 <p class="hint">失联节点仍可接本地预留；被隔离节点停写。调拨要求两端在线，转出即扣源池容量，接收方补领后才增加目标池容量。</p>
 ${isAdmin?`<details class="quota-admin"><summary>管理员：调整库位池总容量</summary>
  <form id="pool-form" class="inline-form"><label>节点<select name="node_id">${q.pools.map(p=>`<option value="${p.node_id}">${esc(p.node)}</option>`).join('')}</select></label>
  <label>总容量<input name="capacity" type="number" min="1" value="${q.pools[0]?.capacity||900}" required></label>
  <button type="submit">保存容量</button></form></details>`:''}
 <section class="split quota-forms">
  <div><h2>库位预留</h2><p>预留在节点本地扣减；断网不会阻止预留，隔离会。重复提交同一幂等键只入账一次。</p>
   ${canWrite?`<form id="reserve-form"><label>额度池<select name="pool_id">${poolOptions}</select></label>
   <div class="fields"><label>预留数量<input name="amount" type="number" min="1" value="100" required></label>
   <label>幂等键<input name="idem_key" value="${key('rs')}" maxlength="80" required></label></div>
   <button type="submit">预留额度</button></form>`:'<p class="readonly">观察员仅可查看账本。</p>'}</div>
  <div><h2>空闲额度调拨</h2><p>两端必须在线。转出后源池立刻扣减；接收节点若中途失联，额度停在“在途”，恢复后补领。</p>
   ${canWrite?`<form id="transfer-form"><div class="fields">
   <label>转出池<select name="from_pool_id">${poolOptions}</select></label>
   <label>接收池<select name="to_pool_id">${poolOptions}</select></label></div>
   <div class="fields"><label>调拨数量<input name="amount" type="number" min="1" value="50" required></label>
   <label>幂等键<input name="idem_key" value="${key('tr')}" maxlength="80" required></label></div>
   <div class="row-actions"><button type="submit">发起两阶段调拨</button>
   <button type="button" class="secondary" id="claim-all">批量补领未到账额度</button></div></form>`:'<p class="readonly">观察员仅可查看调拨记录。</p>'}</div>
 </section>
 <div class="section-title"><h2>调拨在途与补领</h2><span>重试不会重复到账</span></div>
 ${transferTable(q)}
 <div class="section-title"><h2>最近事件流水</h2><span>不可变账本 ${q.events.length} 条</span></div>
 ${table(['时间','事件','池','金额','关联','说明'],q.events.map(e=>[esc(e.created_at),esc(e.kind),e.pool_id??'—',e.amount,e.ref_type?`${esc(e.ref_type)} #${e.ref_id}`:'—',esc(e.detail||'')]))}
 <p class="checksum">模型 ${esc(q.model_version)} · ${esc(q.boundary)}</p>
 <details><summary>最近预留记录</summary>${table(['编号','池','金额','状态','幂等键','时间'],q.reservations.map(r=>[r.id,r.pool_id,r.amount,esc(r.status),esc(r.idem_key||'—'),esc(r.created_at)]))}</details>`;
}

async function run(task,render,okText){
 try{await task();await render();message(okText);}catch(err){message(err.message,true);}
}

export function bindQuota(render){
 document.querySelector('#quota-refresh')?.addEventListener('click',render);
 document.querySelectorAll('[data-net]').forEach(btn=>btn.addEventListener('click',()=>{
  const body={};
  if(btn.dataset.available!==undefined)body.available=btn.dataset.available==='true';
  if(btn.dataset.fenced!==undefined)body.fenced=btn.dataset.fenced==='true';
  btn.disabled=true;
  run(()=>quotaAPI.network(Number(btn.dataset.net),body),render,'网络状态已更新，额度账本已重算');
 }));
 document.querySelectorAll('[data-credit]').forEach(btn=>btn.addEventListener('click',()=>{
  btn.disabled=true;run(()=>quotaAPI.credit(Number(btn.dataset.credit)),render,'接收方已补领入账');
 }));
 document.querySelectorAll('[data-rollback]').forEach(btn=>btn.addEventListener('click',()=>{
  btn.disabled=true;run(()=>quotaAPI.rollback(Number(btn.dataset.rollback)),render,'未到账调拨已回滚，源池额度恢复');
 }));
 document.querySelector('#claim-all')?.addEventListener('click',()=>{
  run(async()=>{const r=await quotaAPI.claim();if(r.stuck.length)throw new Error(`已补领 ${r.credited.length} 笔；仍卡住 ${r.stuck.length} 笔：`+r.stuck.map(s=>`#${s.transfer_id} ${s.reason}`).join('；'));},render,'全部在途调拨已补领到账');
 });
 const poolForm=document.querySelector('#pool-form');
 poolForm?.addEventListener('submit',e=>{e.preventDefault();
  const d=Object.fromEntries(new FormData(poolForm));d.node_id=num(d.node_id);d.capacity=num(d.capacity);
  run(()=>quotaAPI.savePool(d),render,'库位池容量已更新');
 });
 const reserveForm=document.querySelector('#reserve-form');
 reserveForm?.addEventListener('submit',e=>{e.preventDefault();
  const d=Object.fromEntries(new FormData(reserveForm));d.pool_id=num(d.pool_id);d.amount=num(d.amount);
  run(()=>quotaAPI.reserve(d),render,'预留已记入账本，刷新后可核对剩余额度');
 });
 const transferForm=document.querySelector('#transfer-form');
 transferForm?.addEventListener('submit',e=>{e.preventDefault();
  const d=Object.fromEntries(new FormData(transferForm));
  d.from_pool_id=num(d.from_pool_id);d.to_pool_id=num(d.to_pool_id);d.amount=num(d.amount);
  run(()=>quotaAPI.transfer(d),render,'调拨已转出，等待接收方补领入账');
 });
}
