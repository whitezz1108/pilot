(()=>{
  'use strict';

  const roster={
    manager:[
      {code:'M1',name:'枫糖',avatar:0},{code:'M2',name:'云朵',avatar:1},
      {code:'M3',name:'青豆',avatar:2},{code:'M4',name:'松果',avatar:3}
    ],
    compliance:[
      {code:'C1',name:'海盐',avatar:4},{code:'C2',name:'米糕',avatar:5},
      {code:'C3',name:'杏仁',avatar:6},{code:'C4',name:'蓝莓',avatar:7}
    ]
  };
  const positions={
    manager:[['15%','41%'],['35%','41%'],['15%','73%'],['35%','73%']],
    compliance:[['65%','41%'],['85%','41%'],['65%','73%'],['85%','73%']]
  };
  const laneRunIds=[null,null,null,null];
  const snapshots=new Map();
  const activeHandoffs=new Set();
  const seenHandoffs=new Set(readSeenHandoffs());
  const stats=document.querySelector('.stats');
  if(!stats)return;

  function readSeenHandoffs(){
    try{return JSON.parse(sessionStorage.getItem('pilot01-office-handoffs')||'[]')}catch(_){return []}
  }
  function rememberHandoff(key){
    seenHandoffs.add(key);
    try{sessionStorage.setItem('pilot01-office-handoffs',JSON.stringify([...seenHandoffs].slice(-120)))}catch(_){}
  }
  function figureMarkup(){
    return `<svg class="cuteSprite" viewBox="0 0 32 40" shape-rendering="crispEdges" aria-hidden="true">
    <path fill="#433b46" d="M7 6h3V3h13v3h3v17h-3v3h3v10H6V26h3v-3H6V9h1z"/>
    <path fill="var(--hair)" d="M9 7h3V5h10v3h3v13H8V9h1z"/>
    <path fill="var(--skin)" d="M10 13h13v10H10zM8 16h2v5H8zM23 16h2v5h-2z"/>
    <path fill="var(--hair)" d="M9 10h15v4h-4v-3h-3v4h-7z"/>
    <path fill="#303343" d="M12 16h2v3h-2zM20 16h2v3h-2z"/>
    <path fill="#eb8f8e" d="M10 20h3v2h-3zM21 20h3v2h-3z"/>
    <path fill="#a15d55" d="M16 21h3v1h-3z"/><path fill="var(--shirt)" d="M10 26h13v9H9v-6h1z"/>
    <path fill="#fff0d3" d="M14 25h5v3h-5z"/>
    <path class="spriteHands" fill="var(--skin)" d="M6 28h4v6H6zM23 28h4v6h-4z"/>
    <path class="spriteFeet" fill="#414959" d="M10 35h5v5H9v-3h1zM19 35h5v5h-6v-3h1z"/></svg>`;
  }
  function seatMarkup(role,index){
    const person=roster[role][index],position=positions[role][index];
    return `<button type="button" class="gameSeat role-${role} avatar-${person.avatar} is-idle" data-role="${role}" data-lane="${index}" style="--seat-x:${position[0]};--seat-y:${position[1]}" aria-disabled="true">
      <span class="seatSignal" aria-hidden="true"></span>
      <span class="gameSpeech" aria-live="polite"><small>工作播报</small><b></b></span>
      <span class="gameAgent" aria-hidden="true">${figureMarkup()}</span>
      <span class="deskFurniture" aria-hidden="true"><svg viewBox="0 0 136 76" shape-rendering="crispEdges"><path fill="#72523e" d="M7 26h8v46H7zM120 26h8v46h-8z"/><path fill="#4e504c" d="M15 35h27v33H15zM95 35h25v33H95z"/><path fill="#889081" d="M18 38h21v10H18zM18 52h21v11H18zM98 38h19v25H98z"/><path fill="#5d4939" d="M0 5h136v34H0z"/><path fill="#b97c4c" d="M3 4h130v28H3z"/><path fill="#ebbb7e" d="M3 0h130v27H3z"/><path fill="#f8d59d" d="M5 2h126v3H5z"/><path fill="#cc985e" d="M7 14h120v1H7z"/></svg></span>
      <span class="monitorGlow" aria-hidden="true"></span><span class="deskKeyboard" aria-hidden="true"></span><span class="deskMug" aria-hidden="true"></span>
      <span class="seatPlate"><b>${person.code} · ${person.name}</b><em>待命</em></span>
      <span class="seatMetric">暂无 run</span>
    </button>`;
  }

  const office=document.createElement('section');
  office.id='agentOffice';
  office.className='panel pixelOffice gameOffice';
  office.setAttribute('aria-labelledby','officeTitle');
  office.innerHTML=`
    <div class="gameOfficeBar">
      <div class="gameOfficeTitle"><span class="officeSign" aria-hidden="true">8</span><div><h2 id="officeTitle">Agent 像素办公室</h2><p id="officeSubtitle">真实实验驱动 · 角色只播报可审计事件</p></div></div>
      <div class="officeLiveCounts" id="officeLiveCounts" aria-live="polite"></div>
    </div>
    <div class="officeViewport" aria-label="可横向浏览的像素办公室地图">
      <div class="officeGame" id="officeGameScene"><div class="roomWall" aria-hidden="true"><span class="roomWindow"></span><span class="roomClock">◷</span><span class="roomWindow"></span></div><div class="roomRug managerRug"></div><div class="roomRug complianceRug"></div><span class="roomPlant leftPlant" aria-hidden="true">♣</span><span class="roomPlant rightPlant" aria-hidden="true">♣</span>
        <div class="roleFlag manager"><i></i>MANAGER · 4</div>
        <div class="roleFlag compliance"><i></i>COMPLIANCE · 4</div>
        <div class="handoffRoute" aria-hidden="true"><span>HANDOFF</span><i></i></div>
        <div class="gameSeats" id="gameSeats">
          ${[0,1,2,3].map(i=>seatMarkup('manager',i)).join('')}
          ${[0,1,2,3].map(i=>seatMarkup('compliance',i)).join('')}
        </div>
        <div class="gameEvent" id="officeEvent"><span class="eventLamp"></span><b>办公室已开门</b><span id="officeEventText">等待真实实验事件</span></div>
        <div class="queueFlag" id="officeQueueFlag">队列 0</div>
        <div class="officeActors" id="officeActors" aria-live="polite"></div>
      </div>
    </div>
    <div class="gameOfficeFooter">
      <div class="officeLegend"><span><i class="working"></i>工作中</span><span><i class="waiting"></i>等交接</span><span><i class="walking"></i>交接中</span><span><i class="failed"></i>失败</span></div>
      <div class="officeDisclosure">自言自语与交接台词仅由 CASE、节点、phase、调用/工具计数和 A/V/E/D 条件生成；不展示或猜测隐藏推理。点击角色可查看该 run 的正式记录。</div>
    </div>`;
  stats.insertAdjacentElement('afterend',office);

  const scene=document.getElementById('officeGameScene');
  const actors=document.getElementById('officeActors');
  const seatElements={
    manager:[...office.querySelectorAll('.gameSeat.role-manager')],
    compliance:[...office.querySelectorAll('.gameSeat.role-compliance')]
  };
  for(const role of ['manager','compliance']){
    seatElements[role].forEach(seat=>seat.addEventListener('click',()=>{
      if(seat.dataset.runId)showDetail(seat.dataset.runId);
    }));
  }

  function activeRows(){
    return (live.rows||[]).filter(row=>row.status==='running').sort((a,b)=>(a.index||0)-(b.index||0));
  }
  function syncLanes(rows){
    const active=new Set(rows.map(row=>row.run_id));
    for(let i=0;i<laneRunIds.length;i++)if(laneRunIds[i]&&!active.has(laneRunIds[i]))laneRunIds[i]=null;
    for(const row of rows){
      if(laneRunIds.includes(row.run_id))continue;
      const open=laneRunIds.indexOf(null);
      if(open<0)break;
      laneRunIds[open]=row.run_id;
    }
    const byId=new Map(rows.map(row=>[row.run_id,row]));
    return laneRunIds.map(id=>id?byId.get(id)||null:null);
  }
  function currentCase(row){return row?.case_id||row?.run_id||'当前 run'}
  function conditionText(row){return [row?.a,row?.v,row?.d,row?.memo].filter(Boolean).join(' / ')}
  function auditLine(row,role){
    const name=currentCase(row),calls=Number(row.calls||0),tools=Number(row.tools||0);
    const requestNumber=Math.max(1,calls+(row.phase==='requesting'?1:0));
    if(!row.node)return `${name}：材料已装载，等待首个 Manager 请求。`;
    if(role==='manager'){
      if(row.phase==='requesting')return `${name}：第 ${requestNumber} 次模型请求中；工具记录 ${tools}。`;
      if(row.phase==='returned')return `${name}：第 ${calls} 次回复已返回；工具记录 ${tools}，准备下一步。`;
      return `${name}：Manager 节点处理中；调用 ${calls}，工具 ${tools}。`;
    }
    if(row.phase==='requesting')return `${name}：第 ${requestNumber} 次复核请求中；工具记录 ${tools}。`;
    if(row.phase==='returned')return `${name}：复核回复已返回；累计调用 ${calls}，工具 ${tools}。`;
    return `${name}：Compliance 节点处理中；调用 ${calls}，工具 ${tools}。`;
  }
  function idleMeta(role){
    const counters=live.counters||{},waiting=Number(counters.waiting||0);
    const ended=!!live.session&&!live.busy&&['finished','stopped','error'].includes(live.session.status);
    if(live.busy&&waiting)return {state:'waiting',status:'候场',metric:`队列 ${waiting}`,speech:''};
    if(live.busy)return {state:'idle',status:'待命',metric:role==='manager'?'等新 run':'等 Manager',speech:''};
    if(ended)return {state:'done',status:'收工',metric:`完成 ${counters.completed||0} · 失败 ${counters.failed||0}`,speech:''};
    if((draft?.total||0)>0)return {state:'waiting',status:'等开始',metric:`已选 ${selected.size||0}`,speech:''};
    return {state:'idle',status:'休息',metric:'暂无 run',speech:''};
  }
  function seatMeta(role,row){
    if(!row)return idleMeta(role);
    const calls=Number(row.calls||0),tools=Number(row.tools||0),node=row.node||'manager';
    if(role===node||(!row.node&&role==='manager'))return {
      state:'working',status:row.phase==='requesting'?'请求中':row.phase==='returned'?'整理回复':'处理中',
      metric:`${currentCase(row)} · ${calls} call / ${tools} tool`,speech:auditLine(row,role)
    };
    if(role==='compliance'&&node==='manager')return {
      state:'waiting',status:'等交接',metric:currentCase(row),speech:`${currentCase(row)}：等待 Manager 正式交接。`
    };
    return {state:'handed',status:'已交接',metric:currentCase(row),speech:''};
  }
  function updateSeat(role,index,row){
    const seat=seatElements[role][index],meta=seatMeta(role,row),person=roster[role][index];
    seat.classList.remove('is-idle','is-waiting','is-working','is-handed','is-done','is-failed');
    seat.classList.add('is-'+meta.state);
    seat.dataset.runId=row?.run_id||'';
    seat.setAttribute('aria-disabled',row?'false':'true');
    seat.querySelector('.seatPlate em').textContent=meta.status;
    seat.querySelector('.seatMetric').textContent=meta.metric;
    const speech=seat.querySelector('.gameSpeech');
    speech.querySelector('b').textContent=meta.speech;
    speech.classList.toggle('is-visible',!!meta.speech&&meta.state==='working');
    const label=row?`${person.code} ${person.name}，${meta.status}，${meta.metric}。${meta.speech}`:`${person.code} ${person.name}，${meta.status}，${meta.metric}`;
    seat.setAttribute('aria-label',label);
  }
  function setEvent(title,text,tone='normal'){
    const event=document.getElementById('officeEvent');
    event.dataset.tone=tone;
    event.querySelector('b').textContent=title;
    document.getElementById('officeEventText').textContent=text;
  }
  function isRecent(value,windowMs=15000){
    const timestamp=Date.parse(value||'');
    return Number.isFinite(timestamp)&&Date.now()-timestamp>=-2000&&Date.now()-timestamp<windowMs;
  }
  function handoffKey(row){
    return [live.session?.experiment_id||'session',row.run_id,row.handoff_at||row.role_started_at||row.activity_updated_at||'handoff'].join('|');
  }
  function dialogueElement(kind,text,x,y){
    const bubble=document.createElement('div');
    bubble.className='handoffDialogue '+kind;
    bubble.style.left=x+'px';bubble.style.top=y+'px';
    bubble.innerHTML=`<small>${kind==='manager'?'MANAGER':'COMPLIANCE'}</small><b>${esc(text)}</b>`;
    actors.appendChild(bubble);
    return bubble;
  }
  function wait(ms){return new Promise(resolve=>setTimeout(resolve,ms))}
  async function runHandoff(row,lane,key){
    if(activeHandoffs.has(key)||seenHandoffs.has(key))return;
    activeHandoffs.add(key);rememberHandoff(key);
    const managerSeat=seatElements.manager[lane],complianceSeat=seatElements.compliance[lane];
    const managerAgent=managerSeat.querySelector('.gameAgent'),complianceAgent=complianceSeat.querySelector('.gameAgent');
    const sceneRect=scene.getBoundingClientRect(),startRect=managerAgent.getBoundingClientRect(),endRect=complianceAgent.getBoundingClientRect();
    if(!sceneRect.width||!startRect.width||!endRect.width){activeHandoffs.delete(key);return}
    const start={x:startRect.left-sceneRect.left+startRect.width/2,y:startRect.top-sceneRect.top+startRect.height/2};
    const end={x:endRect.left-sceneRect.left+endRect.width/2,y:endRect.top-sceneRect.top+endRect.height/2};
    const dx=end.x-start.x,dy=end.y-start.y,midX=sceneRect.width*.5-start.x;
    const walker=document.createElement('div');
    walker.className=`handoffWalker avatar-${roster.manager[lane].avatar}`;
    walker.style.left=start.x+'px';walker.style.top=start.y+'px';
    walker.innerHTML=`<span class="gameAgent is-walking" aria-hidden="true">${figureMarkup()}</span>`;
    actors.appendChild(walker);managerSeat.classList.add('is-away');
    setEvent('正在交接',`${currentCase(row)} · ${roster.manager[lane].name} 正前往 ${roster.compliance[lane].name} 的工位`,'handoff');
    const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
    try{
      if(!reduced){
        const outward=walker.animate([
          {transform:'translate(0,0)'},
          {transform:`translate(${midX}px,${dy*.18}px)`,offset:.48},
          {transform:`translate(${dx}px,${dy}px)`}
        ],{duration:1900,easing:'steps(16,end)',fill:'forwards'});
        await outward.finished;
      }else walker.style.transform=`translate(${dx}px,${dy}px)`;
      const managerText=`${currentCase(row)} 的 Manager 阶段完成；日志现有 ${Number(row.calls||0)} 次调用、${Number(row.tools||0)} 次工具。`;
      const complianceText=`收到 ${currentCase(row)}。按 ${conditionText(row)||'本轮冻结条件'} 继续核查。`;
      const bubbleTop=Math.max(54,end.y-108),bubbleWidth=Math.min(215,sceneRect.width*.22);
      const managerBubble=dialogueElement('manager',managerText,Math.max(10,end.x-bubbleWidth-24),bubbleTop);
      const complianceBubble=dialogueElement('compliance',complianceText,Math.min(sceneRect.width-bubbleWidth-10,end.x+24),bubbleTop+42);
      managerBubble.style.width=bubbleWidth+'px';complianceBubble.style.width=bubbleWidth+'px';
      setEvent('交接对话',`${roster.manager[lane].name} → ${roster.compliance[lane].name} · ${currentCase(row)}`,'handoff');
      await wait(reduced?1200:3400);
      managerBubble.remove();complianceBubble.remove();
      if(!reduced){
        walker.style.transform=`translate(${dx}px,${dy}px)`;
        const returning=walker.animate([
          {transform:`translate(${dx}px,${dy}px)`},
          {transform:`translate(${midX}px,${dy*.18}px)`,offset:.52},
          {transform:'translate(0,0)'}
        ],{duration:1500,easing:'steps(14,end)',fill:'forwards'});
        await returning.finished;
      }
      setEvent('交接完成',`${currentCase(row)} 已进入 Compliance 节点`,'normal');
    }catch(_){
      setEvent('交接已记录',`${currentCase(row)} 已进入 Compliance 节点`,'normal');
    }finally{
      walker.remove();managerSeat.classList.remove('is-away');activeHandoffs.delete(key);
    }
  }
  function detectChanges(rows,lanes){
    for(const row of rows){
      const previous=snapshots.get(row.run_id),lane=lanes.findIndex(candidate=>candidate?.run_id===row.run_id);
      const changedToCompliance=row.node==='compliance'&&previous?.node==='manager';
      const freshlyRecorded=row.node==='compliance'&&isRecent(row.handoff_at||row.role_started_at);
      const key=handoffKey(row);
      if(lane>=0&&(changedToCompliance||(!previous&&freshlyRecorded))&&!seenHandoffs.has(key))void runHandoff(row,lane,key);
      else if(previous&&(previous.phase!==row.phase||previous.calls!==row.calls||previous.tools!==row.tools||previous.node!==row.node)){
        const role=row.node==='compliance'?'compliance':'manager',person=roster[role][Math.max(0,lane)];
        setEvent(`${person?.name||role} · ${currentCase(row)}`,auditLine(row,role),'normal');
      }
      snapshots.set(row.run_id,{node:row.node,phase:row.phase,calls:row.calls,tools:row.tools,status:row.status});
    }
    for(const id of [...snapshots.keys()])if(!rows.some(row=>row.run_id===id))snapshots.delete(id);
  }
  function renderPixelOffice(){
    const rows=activeRows(),lanes=syncLanes(rows);
    for(let lane=0;lane<4;lane++){
      updateSeat('manager',lane,lanes[lane]);
      updateSeat('compliance',lane,lanes[lane]);
    }
    const managerCount=rows.filter(row=>(row.node||'manager')==='manager').length;
    const complianceCount=rows.filter(row=>row.node==='compliance').length;
    const counters=live.counters||{},waiting=Number(counters.waiting||0),failed=Number(counters.failed||0),overflow=Math.max(0,rows.length-4);
    document.getElementById('officeLiveCounts').innerHTML=`<span class="officeCount working"><i></i>工作 ${rows.length}</span><span class="officeCount waiting"><i></i>排队 ${waiting}</span><span class="officeCount failed"><i></i>失败 ${failed}</span>`;
    document.getElementById('officeQueueFlag').textContent=`M ${managerCount} → C ${complianceCount}${overflow?' · 后台 +'+overflow:''}`;
    document.getElementById('officeSubtitle').textContent=live.session?`${live.session.experiment_id} · ${live.busy?'真实事件实时刷新':'本轮记录已保留'}`:`真实实验驱动 · 当前已选 ${selected.size||0} 轮`;
    if(!rows.length&&!activeHandoffs.size){
      if(live.busy)setEvent('等待调度',waiting?`队列中还有 ${waiting} 个 run`:'当前没有在途 run','normal');
      else if(live.session)setEvent('本轮结束',`完成 ${counters.completed||0} · 失败 ${failed} · 记录已保留`,'normal');
      else setEvent('办公室已开门','等待真实实验事件','normal');
    }
    detectChanges(rows,lanes);
  }

  const originalRenderOverview=renderOverview;
  renderOverview=function(...args){const result=originalRenderOverview(...args);renderPixelOffice();return result};
  renderPixelOffice();
})();
