// 任务：列表、打开 / 关闭、修改状态、执行 / 停止、删除
import {api} from '../core/api.js';

export default {
  data(){ return {
      tasks:[], tq:'', sel:null,
      statusLabel:{todo:'待办',queued:'排队中',running:'执行中',review:'待验收',done:'已完成',failed:'失败',cancelled:'已取消'},
  }; },
  computed:{
    runningCount(){ return this.tasks.filter(this.isActive).length; },
    openCountAll(){ return this.tasks.filter(t=>['todo','review'].includes(t.status)).length; },
    filteredTasks(){
      const k=this.tq.toLowerCase();
      return this.tasks.filter(t=>(!this.cur||t.project===this.cur) && (!k||(t.title+t.description+t.id).toLowerCase().includes(k)));
    },
  },
  methods:{
    isActive(t){ return t && (t.status==='running'||t.status==='queued'); },
    countRunning(n){ return this.tasks.filter(t=>t.project===n&&this.isActive(t)).length; },
    countOpen(n){ return this.tasks.filter(t=>t.project===n&&['todo','review'].includes(t.status)).length; },
    async refreshTasks(){ this.tasks = await api('/api/tasks'); },
    async openTask(id){
      this.sideOpen=false;
      if (!this.sel) history.pushState({wbTask:id}, '');
      if (!this.sel || this.sel.id!==id){ this.logs={}; this.offsets={}; this.followup=''; this.editing=null; this.clearAtts(this.replyImgs); }
      this.sel = await api('/api/tasks/'+id);
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
    async patch(body){ this.sel = {...await api('/api/tasks/'+this.sel.id,{method:'PATCH',body}), runs:this.sel.runs}; this.refreshTasks(); },
    async quickPatch(t, body){ await api('/api/tasks/'+t.id,{method:'PATCH',body}); this.refreshTasks(); },
    async quickRun(t){ await api(`/api/tasks/${t.id}/run`,{method:'POST', body:{message:null}}); this.refreshTasks(); },
    async quickCancel(t){ await api(`/api/tasks/${t.id}/cancel`,{method:'POST'}); },
    del(){ return this.delTask(this.sel); },
    async delTask(t){
      if (!t || this.isActive(t)) return;
      if (!confirm(`删除任务 #${t.id}「${t.title}」？对话和运行日志会一并删除，无法恢复${t.branch?'（worktree 会移除，分支 '+t.branch+' 保留）':''}。`)) return;
      await api('/api/tasks/'+t.id,{method:'DELETE'});
      if (this.sel && this.sel.id===t.id) this.closeTask();
      this.refreshTasks();
    },
  },
};
