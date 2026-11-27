// 运行日志：增量拉取事件，把 Claude / Codex 的事件流转成对话视图
import {api, apiq} from '../core/api.js';
import {toolDetail, toolSummary} from '../core/tools.js';
import {relPath} from '../core/paths.js';

export default {
  data(){ return {
      logs:{}, offsets:{}, workState:{},
      runLabel:{running:'执行中',success:'成功',error:'出错',cancelled:'已停止',interrupted:'已中断',unreported:'结果未确认'},
  }; },
  methods:{
    async pullLogs(scroll){
      if (!this.sel) return;
      if (this._pulling){ this._pullAgain=true; this._pullScroll=this._pullScroll||scroll; return; }   // 正在拉取：结束后再拉一次
      this._pulling=true;
      let ended=false;
      try{
        const box=this.$refs.logBox, atBottom = box && box.scrollHeight-box.scrollTop-box.clientHeight<80;
        const task=this.sel;
        for (const r of task.runs){
          if (this.offsets[r.id]==='done') continue;   // 已读完的运行不会再有输出（sel 里的状态可能还没刷新，不能据此重读）
          const off = typeof this.offsets[r.id]==='number' ? this.offsets[r.id] : 0;
          const res = await apiq(`/api/runs/${r.id}/events?offset=${off}`);
          if (this.sel?.id!==task.id) break;
          const arr = this.logs[r.id] || [];
          res.events.forEach(e=>this.ingest(arr,e));
          this.logs[r.id]=arr;
          if (res.status!=='running'){ arr.forEach(x=>{ if (x.live) x.live=false; }); ended = ended || r.status==='running'; }
          this.offsets[r.id] = ['running','unreported'].includes(res.status) ? res.offset : 'done';
        }
        if (scroll||atBottom) this.$nextTick(()=>{ if(this.$refs.logBox) this.$refs.logBox.scrollTop=1e9; });
      }finally{ this._pulling=false; }
      if (this._pullAgain){ const sc=this._pullScroll; this._pullAgain=this._pullScroll=false; await this.pullLogs(sc); }
      if (ended) this.tick();   // 运行刚结束：立即刷新任务状态，不等下一轮轮询
    },
    // 当前任务执行中时高频拉取新事件（只请求一个接口），让输出几乎实时出现
    pollLive(){
      if (this.sel && this.isActive(this.sel) && !document.hidden) this.pullLogs(false).catch(()=>{});
    },
    // 逐字增量：追加到同类型正在生成的那一行（正文 text / 思考 think），没有就新建
    appendLive(arr, cls, text, item){
      const last=arr[arr.length-1];
      if (last && last.live && last.cls===cls && (!item || !last.item || last.item===item)) last.text+=text;
      else arr.push({cls, text, live:true, item:item||''});
    },
    // 整条内容到达：替换正在生成的那一行，没有就新建
    finishLive(arr, cls, text, item){
      const row=[...arr].reverse().find(x=>x.cls===cls && x.live && (!item || !x.item || x.item===item));
      if (row){ row.text=text||row.text; row.live=false; return; }
      if ((text||'').trim()) arr.push({cls, text, item:item||''});
    },
    ingest(arr,e){
      if (this.ingestCodex(arr,e)) return;
      if (e.type==='wb'){
        if (e.subtype==='approval'){   // Claude / Codex 请求批准执行命令、修改文件或使用工具
          arr.push({cls:'approval', id:e.id, kind:e.kind, tool:e.tool||'', command:e.command, cwd:e.cwd, reason:e.reason, grantRoot:e.grant_root, decision:null, busy:false});
          return;
        }
        if (e.subtype==='approval_done'){ const a=arr.find(x=>x.cls==='approval' && x.id===e.id); if (a) a.decision=e.decision; return; }
        if (e.subtype==='agent_assignment' && e.text) arr.push({cls:'prompt', text:e.text});
        else if (e.subtype==='delta') this.appendLive(arr, 'text', e.text);
        else if (e.subtype==='thinking_delta') this.appendLive(arr, 'think', e.text, e.item);
        else if (e.subtype==='output_delta'){ const row=arr.find(x=>x.cls==='tool' && x.id===e.item); if (row) row.output=(row.output||'')+e.text; }
        else if (e.subtype==='plan'){   // Codex 的计划 / 待办更新：同一轮只保留最新的一份
          const text=(e.explanation ? e.explanation+'\n\n' : '')+(e.plan||[]).map(x=>({completed:'☑',inProgress:'▶'}[x.status]||'☐')+' '+x.step).join('\n');
          const row=arr.find(x=>x.cls==='tool' && x.id==='__plan');
          if (row){ row.output=text; row.summary=(e.plan||[]).length+' 项'; }
          else arr.push({cls:'tool', id:'__plan', name:'Todo', summary:(e.plan||[]).length+' 项', output:text});
        }
        else if (e.subtype==='warning') arr.push({cls:'sys', text:'⚠ '+e.text,
          agentUnreported:e.text==='CLI 事件流已结束，子会话结果未回报'});
        else if (e.subtype==='start' && e.prompt) arr.push({cls:'prompt', text:e.prompt, cwd:e.cwd, meta:this.runMeta(e), images:e.images||[]});
        else if (e.subtype==='stderr' && e.text.trim()) arr.push({cls:'err', text:e.text});
        else if (e.subtype==='error') arr.push({cls:'err', text:'工作台错误：'+e.text});
        return;
      }
      if (e.type==='system' && e.subtype==='init'){ arr.push({cls:'sys', text:`会话 ${e.session_id} · 模型 ${e.model} · 权限 ${e.permissionMode}`}); return; }
      if (e.type==='assistant'){
        for (const c of (e.message?.content||[])){
          if (c.type==='text') this.finishLive(arr, 'text', c.text);
          else if (c.type==='thinking') this.finishLive(arr, 'think', c.thinking);
          else if (c.type==='redacted_thinking') arr.push({cls:'think', text:'（这段思考被加密，CLI 没有提供明文）'});
          else if (c.type==='tool_use') arr.push({cls:'tool', id:c.id, name:c.name, summary:toolSummary(c.name,c.input), detail:toolDetail(c.name,c.input), output:''});
        }
        return;
      }
      if (e.type==='user'){
        for (const c of (e.message?.content||[])){
          if (c.type!=='tool_result') continue;
          const tool = arr.find(x=>x.cls==='tool'&&x.id===c.tool_use_id);
          const out = Array.isArray(c.content) ? c.content.map(x=>x.text||(x.type==='image'?'［图片］':'')).join('\n') : String(c.content||'');
          if (tool){ tool.output=out; tool.isErr=!!c.is_error; }
        }
        return;
      }
      if (e.type==='result') arr.push({cls:e.is_error?'result err':'result', text:e.result||'', cost:e.total_cost_usd, ms:e.duration_ms||0, turns:e.num_turns});
    },
    ingestCodex(arr,e){
      const t=e.type||'';
      if (t==='thread.started'){ arr.push({cls:'sys', text:`Codex 会话 ${e.thread_id}`}); return true; }
      if (t==='turn.started') return true;
      if (t==='turn.completed'){ arr.push({cls:'result', text:'', tokens:e.usage}); return true; }
      if (t==='turn.failed'){ arr.push({cls:'result err', text:(e.error||{}).message||'执行失败'}); return true; }
      if (t==='error'){ arr.push({cls:'err', text:e.message||JSON.stringify(e)}); return true; }
      if (!t.startsWith('item.')) return false;
      const it=e.item||{}, done=t==='item.completed';
      let row=arr.find(x=>x.cls==='tool'&&x.id===it.id);
      const tool=(name,summary,detail)=>{
        if (!row){ row={cls:'tool', id:it.id, name, summary, detail:detail||'', output:''}; arr.push(row); }
        else { row.summary=summary; if (detail) row.detail=detail; }
      };
      switch(it.type){
        case 'agent_message': if (done) this.finishLive(arr, 'text', it.text); break;
        case 'reasoning': {   // 思考：常驻进程给出摘要 + 正文，codex exec 给出 text
          if (!done) break;
          const text = it.text || [...(it.summary||[]), ...(it.content||[])].join('\n\n');
          if (text.trim()) this.finishLive(arr, 'think', text, it.id);
          else if (!arr.some(x=>x.cls==='think' && x.item===it.id)) arr.push({cls:'think', item:it.id, empty:true,
            text:'模型进行了一段思考，但没有提供可读的内容（Codex 的思考摘要设置为 none 时只返回加密内容，终端 CLI 里同样看不到）'});
          break;
        }
        case 'plan': if (done) this.finishLive(arr, 'think', '**计划**\n\n'+(it.text||''), it.id); break;
        case 'command_execution':
          tool('Bash', (it.command||'').replace(/^\/bin\/(zsh|bash) -lc /,''), it.cwd ? '目录：'+it.cwd : '');
          if (done){ row.output=it.aggregated_output||row.output||''; row.isErr=it.exit_code!==0 && it.exit_code!=null; }
          break;
        case 'file_change': {
          const ch=it.changes||[];
          tool('Edit', ch.map(c=>`${c.kind||''} ${relPath(c.path)}`).join(', '));
          const diff=ch.map(c=>c.diff ? `--- ${c.path}${c.move_path ? ' → '+c.move_path : ''}\n${c.diff}` : '').filter(Boolean).join('\n\n');
          if (diff) row.output=diff;
          break;
        }
        case 'mcp_tool_call':
          tool(`${it.server}.${it.tool}`, JSON.stringify(it.arguments||{}).slice(0,160), '参数：\n'+JSON.stringify(it.arguments||{}, null, 2));
          if (done){
            if (it.result) row.output=typeof it.result==='string' ? it.result : JSON.stringify(it.result, null, 2);
            if (it.error){ row.output=(row.output ? row.output+'\n\n' : '')+'错误：'+JSON.stringify(it.error, null, 2); row.isErr=true; }
          }
          break;
        case 'web_search': tool('WebSearch', it.query||'', it.action ? JSON.stringify(it.action, null, 2) : '');
          if (done && it.results) row.output=JSON.stringify(it.results, null, 2); break;
        case 'todo_list': tool('Todo', (it.items||[]).map(x=>(x.completed?'☑':'☐')+' '+x.text).join('  ')); break;
        case 'error': if (/unrecognized configuration setting/i.test(it.message||'')) break;
          if (done && !arr.some(x=>x.text==='⚠ '+(it.message||''))) arr.push({cls:'sys', text:'⚠ '+(it.message||'')}); break;
        case 'other':   // 没有专门格式的条目（子代理、图片、上下文压缩等）：原样显示
          if (done){ tool(it.item_type||'条目', ''); row.output=JSON.stringify(it.raw||it, null, 2); }
          break;
        default: if (done){ tool(it.type||'条目', ''); row.output=JSON.stringify(it, null, 2); }
      }
      return true;
    },
    // 一轮执行拆成：用户消息 / 过程（工具调用、中间输出，可折叠）/ 错误 / 最终回复
    runView(r){
      const a=this.logs[r.id]||[];
      const prompt=a.find(e=>e.cls==='prompt')||null;
      const notes=[], seq=[];
      for (const e of a){
        if (e.cls==='prompt') continue;
        if (e.agentUnreported && r.status!=='unreported') continue;
        if (e.cls==='err' || (e.cls.startsWith('result') && e.cls!=='result')){ if (e.text) notes.push(e); continue; }
        if (e.cls==='result'){
          const last=[...seq].reverse().find(x=>x.cls==='text');
          if (e.text && !(last && last.text.trim()===e.text.trim())) seq.push({cls:'text', text:e.text});
          continue;
        }
        if (e.cls==='text' || e.cls==='think' || e.cls==='tool' || (e.cls==='sys' && e.text.startsWith('⚠'))) seq.push(e);
      }
      let work=seq, final=[];
      if (r.status!=='running'){
        let k=-1; seq.forEach((e,i)=>{ if (e.cls!=='text') k=i; });
        work=seq.slice(0,k+1); final=seq.slice(k+1);
      }
      return {prompt, work, final, notes, tools:work.filter(e=>e.cls==='tool').length, approvals:a.filter(e=>e.cls==='approval')};
    },
    async decide(runId, a, decision){
      if (a.busy || a.decision) return;
      a.busy=true;
      try{ await api(`/api/tasks/${this.sel.id}/approval`,{method:'POST', body:{id:a.id, run_id:runId, decision}}); a.decision=decision; }
      catch(e){} finally{ a.busy=false; }
    },
    approvalText(a, running){
      if (!a.decision) return running ? '' : '已失效（未处理，已按拒绝处理）';
      return {accept:'已批准', acceptForSession:'已批准（本会话同类操作不再询问）', decline:'已拒绝', cancel:'已拒绝并停止', expired:'已失效'}[a.decision]||a.decision;
    },
    workOpen(r){ const v=this.workState[r.id]; return v===undefined ? r.status==='running' : v; },
    toggleWork(r){ this.workState[r.id]=!this.workOpen(r); },
    runMeta(e){
      if (!('model' in e)) return '';
      const eng=e.engine||'claude';
      return [e.model||'默认模型', '思考 '+(e.effort ? this.effortLabel(e.effort, eng) : (eng==='codex' && this.codexDefault(e.model) ? this.effortLabel(this.codexDefault(e.model),'codex')+'（默认）' : '默认'))].join(' · ');
    },
    runTokens(runId){ const r=(this.logs[runId]||[]).find(x=>x.tokens); return r ? `${r.tokens.input_tokens} 入 / ${r.tokens.output_tokens} 出 tokens` : ''; },
    runStatusClass(s){ return {success:'st-done',error:'st-failed',cancelled:'st-cancelled',interrupted:'st-failed',running:'st-running',unreported:'st-unreported'}[s]||'st-todo'; },
    toolIcon(n){ n=(n||'').toLowerCase();
      if (n==='bash') return 'terminal';
      if (n==='grep'||n==='glob') return 'search';
      if (/edit|write|read|notebook/.test(n)) return 'file';
      if (/web|fetch/.test(n)) return 'globe';
      if (/todo/.test(n)) return 'list';
      return 'tool'; },
    md(t){
      const html = DOMPurify.sanitize(marked.parse(t||''));
      if (!this.sel || !/(src|href)=/.test(html)) return html;
      // AI 回复里的本地路径（/Users/…、file://…、相对路径）改走 /api/tasks/:id/file，否则浏览器加载不到
      const tpl=document.createElement('template'); tpl.innerHTML=html;
      const local=u=>u && !/^(https?:|mailto:|data:|blob:|#|\/api\/|\/\/)/i.test(u);
      const fileUrl=u=>`/api/tasks/${this.sel.id}/file?path=${encodeURIComponent(decodeURI(u.replace(/[?#].*$/,'')))}`;
      tpl.content.querySelectorAll('img[src]').forEach(el=>{ const u=el.getAttribute('src'); if (local(u)) el.setAttribute('src', fileUrl(u)); el.setAttribute("title","点击预览"); });
      tpl.content.querySelectorAll('a[href]').forEach(el=>{ const u=el.getAttribute('href'); if (local(u)){ el.setAttribute('href', fileUrl(u)); el.setAttribute('data-wb-file', decodeURI(u.replace(/[?#].*$/,''))); el.setAttribute('target','_blank'); el.setAttribute('rel','noopener'); } });
      return tpl.innerHTML;
    },
  },
};
