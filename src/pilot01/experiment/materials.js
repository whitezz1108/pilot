'use strict';
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let cases=[],current=null,view='E0',requestSerial=0;
const names={old40:'旧40份',new40:'新40份'};
async function read(path){const r=await fetch(path,{cache:'no-store'});if(!r.ok)throw Error('读取失败，请检查登录状态或刷新重试');return r.json()}
function filtered(){const cohort=$('#cohort').value,q=$('#query').value.trim().toLowerCase();return cases.filter(c=>(cohort==='all80'||c.cohort===cohort)&&(!q||c.case_id.toLowerCase().includes(q)))}
function renderList(){const rows=filtered();$('#count').textContent=`${rows.length}份合同 · ${rows.reduce((n,c)=>n+c.memo_variants.length,0)}份可用memo`;
$('#cases').innerHTML=rows.length?rows.map(c=>`<button class="case ${current?.case_id===c.case_id?'active':''}" data-case="${esc(c.case_id)}"><b>${esc(c.case_id)}</b><small>${names[c.cohort]} · ${c.memo_variants.join(' / ')} · ${c.characters.toLocaleString()}字</small></button>`).join(''):'<div class="empty">没有匹配合同</div>';
$$('[data-case]').forEach(b=>b.onclick=()=>select(b.dataset.case));}
function render(){if(!current)return;$('#caseTitle').textContent=current.case_id;$('#caseMeta').textContent=`${names[current.cohort]} · ${current.contract_text.length.toLocaleString()}字 · ${current.memo_variants.join(' / ')}`;
$('#hash').textContent='原文校验值 '+current.contract_text_hash;
$$('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===view);b.disabled=b.dataset.view==='E1'&&!current.memos.E1;b.title=b.disabled?'这份合同没有可删除的目标claim，冻结数据只提供E0':''});
$('#content').className='';
if(view==='contract'){$('#content').replaceChildren();const pre=document.createElement('pre');pre.textContent=current.contract_text;$('#content').append(pre);return;}
const memo=current.memos[view];if(!memo){$('#content').innerHTML='<div class="empty">这份合同没有E1材料。</div>';return;}
$('#content').innerHTML=`<div class="muted">${view==='E0'?'完整摘要':'遗漏摘要：一个目标claim被中性背景claim替换，摘要仍保持5条'} · ${memo.claims.length}条claim</div>`+memo.claims.map((c,i)=>`<article class="claim"><div class="claimHead"><strong>${i+1}. ${esc(c.category)}</strong><span class="tag">${esc(c.status)}</span><span class="tag">${esc(c.claim_id)}</span></div><p>${esc(c.summary)}</p>${c.source_ids.map(id=>`<details><summary>原文依据 · ${esc(id)}</summary><pre>${esc(current.cited_sources[id]||'该引用没有可预览的原文片段')}</pre></details>`).join('')}${!c.source_ids.length?'<span class="muted">这条claim未附段落引用。</span>':''}</article>`).join('');}
async function select(id){const serial=++requestSerial;$('#content').textContent='正在读取合同与memo…';try{const data=await read('/api/materials/case?case_id='+encodeURIComponent(id));if(serial!==requestSerial)return;current=data;if(view==='E1'&&!data.memos.E1)view='E0';renderList();render();const url=new URL(location.href);url.searchParams.set('case_id',id);history.replaceState(null,'',url);}catch(e){if(serial===requestSerial){$('#content').className='error';$('#content').textContent=e.message;}}}
$('#query').oninput=renderList;$('#cohort').onchange=()=>{renderList();const rows=filtered();if(rows.length&&!rows.some(r=>r.case_id===current?.case_id))select(rows[0].case_id);};
$$('[data-view]').forEach(b=>b.onclick=()=>{view=b.dataset.view;render();});
(async()=>{try{cases=(await read('/api/materials')).materials;const params=new URLSearchParams(location.search),cohort=params.get('cohort');if(['old40','new40','all80'].includes(cohort))$('#cohort').value=cohort;renderList();const wanted=params.get('case_id'),rows=filtered();const id=rows.some(c=>c.case_id===wanted)?wanted:rows[0]?.case_id;if(id)await select(id);else $('#content').textContent='没有可预览的材料';}catch(e){$('#content').textContent=e.message;$('#content').className='error';}})();
