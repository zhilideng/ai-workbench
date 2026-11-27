// 任务：列表、打开 / 关闭、修改状态、执行 / 停止、删除
import {api, apiq} from '../core/api.js';

export default {
  data(){ return {
      tasks:[], tq:'', sel:null, tasksRefreshCount:0, tasksRefreshApplied:0,
      statusLabel:{todo:'待办',queued:'排队中',running:'执行中',review:'待验收',done:'已完成',failed:'失败',cancelled:'已取消',unreported:'结果未确认'},
  }; },
  computed:{
    tasksById(){ return new Map(this.tasks.map(t=>[t.id,t])); },
    agentChildren(){
      const children=new Map();
      for (const t of [...this.tasks].reverse()){
        if (!t.parent_task_id) continue;
        if (!children.has(t.parent_task_id)) children.set(t.parent_task_id,[]);
        children.get(t.parent_task_id).push(t);
      }
      return children;
    },
    // 主任务验收后，所有子任务的展示状态随之完成，任务记录仍保留执行状态。
    completedChildIds(){
      const ids=new Set();
      const parents=this.tasks.filter(t=>!this.isChildTask(t) && t.status==='done');
      while (parents.length){
        const parent=parents.pop();
        for (const child of this.agentChildren.get(parent.id) || []){
          ids.add(child.id);
          parents.push(child);
        }
      }
      return ids;
    },
    runningCount(){ return this.tasks.filter(this.isActive).length; },
    openCountAll(){ return this.tasks.filter(this.isOpenTask).length; },
    filteredTasks(){
      const k=this.tq.toLowerCase();
      return this.tasks.filter(t=>(!this.cur||t.project===this.cur) && (!k||(t.title+t.description+t.id).toLowerCase().includes(k)));
    },
  },
  methods:{
    isChildTask(t){ return !!(t.agent_id || t.parent_task_id); },
    displayTaskStatus(t){ return this.completedChildIds.has(t.id) ? 'done' : t.status; },
    isOpenTask(t){ return ['todo','review'].includes(this.displayTaskStatus(t)); },
    isActive(t){ return t && (t.status==='running'||t.status==='queued'); },
    countRunning(n){ return this.tasks.filter(t=>t.project===n&&this.isActive(t)).length; },
    countOpen(n){ return this.tasks.filter(t=>t.project===n&&this.isOpenTask(t)).length; },
    async refreshTasks(){
      const request=++this.tasksRefreshCount;
      const tasks=await apiq('/api/tasks');
      if (request<this.tasksRefreshApplied) return;
      this.tasksRefreshApplied=request;
      this.tasks=tasks;
    },
    async openTask(id){
      this.sideOpen=false;
      if (!this.sel) history.pushState({wbTask:id}, '');
      if (!this.sel || this.sel.id!==id){ this.logs={}; this.offsets={}; this.followup=''; this.editing=null; this.clearAtts(this.replyImgs); }
      this.sel = await api('/api/tasks/'+id);
      this.prewarmSel();
      await this.pullLogs(true);
      this.$nextTick(()=>this.$refs.replyInput && !this.isActive(this.sel) && this.$refs.replyInput.focus());
    },
    closeTask(){
      // 打开任务时压入过一条历史记录：退回去，让浏览器后退键 / 触控板手势与按钮行为一致
      if (this.sel && history.state && history.state.wbTask){ history.back(); return; }
      this.sel=null; this.showInfo=false; this.pv=null; this.editing=null;
    },
    newTask(){ this.sideOpen=false; this.closeTask(); this.$nextTick(()=>this.$refs.promptInput && this.$refs.promptInput.focus()); },
    async cancel(){ await api(`/api/tasks/${this.sel.id}/cancel`,{method:'POST'}); },
    async patch(body){
      this.sel = {...await api('/api/tasks/'+this.sel.id,{method:'PATCH',body}), runs:this.sel.runs}; this.refreshTasks();
      if (['engine','model','effort','permission_mode'].some(k=>k in body)) this.prewarmSel();   // 设置变了：按新设置提前准备
    },
    // 打开本机任务：后台提前恢复它的会话，写回复时就准备好了
    prewarmSel(){
      const t=this.sel;
      if (t && !t.ssh_id && !this.isActive(t) && !t.agent_id) apiq(`/api/tasks/${t.id}/prewarm`,{method:'POST'}).catch(()=>{});
    },
    async quickPatch(t, body){ await api('/api/tasks/'+t.id,{method:'PATCH',body}); this.refreshTasks(); },
    async quickRun(t){
      if (t.agent_id) return;
      await api(`/api/tasks/${t.id}/run`,{method:'POST', body:{message:null}});
      this.refreshTasks();
    },
    async quickCancel(t){ await api(`/api/tasks/${t.id}/cancel`,{method:'POST'}); },
    del(){ return this.delTask(this.sel); },
    canDeleteTask(t){
      return !!t && !this.isActive(t) && !this.isActive(this.tasksById.get(t.parent_task_id))
        && !this.agentChildren.get(t.id)?.some(this.isActive);
    },
    async delTask(t){
      if (!this.canDeleteTask(t)) return;
      const children=this.agentChildren.has(t.id) ? '及其所有子会话' : '';
      if (!confirm(`删除任务 #${t.id}「${t.title}」${children}？对话和运行日志会一并删除，无法恢复${t.branch?'（worktree 会移除，分支 '+t.branch+' 保留）':''}。`)) return;
      await api('/api/tasks/'+t.id,{method:'DELETE'});
      if (this.sel && this.sel.id===t.id) this.closeTask();
      this.refreshTasks();
    },
  },
};
