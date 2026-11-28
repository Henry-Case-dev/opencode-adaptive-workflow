import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {statLine,clock,reportFacts,renderReport,publishedReport,createReporter,readReportState,sourcePrefixes,parseProse,containsReport} from '../report.mjs';
import {createTelemetry} from '../telemetry.mjs';
const scope={schema:'openworkflow-progress-scope-v1',tasks:[{id:'b',title:'Проверка',status:'WAITING_EXTERNAL',queue_position:2,blocked_by:['a']},{id:'a',title:'Время',status:'BUILDING',queue_position:1},{id:'c',title:'Готово',status:'VERIFIED'},{id:'d',status:'PENDING'}]};
const progress={measurable:true,requirements_measurable:true,queue_registration_complete:false,tasks:{done:1,total:4},slices:{done:1,total:4},requirements:{done:17,total:27}};
const prose={production:'Не подтверждено.',completed:'Проверено.',active:'Разработка.',next:'Проверка.',blockers:'Есть зависимости.',human:'Нет запроса.',titles:{a:'Время',b:'Проверка',d:'Неизвестная задача'}};
test('exact progress, unknown ratios and local offset',()=>{assert.match(statLine({done:17,total:27},true),/62\.9|63\.0/);assert.match(statLine({done:17,total:27},true),/\[#{12}-{8}\]/);assert.match(statLine({done:28,total:27},true),/неизвестно/);assert.match(clock(),/UTC[+-]\d\d:\d\d$/)});
test('full open queue, order, dependencies, no invented progress',()=>{const facts=reportFacts({scope,workflow:'Human gate: none'},progress);assert.deepEqual(facts.tasks.map(x=>x.id),['a','b','d']);const text=renderReport(facts,prose,Date.now(),'abc');assert.match(text,/Полная очередь \(3\)/);assert.match(text,/зависит от: Время/);assert.match(text,/Без позиции/);assert.match(text,/Порядок очереди не полностью подтверждён/);assert.match(reportFacts({scope,workflow:''},{measurable:false}).progress.requirements,/неизвестно/)});
test('only actual newest assistant publication counts',()=>{const msg={type:'assistant',id:'new',content:{type:'text',text:'FULL REPORT'}};assert.equal(publishedReport([msg],'FULL REPORT'),'new');assert.ok(!publishedReport([msg],'FULL REPORT','new'));assert.ok(!publishedReport([{...msg,type:'tool'}],'FULL REPORT'));assert.ok(!publishedReport([msg,{...msg,id:'later',content:{type:'text',text:'other'}}],'FULL REPORT'))});
test('background model never blocks reports, fresh boundary receipt, title reuse, observed revision, root isolation',async()=>{
 const repo=await fs.mkdtemp(path.join(os.tmpdir(),'report-test-'));try{
 await fs.mkdir(path.join(repo,'plans'));await fs.writeFile(path.join(repo,'plans','progress_scope.json'),JSON.stringify(scope));await fs.writeFile(path.join(repo,'plans','workflow_state.md'),'Human gate: none');
 let n=0,release,started,observed=[],root='root',time=1000;const model={providerID:'p',id:'m'};const logs=[];const inputs=[];
 const ctx={experimental:{generate:{text:async(input)=>{n++;inputs.push(input);await new Promise(r=>{release=r;started?.()});return {text:JSON.stringify(prose)}}}}};
 const pluginDir=path.dirname(fileURLToPath(new URL('../report.mjs',import.meta.url)));
 const r=createReporter(ctx,{repo,pluginDir,getRoot:()=>root,getModel:()=>model,getObserved:()=>observed,now:()=>time,progressFn:async()=>progress,log:async(e,p)=>logs.push([e,p])});
 const began=new Promise(r=>started=r);await r.prewarm(root);await began;
 const a=await r.generate(root,undefined,1);assert.match(a,/описание обновляется/);assert.equal(n,1);assert.equal(r.expected(root,1),a);assert.equal(await r.generate(root,undefined,1),a);
 release();await new Promise(r=>setImmediate(r));
 const b=await r.generate(root,undefined,1);assert.match(b,/отдельная генерация модели/);assert.notEqual(a,b);assert.equal(n,1);
 assert.equal(r.markPublished(root,1),true);time+=60000;const c=await r.generate(root,undefined,2);assert.notEqual(c,b);assert.notEqual(c.match(/Публикация: (.*)/)[1],b.match(/Публикация: (.*)/)[1]);assert.equal(r.expected(root,1),null);
 observed=[{id:'child',state:'running'}];const d=await r.generate(root,undefined,3);assert.match(d,/Запущено агентов: 1/);assert.notEqual(c.match(/Ревизия отчёта: (.*)/)[1],d.match(/Ревизия отчёта: (.*)/)[1]);
 await assert.rejects(r.generate('other'));r.reset();root='other';assert.equal(r.expected('root',3),null);release?.();await new Promise(r=>setImmediate(r));assert.equal(r.expected('other',3),null);
 }finally{await fs.rm(repo,{recursive:true,force:true})}
});
test('pending input shows registered-only counts without pretending scope is complete',()=>{
 const state={scope:{...scope,owner_inputs:[{id:'pending-amendment',kind:'correction',reconciled:false,target_task_ids:['a']}]},workflow:''};
 const facts=reportFacts(state,{measurable:false,registered_scope_progress:{tasks:{done:1,total:4},requirements:{done:17,total:27},slices:{done:1,total:4}},reason:'pending'});
 assert.match(facts.progress.requirements,/17\/27/);const text=renderReport(facts,prose);assert.match(text,/не общий процент/);assert.match(text,/pending-amendment/);
});
test('publication tolerates CRLF and trailing whitespace, not missing rows or receipt-only output',()=>{
 const expected='first\n- task\nПубликация: receipt';const msg={type:'assistant',id:'new',content:{type:'text',text:'first  \r\n- task\r\nПубликация: receipt'}};assert.equal(publishedReport([msg],expected),'new');assert.ok(!publishedReport([{...msg,content:{type:'text',text:'Публикация: receipt'}}],expected));
});
test('telemetry marks gaps and separates observed durations',()=>{let now=0;const t=createTelemetry(()=>now);t.observe('session.execution.started','a');now=5;t.observe('session.step.started','a',{stepID:'s'});now=10;t.observe('session.step.first-token','a',{stepID:'s'});now=20;t.observe('session.step.ended','a',{stepID:'s'});t.markGap();now=30;const r=t.observe('session.execution.succeeded','a');assert.equal(r.wallMs,30);assert.equal(r.modelStepMs,15);assert.equal(r.meanFirstTokenMs,5);assert.equal(r.incomplete,true)});

test('native assistantMessageID and stream boundary are measured; missing steps are unknown',()=>{let now=0;const t=createTelemetry(()=>now);t.observe('session.execution.started','a');now=10;t.observe('session.step.started','a',{assistantMessageID:'msg_a'});now=15;t.observe('session.step.first-token','a',{assistantMessageID:'msg_a'});now=20;t.observe('session.step.streamed','a',{assistantMessageID:'msg_a'});now=30;t.observe('session.step.ended','a',{assistantMessageID:'msg_a'});t.observe('session.step.ended','a',{assistantMessageID:'msg_a'});now=40;const r=t.observe('session.execution.succeeded','a');assert.equal(r.steps,1);assert.equal(r.modelStepMs,20);assert.equal(r.providerStreamMs,10);assert.equal(r.meanFirstTokenMs,5);t.observe('session.execution.started','b');assert.equal(t.observe('session.execution.succeeded','b').modelStepMs,null);});

test('moving snapshots retry once and never report stale numbers; unreadable scope is not empty queue',async()=>{
 const repo=await fs.mkdtemp(path.join(os.tmpdir(),'report-drift-'));try{
  await fs.mkdir(path.join(repo,'plans'));const wf=path.join(repo,'plans','workflow_state.md');const sc=path.join(repo,'plans','progress_scope.json');await fs.writeFile(sc,JSON.stringify(scope));await fs.writeFile(wf,'Human gate: none');let calls=0;
  const r=createReporter({}, {repo,pluginDir:path.dirname(fileURLToPath(new URL('../report.mjs',import.meta.url))),getRoot:()=> 'root',progressFn:async()=>{await fs.writeFile(wf,`Human gate: none\nTask: drift-${++calls}`);return progress;}});
  const a=await r.generate('root',undefined,1);assert.equal(calls,2);assert.match(a,/Состояние изменилось во время подсчёта/);assert.match(a,/Требования: \[\?{20}\]/);
  await fs.writeFile(sc,'{broken');const b=await r.generate('root',undefined,2);assert.match(b,/Полная очередь:\*\* не подтверждена/);assert.doesNotMatch(b,/Нет незакрытых записей/);r.reset();
 }finally{await fs.rm(repo,{recursive:true,force:true})}
});
test('concurrent same-boundary report reads coalesce; stale boundary cannot overwrite new report',async()=>{
 const repo=await fs.mkdtemp(path.join(os.tmpdir(),'report-boundary-'));try{
  await fs.mkdir(path.join(repo,'plans'));await fs.writeFile(path.join(repo,'plans','progress_scope.json'),JSON.stringify(scope));await fs.writeFile(path.join(repo,'plans','workflow_state.md'),'Human gate: none');let boundary=1,release,started,calls=0;
  const r=createReporter({}, {repo,pluginDir:path.dirname(fileURLToPath(new URL('../report.mjs',import.meta.url))),getRoot:()=> 'root',getBoundary:()=>boundary,progressFn:async()=>{if(++calls===1)await new Promise(r=>{release=r;started()});return progress;}});
  const began=new Promise(r=>started=r);const a=r.generate('root',undefined,1);const b=r.generate('root',undefined,1);const old=Promise.allSettled([a,b]);await began;boundary=2;const current=await r.generate('root',undefined,2);release();const result=await old;assert.ok(result.every(x=>x.status==='rejected'));assert.equal(calls,2);assert.equal(r.expected('root',2),current);r.reset();
 }finally{await fs.rm(repo,{recursive:true,force:true})}
});

test('partial JSON accepts valid sections independently, including no titles',()=>{
 const p=parseProse('```json\n{"active":"Проверяется работа памяти.","next":null,"production":"verified_deployed checkout_sha=abcdef123456 DDL 0"}\n```',['active','next','production']);
 assert.equal(p.active,'Проверяется работа памяти.');assert.equal(p.next,undefined);assert.equal(p.production,undefined);assert.equal(Object.keys(p.titles).length,0);
 assert.throws(()=>parseProse('not JSON'),/invalid-json/);
});
test('degraded snapshot from real screenshot-style checkpoint never dumps raw evidence and preserves queue gaps',async()=>{
 const repo=await fs.mkdtemp(path.join(os.tmpdir(),'report-readable-'));try{
 await fs.mkdir(path.join(repo,'plans'));await fs.writeFile(path.join(repo,'plans','progress_scope.json'),JSON.stringify({...scope,tasks:[{id:'clock',title:'Понимание времени',status:'REVIEW',queue_position:3},{id:'emb',status:'BUILDING',queue_position:47,source_note:'owner corrective; SIMPL-01..07 VERIFIED; MODEL-SWAP-01 pending; checkout_sha=abcdef123456'}]}));
 await fs.writeFile(path.join(repo,'plans','workflow_state.md'),`Production: 2.58.241 @ 3f42cfdd1 (branch release/embed-simplify-2.58.241); healthz verified_deployed (checkout_sha=abcdef123456), PG digests unchanged, NRestarts 0, DDL 0\nLast completed: code-only rollback 2.58.240\nActive feature: owner corrective — MODEL-SWAP-01..05 PENDING + QRNG IBM P5B\nNext execution: awaiting_children\nNext unlocks: M5B/3 REVIEW -> code-only 2.58.238\nHuman gate: none`);
 const r=createReporter({}, {repo,pluginDir:path.dirname(fileURLToPath(new URL('../report.mjs',import.meta.url))),getRoot:()=> 'root',progressFn:async()=>progress});
 const t=await r.generate('root',undefined,1);assert.match(t,/на сервере версия 2\.58\.241/);assert.doesNotMatch(t,/checkout_sha|verified_deployed|PG digests|NRestarts|MODEL-SWAP|M5B\/3|rollback|2\.58\.240/);assert.match(t,/\n- Этапы:/);assert.match(t,/\n\n\*\*Сейчас в работе\*\*/);assert.match(t,/Полная очередь \(2\)/);assert.match(t,/№3\. ID: \*\*clock\*\* — Понимание времени/);assert.match(t,/№47\. ID: \*\*emb\*\* — Название уточняется/);assert.match(t,/После получения результатов агентов/);r.reset();
 }finally{await fs.rm(repo,{recursive:true,force:true})}
});
test('one bad field cannot discard valid translation; repeat failures for unchanged fields stop after next real boundary',async()=>{
 const repo=await fs.mkdtemp(path.join(os.tmpdir(),'report-partial-'));try{
 await fs.mkdir(path.join(repo,'plans'));await fs.writeFile(path.join(repo,'plans','progress_scope.json'),JSON.stringify(scope));await fs.writeFile(path.join(repo,'plans','workflow_state.md'),'Human gate: none\nActive feature: phase one');
 let calls=0;const requests=[],logs=[];let waitResult;
 const ctx={experimental:{generate:{text:async({prompt})=>{calls++;requests.push(prompt);return {text:JSON.stringify({active:'Проверяется работа памяти.',titles:{d:'Другая задача'}})}}}}};
 const r=createReporter(ctx,{repo,pluginDir:path.dirname(fileURLToPath(new URL('../report.mjs',import.meta.url))),getRoot:()=> 'root',getModel:()=>({providerID:'p',id:'m'}),progressFn:async()=>progress,log:async(e,p)=>{logs.push([e,p]);if(e==='reporter-model-partial'||e==='report-degraded')waitResult?.();}});
 const launch=async b=>{let done=new Promise(resolve=>waitResult=resolve);await r.prewarm('root',b);await done;await new Promise(r=>setImmediate(r));};
 await launch(1);const a=await r.generate('root',undefined,1);assert.match(a,/Проверяется работа памяти/);assert.match(a,/Другая задача/);assert.equal(calls,1);assert.ok(logs.some(([e,p])=>e==='reporter-model-partial'&&p.acceptedSections===1));
 await launch(2);assert.equal(calls,2);const b=await r.generate('root',undefined,3);assert.match(b,/Проверяется работа памяти/);assert.equal(calls,2);assert.notEqual(a.match(/Публикация: (.*)/)[1],b.match(/Публикация: (.*)/)[1]);
 assert.doesNotMatch(requests[1].split('JSON data only').at(-1),/"active":\{/);r.reset();
 }finally{await fs.rm(repo,{recursive:true,force:true})}
});
test('flattened Markdown and omitted queue rows must both fail publication',()=>{
 const expected='**Отчёт**\n\nПоручения: 1/2\n\n- №1 Проверка\n- №2 Память\n\nПубликация: receipt';
 assert.ok(!containsReport(expected.replace(/\n+/g,' '),expected));assert.ok(containsReport(expected.replace(/\n/g,'\r\n'),expected));assert.ok(!containsReport('Технические детали\n'+expected,expected));assert.ok(!containsReport(expected.replace('- №2 Память\n',''),expected));assert.ok(!containsReport('Публикация: receipt',expected));
});

test('Nov 25 screenshot: operational jargon is rejected in model fields AND direct render',()=>{
 const raw='В работе 2.58.241 @ 3f42cfdd1 — выложено и проверено: health local+public 200, identity verified_deployed, схема 48/DDL 0, NRestarts 0, backup 5 609 562 112 B, диск keep=2.';
 const legs='Проверены три реальные ноги.';
 const data={production:raw,completed:legs,active:'Исправляют ошибку, из-за которой страница настроек исчезает после сохранения ключа.',next:'Повторная проверка интеграции IBM P5B R1 re-review.',blockers:'Для проверки подключения IBM нужен действующий ключ.',human:'Ничего не требуется.',titles:{a:'Улучшение понимания времени',b:'Проверка RC1-RC7',d:'Поддержка GPT-4 в настройках'}};
 const accepted=parseProse(JSON.stringify(data));
 assert.equal(accepted.production,undefined);assert.equal(accepted.completed,undefined);assert.equal(accepted.next,undefined);assert.equal(accepted.active,data.active);assert.equal(accepted.blockers,data.blockers);assert.equal(accepted.titles.b,undefined);assert.equal(accepted.titles.d,data.titles.d);
 const f=reportFacts({scope,workflow:`Production: ${raw}\nLast completed: ${legs}\nActive feature: MODEL-SWAP-01\nHuman gate: none`},progress);
 const text=renderReport(f,data,Date.now(),'revision');
 assert.match(text,/на сервере версия 2\.58\.241/);assert.match(text,/страница настроек исчезает/);
 assert.doesNotMatch(text,/health|verified_deployed|DDL|NRestarts|keep=2|3f42cfdd1|реальные ноги|RC1-RC7|P5B|re-review/);
});
test('existing checkpoint functional sentence is usable even with no model; proof suffix never shown',()=>{
 const wf='Production: 2.58.241 — Упростили проверку совместимости векторной памяти. | evidence: health local+public 200, DDL 0, SHA 3f42cfdd1\nLast completed: Бот теперь точнее понимает относительные даты. | evidence: UR-C-CLOCK 27/27\nActive feature: Исправляют ошибку сохранения ключа в настройках случайности.\nNext unlocks: Повторно проверят подключение IBM.\nJoin barriers: Для проверки квантового устройства нужен действующий ключ.\nHuman gate: none';
 const f=reportFacts({scope,workflow:wf},progress);
 const t=renderReport(f,{degraded:true},Date.now(),'revision');
 assert.match(t,/Упростили проверку совместимости векторной памяти/);assert.match(t,/Бот теперь точнее понимает относительные даты/);assert.match(t,/Исправляют ошибку сохранения ключа/);assert.match(t,/Повторно проверят подключение IBM/);assert.match(t,/нужен действующий ключ/);assert.doesNotMatch(t,/health|DDL|SHA|3f42cfdd1|UR-C-CLOCK|evidence:/);
});
test('42 open tasks retain each separate bullet and queue positions; known titles retain lookup IDs',()=>{
 const tasks=Array.from({length:42},(_,i)=>({id:'internal-task-'+i,title:'Задача разработки '+(i+1),queue_position:i+1+(i>=3?2:0),priority:i%2===0?0:'p3',status:i===0?'REVIEW':'PENDING'}));
 const f=reportFacts({scope:{schema:scope.schema,tasks},workflow:'Human gate: none'},progress);
 const t=renderReport(f,{degraded:true},Date.now(),'revision')+'\n\nПубликация: receipt';
 assert.match(t,/Полная очередь \(42\)/);assert.equal((t.match(/^- №\d+\./gm)||[]).length,42);assert.match(t,/- №6\. ID: \*\*internal-task-3\*\* — Задача разработки 4/);assert.match(t,/приоритет P0/);assert.equal((t.match(/ID: \*\*internal-task-/g)||[]).length,42);assert.match(t,/- Поручения:[^\n]+\n- Этапы:[^\n]+\n- Требования:/);
 assert.ok(!containsReport(t.replace(/\n- №/g,' - №'),t));assert.ok(!containsReport(t.replace(/\n\n/g,'\n'),t));assert.ok(!containsReport(t.replace(/^(- №6\.[^\n]*)\n/m,''),t));assert.ok(containsReport(t.replace(/\n/g,'\r\n'),t));
});


test('queue uses exact original source codes, mapped aliases, and labeled fallback without model invented prefixes',async()=>{
 const repo=await fs.mkdtemp(path.join(os.tmpdir(),'report-prefix-'));
 try{
  await fs.mkdir(path.join(repo,'plans'));
  const h='EXAMPLE PROJECT — MEMORY-DESIGN-01: Интеграция памяти';
  const sid='H1:'+crypto.createHash('sha256').update(h.toLowerCase()).digest('hex').slice(0,12)+':1';
  const taskScope={schema:scope.schema,tasks:[{id:'search-quality-01',title:'Достоверный фактчек',status:'PENDING',queue_position:9},{id:'memory-rework',title:'Улучшение памяти',status:'PENDING',queue_position:10},{id:'unknown-code-01',title:'Другая задача',status:'PENDING',queue_position:11},{id:'code-example-01',title:'Пример задачи',status:'PENDING',queue_position:12}],source_inventory:{source_path:'plans/current_task.md',heading_level:1,sections:[{id:sid,disposition:'mapped',target_task_ids:['memory-rework']}]}};
  const source='# EXAMPLE PROJECT — SEARCH-QUALITY-01 — Достоверный фактчек\n# '+h+'\n```md\n# CODE-EXAMPLE-01\n```\n';
  await fs.writeFile(path.join(repo,'plans','current_task.md'),source);await fs.writeFile(path.join(repo,'plans','progress_scope.json'),JSON.stringify(taskScope));await fs.writeFile(path.join(repo,'plans','workflow_state.md'),'Human gate: none');
  const state=await readReportState(repo);const text=renderReport(reportFacts(state,progress),{titles:{'search-quality-01':'НЕВЕРНЫЙ-CODE-99'}});
  assert.match(text,/- №9\. \*\*SEARCH-QUALITY-01\*\* — Достоверный фактчек/);
  assert.match(text,/- №10\. \*\*MEMORY-DESIGN-01\*\* — Улучшение памяти/);
  assert.match(text,/- №11\. ID: \*\*unknown-code-01\*\* — Другая задача/);
  assert.match(text,/- №12\. ID: \*\*code-example-01\*\* — Пример задачи/);
  assert.doesNotMatch(text,/CODE-EXAMPLE-01|НЕВЕРНЫЙ/);
  await fs.writeFile(path.join(repo,'plans','current_task.md'),source.replace('SEARCH-QUALITY-01','SEARCH-QUALITY-02'));
  const revised=await readReportState(repo);assert.notEqual(revised.revision,state.revision);assert.equal(revised.sourcePrefixes.has('search-quality-01'),false);
 }finally{await fs.rm(repo,{recursive:true,force:true});}
});
test('ambiguous or unlinked headings never turn a registry slug into an alleged exact prefix',()=>{
 const h='EXAMPLE PROJECT — FIRST-01 — SECOND-02';
 const id='H1:'+crypto.createHash('sha256').update(h.toLowerCase()).digest('hex').slice(0,12)+':1';
 const r={id:'alias',status:'PENDING'};
 const s={tasks:[r],source_inventory:{source_path:'plans/current_task.md',heading_level:1,sections:[{id,disposition:'mapped',target_task_ids:['alias','other']}]}};
 assert.equal(sourcePrefixes(s,'# '+h).size,0);
 s.source_inventory.sections[0].target_task_ids=['alias'];s.source_inventory.sections.push({...s.source_inventory.sections[0]});assert.equal(sourcePrefixes(s,'# '+h).size,0);
 assert.equal(sourcePrefixes({tasks:[{id:'first-01'}]},'# FIRST-010').size,0);
});
test('liveness report distinguishes suspicion, permissions and terminal mismatch from success',()=>{
 const f=reportFacts({scope,workflow:'Human gate: none',observed:[{id:'a',state:'terminal-unreconciled',role:'Builder',startedAt:1000},{id:'b',state:'silence-unverified'},{id:'c',state:'permission-wait'}]},progress);
 const out=renderReport(f,prose,19*3600000+1000);assert.match(out,/завершения хода или задания/);assert.match(out,/зависание не доказано/);assert.match(out,/ожидают разрешения/);assert.match(out,/Разработчик: с начала наблюдения прошло 19 ч 0 мин/);
});

test('finite delivery separates external proof, legacy closed reconciliation and optional observation from engineering',()=>{
 const s={...scope,tasks:[...scope.tasks,{id:'parked',title:'Проверка внешнего оборудования',status:'WAITING_EXTERNAL',queue_position:9,execution_state:'delivered_awaiting_evidence'},{id:'unchecked',title:'Старый релиз',status:'VERIFIED'},{id:'optional',title:'Оценка естественности',status:'VERIFIED'}]};
 const d={done:2,total:7,external_evidence_task_ids:['parked'],reconcile_task_ids:['unchecked'],optional_observation_task_ids:['optional']};
 const facts=reportFacts({scope:s,workflow:'Human gate: none'},{...progress,engineering_delivery:d});
 const t=renderReport(facts,{degraded:true});
 assert.match(t,/Инженерно завершено[^\n]*2\/7/);
 const [queue,external]=t.split('**Доставлено, ожидаются обязательные внешние доказательства');
 assert.match(queue,/Полная очередь \(3\)/);assert.doesNotMatch(queue,/Проверка внешнего оборудования|Старый релиз|Оценка естественности/);
 assert.match(external,/Проверка внешнего оборудования/);assert.match(external,/Сверка ранее заявленной доставки/);assert.match(external,/Старый релиз/);assert.match(external,/Необязательное наблюдение после выпуска/);assert.match(external,/Оценка естественности/);
 const unknown=renderReport(reportFacts({scope:s,workflow:''},{...progress,measurable:false,stale_sources:['changed'],engineering_delivery:d}),{});
 assert.match(unknown,/Полная очередь \(4\)/);assert.doesNotMatch(unknown,/Доставлено, ожидаются обязательные внешние доказательства/);
});
