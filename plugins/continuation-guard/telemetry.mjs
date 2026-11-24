/** Read-only observed event timing. No scheduler, process polling or task status inference. */
export function createTelemetry(now=Date.now){
  const rows=new Map();
  const recent=new Map();
  const fresh=()=>({started:null,steps:new Map(),tools:new Map(),modelMs:0,toolMs:0,firstTokenMs:0,firstTokenCount:0,stepCount:0,toolCount:0,toolFailures:0,retries:0,compactions:0,compactionMs:0,compactionStart:null,jobs:0,incomplete:false});
  const duration=(start,t)=>typeof start==='number'&&t>=start?t-start:null;
  function observe(type,id,p={}){
    if(!id)return null;
    const t=now();
    if(type==='session.execution.started'){
      if(!rows.has(id))rows.set(id,{...fresh(),started:t});
      return null;
    }
    const row=rows.get(id);if(!row)return null;
    const sid=p.stepID??p.stepId??p.step?.id??p.messageID??null;
    const tid=p.toolCallID??p.toolCallId??p.callID??p.tool?.id??p.id??null;
    if(type==='session.step.started'&&sid&&!row.steps.has(sid)){row.steps.set(sid,{at:t,first:false});row.stepCount++;}
    if(type==='session.step.first-token'&&sid&&row.steps.has(sid)){
      const step=row.steps.get(sid);if(!step.first){row.firstTokenMs+=duration(step.at,t)??0;row.firstTokenCount++;step.first=true;}
    }
    if(['session.step.ended','session.step.failed'].includes(type)&&sid&&row.steps.has(sid)){row.modelMs+=duration(row.steps.get(sid).at,t)??0;row.steps.delete(sid);}
    if(type==='session.tool.called'&&tid&&!row.tools.has(tid)){row.tools.set(tid,t);row.toolCount++;}
    if(['session.tool.success','session.tool.failed'].includes(type)&&tid&&row.tools.has(tid)){row.toolMs+=duration(row.tools.get(tid),t)??0;row.tools.delete(tid);if(type.endsWith('failed'))row.toolFailures++;}
    if(type==='session.retry.scheduled')row.retries++;
    if(type==='session.compaction.started'&&row.compactionStart===null){row.compactions++;row.compactionStart=t;}
    if(['session.compaction.ended','session.compaction.failed'].includes(type)&&row.compactionStart!==null){row.compactionMs+=duration(row.compactionStart,t)??0;row.compactionStart=null;}
    if(type==='experimental.job.updated')row.jobs++;
    if(['session.execution.succeeded','session.execution.failed','session.execution.interrupted'].includes(type)){
      const result={sessionId:id,outcome:type.split('.').at(-1),wallMs:duration(row.started,t),modelStepMs:row.modelMs,toolCallMs:row.toolMs,meanFirstTokenMs:row.firstTokenCount?Math.round(row.firstTokenMs/row.firstTokenCount):null,steps:row.stepCount,tools:row.toolCount,toolFailures:row.toolFailures,retries:row.retries,compactions:row.compactions,compactionMs:row.compactionMs,jobUpdates:row.jobs,incomplete:row.incomplete||row.steps.size>0||row.tools.size>0||row.compactionStart!==null,overlappingDurations:true};
      rows.delete(id);recent.set(id,result);return result;
    }
    if((type.startsWith('session.step.')&&!sid)||(type.startsWith('session.tool.')&&!tid))row.incomplete=true;
    return null;
  }
  return {observe,markGap(){for(const r of rows.values())r.incomplete=true;},forget(id){rows.delete(id);recent.delete(id);},reset(){rows.clear();recent.clear();},recent(id){return recent.get(id)??null;}};
}
