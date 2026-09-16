"use strict";
let token = "", view = "issues", page = 0, status = "", overview = null;
const $ = id => document.getElementById(id);
const names = {issues:"问题总览",proposals:"修复审核",commits:"仓库动态",faqs:"常见问题",blacklist:"黑名单",jobs:"任务队列",audit:"操作记录"};
const labels = {open:"待处理",deferred:"暂缓处理",resolved:"上游已修复",archived:"已归档"};
function el(tag, text, cls) {const n=document.createElement(tag); if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function button(text, action, cls) {const b=el("button",text,cls);b.onclick=()=>Promise.resolve().then(action).catch(fail);return b;}
function fail(e){$("error").hidden=false;$("error").textContent=e.message||String(e);}
function date(t){return new Date(t*1000).toLocaleString("zh-CN");}
async function api(path, method="GET", body){
 const r=await fetch("api/"+path,{method,headers:{Authorization:"Bearer "+token,"Content-Type":"application/json"},body:body===undefined?undefined:JSON.stringify(body),cache:"no-store"});
 const data=await r.json();if(!r.ok)throw new Error(typeof data.detail==="string"?data.detail:JSON.stringify(data.detail));return data;
}
function badge(s){return el("span",labels[s]||s,"badge "+s);}
function link(url,text){const a=el("a",text);if(/^https:\/\/github\.com\//.test(url)){a.href=url;a.target="_blank";a.rel="noopener noreferrer";}return a;}
function empty(text="暂无记录"){const n=el("div",undefined,"empty");n.append(el("strong",text),el("span","新记录出现后会显示在这里。"));$("content").append(n);}
function pagination(length,size=100,total=null){const p=el("div",undefined,"pagination");const prev=button("上一页",()=>{page--;return load();});prev.disabled=page===0;const next=button("下一页",()=>{page++;return load();});next.disabled=total===null?length<size:(page+1)*size>=total;p.append(prev,el("span",`第 ${page+1} 页`,"muted"),next);$("content").append(p);}
function metric(value,title,note){const n=el("div",undefined,"metric");n.append(el("span",title),el("strong",String(value)),el("small",note));return n;}
function table(headers, rows){const wrap=el("div",undefined,"table-wrap"),t=el("table"),head=el("tr");headers.forEach(x=>head.append(el("th",x)));const thead=el("thead");thead.append(head);t.append(thead);const body=el("tbody");rows.forEach(cells=>{const row=el("tr");cells.forEach(c=>{const td=el("td");td.append(c instanceof Node?c:document.createTextNode(String(c)));row.append(td)});body.append(row)});t.append(body);wrap.append(t);$("content").append(wrap);}
async function load(){
 if(!token)return;$("error").hidden=true;
 overview=await api("overview");$("login").hidden=true;$("connection").textContent="● 管理员已连接";
 $("stats").replaceChildren(metric(overview.issues,"问题总数","多个仓库共用的问题库"),metric(overview.reporters,"反馈成员","按 QQ 去重统计"),metric(overview.reports,"有效反馈","保留原文与设备信息"),metric(overview.statuses.find(s=>s.status==="resolved")?.n||0,"上游已修复","已确认代码层面的修复"));
 $("heading").textContent=names[view];$("toolbar").replaceChildren();$("content").replaceChildren();
 if(!overview.ai_configured)$("content").append(el("div","AI 尚未配置，反馈会等待处理。请填写 AI 地址、密钥和模型后重启。","status-note"));
 const vision=overview.vision;
 if(vision){const controls=el("div",undefined,"panel"),models=el("select");models.setAttribute("aria-label","视觉模型");
  vision.models.forEach(m=>{const o=el("option",m);o.value=m;models.append(o)});models.value=vision.model;
  controls.append(el("h3",`截图视觉分析 · ${vision.enabled?"已开启":"已关闭"}`),el("p","仅分析与反馈同条发送或通过 /反馈补图 明确关联的截图。切换后立即生效。","muted"),models,
   button("保存模型",async()=>{await api("vision","PUT",{enabled:vision.enabled,model:models.value});await load();}),
   button(vision.enabled?"关闭视觉分析":"开启视觉分析",async()=>{await api("vision","PUT",{enabled:!vision.enabled,model:models.value||null});await load();}));
  if(!vision.configured)controls.append(el("p","请先在配置中填写视觉模型和 API 密钥。","muted"));$("toolbar").append(controls);}
 await renderers[view]();
}
async function issueDetail(id,offset=0){
 const d=await api(`issues/${id}?offset=${offset}`),box=$("detail-content");box.replaceChildren();box.append(el("div","问题 #"+id,"eyebrow"),el("h2",d.issue.title),badge(d.issue.status),el("p",d.issue.summary));
 if(d.issue.resolution)box.append(el("pre",d.issue.resolution));
 if(d.screenshots?.length){box.append(el("h3","关联截图的视觉分析"));d.screenshots.forEach(s=>{const a=JSON.parse(s.analysis),n=el("div",undefined,"report");n.append(el("div",`截图记录 #${s.id} · 反馈 #${s.report_id} · QQ ${s.qq} · 模型 ${s.model}`,"muted"),el("p",a.related&&a.confidence>=0.85?"已关联问题":"关联不明确，仅保存分析记录"),el("pre",s.original),el("p",a.summary),el("pre",a.visible_text||"未识别到文字"));a.observations.forEach(o=>n.append(el("p",o)));if(a.limitations)n.append(el("p",a.limitations,"muted"));box.append(n)});}
 const text=el("textarea");text.value=d.issue.comment;text.placeholder="管理员评论：后续同类反馈会直接收到此说明";
 const select=el("select");["open","deferred","archived"].forEach(s=>{const o=el("option",labels[s]);o.value=s;select.append(o)});select.value=d.issue.status==="resolved"?"open":d.issue.status;
 box.append(text);const actions=el("div",undefined,"actions");actions.append(select,button("保存评论与状态",async()=>{await api(`issues/${id}/comment`,"PUT",{comment:text.value,status:select.value});await issueDetail(id,offset);await load();},"primary"),button("重新打开",async()=>{await api(`issues/${id}/reopen`,"POST");await issueDetail(id,offset);await load();}));box.append(actions,el("h3",`反馈原文 · ${d.total} 条`));
 d.reports.forEach(r=>{const n=el("div",undefined,"report");n.append(el("div",`反馈 #${r.id} · QQ ${r.qq} · 群 ${r.group_id} · ${date(r.created)}`,"muted"),el("p",r.device+" / "+r.browser),el("pre",r.original),button("删除这条反馈",async()=>{if(!confirm(`删除 QQ ${r.qq} 的反馈 #${r.id}？`))return;await api("reports/delete","POST",{qq:r.qq,ids:[r.id]});await issueDetail(id,offset);await load();},"danger"));box.append(n)});
 const pager=el("div",undefined,"actions");if(offset>0)pager.append(button("上一页原文",()=>issueDetail(id,offset-100)));if(offset+100<d.total)pager.append(button("下一页原文",()=>issueDetail(id,offset+100)));box.append(pager);if(!$("detail").open)$("detail").showModal();
}
const renderers={
 async issues(){
  const bar=el("div",undefined,"toolbar"),s=el("select");[["","全部状态"],...Object.entries(labels)].forEach(([v,t])=>{const o=el("option",t);o.value=v;s.append(o)});s.value=status;s.onchange=()=>{status=s.value;page=0;load().catch(fail)};bar.append(el("span","优先级按不同 QQ 的反馈人数排序","muted"),s);$("toolbar").append(bar);
  const d=await api(`issues?status=${status}&limit=50&offset=${page*50}`);if(!d.items.length)return empty("还没有符合条件的问题");
  table(["问题","分类","影响人数 ↓","状态","操作"],d.items.map(i=>{const title=el("div",undefined,"title");title.append(el("span",`#${i.id} `,"id"),document.createTextNode(i.title),el("small",i.summary));return [title,i.category,el("span",String(i.reporters),"count"),badge(i.status),button("查看详情",()=>issueDetail(i.id))]}));pagination(d.items.length,50,d.total);
 },
 async proposals(){const d=await api(`proposals?offset=${page*100}`);$("toolbar").append(el("p","检查代码证据后确认；确认会通知该问题的反馈人。高置信度自动处理的记录可在操作记录中查询。","muted"));if(!d.length)return empty("没有待审核的修复建议");d.forEach(p=>{const n=el("article",undefined,"card");n.append(el("h3",`#${p.issue_id} ${p.title}`),el("p",`置信度 ${Math.round(p.confidence*100)}% · ${p.repo}`),el("p",p.explanation),el("pre",JSON.stringify(JSON.parse(p.evidence),null,2)),link(`https://github.com/${p.repo}/commit/${p.sha}`,"查看提交"));const a=el("div",undefined,"actions");["accept","reject"].forEach(action=>a.append(button(action==="accept"?"确认修复并通知":"拒绝建议",async()=>{if(action==="accept"&&!confirm("确认该问题已在上游代码修复，并通知反馈人？"))return;await api(`proposals/${p.id}`,"POST",{action});await load();},action==="accept"?"primary":"")));n.append(a);$("content").append(n)});pagination(d.length);},
 async commits(){overview.repos.forEach(r=>{const n=el("div",undefined,"card");n.append(el("h3",r.key),el("p",`轮询间隔 ${r.poll_seconds} 秒 · 最近检查 ${r.state?date(r.state.checked):"尚未检查"}`));if(r.state?.error){n.append(el("p",r.state.error,"danger"),button("重置监视基线",async()=>{if(!confirm("重置后将从下一次轮询的 HEAD 开始，跳过旧历史。确定？"))return;await api("repos/reset","POST",{repo_key:r.key});await load();}));}$("content").append(n)});if(!overview.monitor_enabled)$("content").append(el("p","Commit 监视插件未启用。"));const d=await api(`commits?offset=${page*100}`);d.forEach(c=>{const n=el("div",undefined,"card");n.append(el("h3",c.repo),el("p",c.summary),link(c.url,c.sha.slice(0,12)),el("p",date(c.created),"muted"));$("content").append(n)});if(!d.length)empty("暂无新提交摘要");pagination(d.length);},
 async faqs(){
  const d=await api("faqs");function editor(f){const n=el("div",undefined,"card"),q=el("textarea"),a=el("textarea"),active=el("input");q.placeholder="问题与适用设备/浏览器";q.value=f?.question||"";a.placeholder="明确的解决步骤";a.value=f?.answer||"";active.type="checkbox";active.checked=f?!!f.enabled:true;const label=el("label","启用 ");label.append(active);n.append(el("h3",f?`FAQ #${f.id}`:"添加常见问题"),q,a,label,button("保存",async()=>{await api(f?`faqs/${f.id}`:"faqs",f?"PUT":"POST",{question:q.value,answer:a.value,enabled:active.checked});await load();},"primary"));return n;}$("content").append(editor(null));d.forEach(f=>$("content").append(editor(f)));
 },
 async blacklist(){const panel=el("div",undefined,"panel"),qq=el("input"),reason=el("input");qq.placeholder="QQ 号";reason.placeholder="拉黑原因";const a=el("div",undefined,"actions");a.append(qq,reason,button("拉黑成员",async()=>{await api("blacklist","POST",{qq:qq.value,reason:reason.value});await load();},"danger"));panel.append(el("h3","禁止成员与机器人交互"),a);const delqq=el("input"),ids=el("input");delqq.placeholder="要删除反馈的 QQ";ids.placeholder="反馈 ID，逗号分隔；全部填 all";const b=el("div",undefined,"actions");b.append(delqq,ids,button("删除反馈",async()=>{const all=ids.value.trim()==="all";if(!confirm(`删除 QQ ${delqq.value} 的${all?"全部":"指定"}反馈？`))return;await api("reports/delete","POST",{qq:delqq.value,all,ids:all?null:ids.value.split(",").map(Number)});await load();},"danger"));panel.append(el("h3","删除某个成员的部分 / 全部反馈"),b);$("content").append(panel);const d=await api("blacklist");table(["QQ","原因","操作者","操作"],d.map(b=>[b.qq,b.reason,b.actor,button("手动解封",async()=>{await api(`blacklist/${b.qq}`,"DELETE");await load();})]));},
 async jobs(){const d=await api(`jobs?offset=${page*100}`);if(!d.length)return empty("所有任务均已处理");table(["ID / 类型","状态","失败次数","错误","操作"],d.map(j=>[`${j.id} / ${j.kind}`,j.state,j.attempts,j.error||"—",j.state==="failed"?button("重试",async()=>{await api(`jobs/${j.id}/retry`,"POST");await load();}):"等待处理"]));pagination(d.length);},
 async audit(){const d=await api(`audit?offset=${page*100}`);if(!d.length)return empty();table(["时间","操作者","操作","详情"],d.map(a=>[date(a.created),a.actor,a.action,el("pre",a.detail)]));pagination(d.length);}
};
$("login-form").onsubmit=e=>{e.preventDefault();token=$("token").value;$("token").value="";load().catch(fail)};
$("refresh").onclick=()=>load().catch(fail);$("logout").onclick=()=>location.reload();$("close-detail").onclick=()=>$("detail").close();
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{view=b.dataset.view;page=0;document.querySelectorAll("nav button").forEach(x=>x.classList.toggle("active",x===b));load().catch(fail)});
