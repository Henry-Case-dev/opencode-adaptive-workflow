/** Deterministic decisions. No user-message parsing, project allowlists or hardcoded paths. */
export const TERMINAL = new Set(['VERIFIED', 'DONE', 'COMPLETED', 'CANCELLED', 'USER_EXCLUDED', 'N/A_WITH_REASON']);
export const PASSIVE = new Set(['WAITING_EXTERNAL', 'BLOCKED_REAL_HUMAN_GATE', 'BLOCKED', 'WAITING_OWNER']);

export function normalizePath(value) {
  const raw = String(value ?? '').trim().replaceAll('\\', '/').replace(/\/+$/, '');
  return /^[a-zA-Z]:\//.test(raw) ? raw.toLowerCase() : raw;
}
/** Machine fields are taken from the current compact header, never append-only history. */
export function readMachineHeader(workflowText) {
  if (typeof workflowText !== 'string') return {ok:false,reason:'checkpoint-unavailable'};
  // The compact header precedes the first second-level section; historical
  // notes below it are not machine state. An older flat checkpoint still works.
  const header=workflowText.split(/\r?\n(?=##\s)/,1)[0];
  const fields={};
  for (const line of header.split(/\r?\n/)) {
    const match=line.match(/^(Status|Human gate|Next execution):[ \t]*([^\r\n]*)$/i);
    if(!match)continue;
    const key=match[1].toLowerCase();
    (fields[key]??=[]).push(match[2].trim());
  }
  const machineValue = raw => {
    if (typeof raw !== 'string') return null;
    const match=raw.match(/^([a-z][a-z0-9_-]*)(?:[ \t]*\([^()\r\n]*\))?$/i);
    return match?match[1].toLowerCase():null;
  };
  // A second Human gate is never safe to reconcile automatically; even if
  // both say 'none', competing writers violated the checkpoint contract.
  if((fields['human gate']?.length??0)!==1)
    return {ok:false,reason:(fields['human gate']?.length??0)>1?'human-gate-duplicate':'human-gate'};
  const human=machineValue(fields['human gate'][0]);
  if(human!=='none')return {ok:false,reason:'human-gate'};
  const statuses=(fields.status??[]).map(machineValue);
  const executions=(fields['next execution']??[]).map(machineValue);
  const knownStatuses=new Set(['in_progress','done','waiting_external','blocked']);
  const knownExecutions=new Set(['runnable','awaiting_children','none']);
  // A valid ready task cannot authorize guesses about missing or malformed
  // machine fields. Duplicate *known* values are a repairable old snapshot.
  if(!statuses.length||statuses.some(x=>!knownStatuses.has(x)))
    return {ok:false,reason:'workflow-not-active'};
  if(!executions.length||executions.some(x=>!knownExecutions.has(x)))
    return {ok:false,reason:'next-execution-missing-or-invalid'};
  return {ok:true,human,status:statuses.length===1?statuses[0]:null,
    nextExecution:executions.length===1?executions[0]:null,
    inconsistent:statuses.length!==1||executions.length!==1,
    duplicates:{status:statuses.length,execution:executions.length}};
}

const ACTIONABLE=new Set(['PENDING','PLANNED','READY','PREPARING','BUILDING','IN_PROGRESS',
  'REVIEW','REVIEWING','DELIVERY','DEPLOYING','READY_FOR_LIVE','FAILED',
  'NEEDS_FIXES','TO_DO','TODO']);
const NEW_READY=new Set(['PENDING','PLANNED','READY','PREPARING','TO_DO','TODO']);
const DEPENDENCY_SATISFIED=new Set(['VERIFIED','DONE','COMPLETED']);

/** A narrow proof of queued, unblocked engineering work. Not dispatch authority. */
export function certifiedReadyQueueIds(scope) {
  if (!Array.isArray(scope?.tasks) || !scope.tasks.length) return [];
  const ids=new Map(),positions=new Set(),ready=[];
  for(const row of scope.tasks){
    if(!row||typeof row.id!=='string'||!row.id.trim()||ids.has(row.id)||
      typeof row.status!=='string')return [];
    const status=row.status.trim().toUpperCase();
    if(!TERMINAL.has(status)&&!PASSIVE.has(status)&&!ACTIONABLE.has(status))return [];
    ids.set(row.id,row);
    if(TERMINAL.has(status)||PASSIVE.has(status))continue;
    if(Number.isSafeInteger(row.queue_position)&&row.queue_position>0){
      if(positions.has(row.queue_position))return [];
      positions.add(row.queue_position);
    }
  }
  for(const row of scope.tasks){
    const status=row.status.trim().toUpperCase();
    if(!NEW_READY.has(status))continue;
    if(!Number.isSafeInteger(row.queue_position)||row.queue_position<=0)continue;
    if(!((typeof row.priority==='string' && /^P[0-9]$/i.test(row.priority)) ||
         (Number.isInteger(row.priority)&&row.priority>=0&&row.priority<=9)))continue;
    if(!Array.isArray(row.blocked_by)||new Set(row.blocked_by).size!==row.blocked_by.length)continue;
    // Historical blocker edges remain in some old queue entries. Clear only
    // dependencies with a *verified* terminal task; missing or externally
    // documented blocker IDs and CANCELLED tasks remain blocking.
    if(row.blocked_by.some(id=> typeof id!=='string'||
      !DEPENDENCY_SATISFIED.has(String(ids.get(id)?.status??'').trim().toUpperCase())))continue;
    if(['delivered_awaiting_evidence','waiting_external','blocked','running','assigned'].includes(
       String(row.execution_state??'').toLowerCase()))continue;
    ready.push({id:row.id,position:row.queue_position});
  }
  ready.sort((a,b)=>a.position-b.position);
  return ready.map(x=>x.id);
}
export function evaluateWork(scope, workflowText) {
  if (!scope || scope.schema !== 'openworkflow-progress-scope-v1' ||
      !Array.isArray(scope.tasks) || !scope.tasks.length)
    return {ok:false,reason:'missing-or-empty-workflow-scope'};
  if(scope.tasks.some(t=>!t||typeof t.id!=='string'||!t.id.trim()||typeof t.status!=='string'))
    return {ok:false,reason:'invalid-task-status'};
  if(scope.tasks.some(t=>{
    const status=t.status.trim().toUpperCase();
    return !TERMINAL.has(status)&&!PASSIVE.has(status)&&!ACTIONABLE.has(status);
  }))return {ok:false,reason:'unknown-task-status'};
  const header=readMachineHeader(workflowText);
  if(!header.ok)return {ok:false,reason:header.reason};
  const certifiedReady=certifiedReadyQueueIds(scope);
  // The queue is canonical for whether a next *independent* task exists; a
  // stale 'done'/'none'/'awaiting_children' (or duplicate header) is only a
  // checkpoint discrepancy. Never bypass an actual human gate or child barrier.
  const rescueHandoff=certifiedReady.length>0&&
    (header.inconsistent||header.status!=='in_progress'||header.nextExecution!=='runnable');
  if(header.inconsistent&&!rescueHandoff){
    const reason=header.duplicates.status===1&&header.status===null?'workflow-not-active':
      header.duplicates.execution===0||header.duplicates.execution===1&&!['runnable','awaiting_children','none'].includes(header.nextExecution)
        ?'next-execution-missing-or-invalid':
      'checkpoint-ambiguous-no-certified-ready-work';
    return {ok:false,reason};
  }
  if(header.status==='done'&&!rescueHandoff)
    return {ok:false,reason:'workflow-not-active'};
  if(header.nextExecution==='none'&&!rescueHandoff)
    return {ok:false,reason:'no-runnable-transition'};
  const statuses=scope.tasks.map(t=>t.status.trim().toUpperCase());
  const count=statuses.filter(s=>ACTIONABLE.has(s)).length;
  if(!count)return {ok:false,reason:'no-actionable-work'};
  return {ok:true,reason:rescueHandoff?'queue-handoff-needed':'actionable-work',count,
    mode:rescueHandoff?'runnable':header.nextExecution,
    ...(rescueHandoff?{certifiedReady}:{}),
    ...(header.inconsistent?{checkpointInconsistent:true}: {})};
}
export function sessionMatches(info,id,repo,agentId) {
  if (!info || info.id !== id || info.parentID) return false;
  if (typeof info.agent === 'string' && info.agent.toLowerCase() !== agentId.toLowerCase()) return false;
  const loc = info.directory ?? info.location?.directory;
  return !loc || normalizePath(loc) === normalizePath(repo);
}
export function eventSessionId(event) {
  const p = event?.properties ?? event?.data ?? {};
  return p.sessionID ?? p.sessionId ?? p.info?.id ?? event?.sessionID ?? null;
}
export function eventReason(event) {
  const p = event?.properties ?? event?.data ?? {};
  return p.reason ?? p.error?.type ?? null;
}
export function safePermissionCount(data) {
  if (Array.isArray(data)) return data.length;
  if (Array.isArray(data?.data)) return data.data.length;
  if (Array.isArray(data?.items)) return data.items.length;
  return null;
}
export function fingerprint(scope) {
  // Deliberately exclude workflow_state.md, Git HEAD, timestamps and reports:
  // prose checkpoints and documentation commits are NOT proof that real work
  // advanced. Only actual task/intake transitions reset the retry budget.
  const tasks = scope.tasks.map(t => `${t.id}:${String(t.status).trim().toUpperCase()}`).sort();
  const admitted = certifiedReadyQueueIds(scope).sort();
  const inputs = (Array.isArray(scope.owner_inputs) ? scope.owner_inputs : [])
    .map(i => `${i?.id || ''}:${i?.reconciled === true ? 'reconciled' : 'open'}`).sort();
  return JSON.stringify([tasks,inputs,admitted]);
}
export function assessAttempt(state, sig, cfg, now) {
  if (!state?.armedSessionId || state.suspended || !state.lastSucceededAt || !state.lastStartedAt)
    return {ok:false, reason:'unarmed-or-not-succeeded'};
  if (state.lastSucceededAt < state.lastStartedAt) return {ok:false,reason:'execution-running'};
  if (state.lastSucceededAt > now+5000 || now-state.lastSucceededAt > cfg.maxIdleAgeMs)
    return {ok:false,reason:'stale-idle'};
  if (now-state.lastSucceededAt < cfg.minIdleMs) return {ok:false,reason:'idle-grace'};
  if (state.lastAttemptForEnd === state.lastSucceededAt) return {ok:false,reason:'same-completion-already-resumed'};
  if (now-(state.lastAttemptAt||0) < cfg.cooldownMs) return {ok:false,reason:'cooldown'};
  const repeat = state.signature === sig ? Number(state.attempts)||0 : 0;
  if (repeat >= cfg.maxAttemptsWithoutProgress) return {ok:false,reason:'no-progress-cap'};
  return {ok:true,repeat};
}
export function positiveInt(value, fallback,min,max) {
  return Number.isFinite(Number(value)) ? Math.max(min, Math.min(max,Math.round(Number(value)))) : fallback;
}
