import {api} from './client.js';
export const nodesAPI={list:()=>api('/nodes'),save:(data,id)=>api('/nodes'+(id?'/'+id:''),{method:id?'PUT':'POST',body:JSON.stringify(data)})};
export const scenariosAPI={list:()=>api('/scenarios'),create:data=>api('/scenarios',{method:'POST',body:JSON.stringify(data)})};
export const drillsAPI={list:()=>api('/drills'),get:id=>api('/drills/'+id),create:data=>api('/drills',{method:'POST',body:JSON.stringify(data)})};
export const quotaAPI={
 overview:()=>api('/quota/overview'),
 savePool:data=>api('/quota/pools',{method:'PUT',body:JSON.stringify(data)}),
 reserve:data=>api('/quota/reservations',{method:'POST',body:JSON.stringify(data)}),
 transfer:data=>api('/quota/transfers',{method:'POST',body:JSON.stringify(data)}),
 credit:id=>api('/quota/transfers/'+id+'/credit',{method:'POST'}),
 rollback:id=>api('/quota/transfers/'+id+'/rollback',{method:'POST'}),
 claim:()=>api('/quota/claim',{method:'POST'}),
 network:(id,data)=>api('/quota/nodes/'+id+'/network',{method:'POST',body:JSON.stringify(data)})
};
