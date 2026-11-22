// 看板 / 列表视图：分列、分页、拖拽改状态

export default {
  data(){ return {
      view: localStorage.getItem('wb.view')||'board', colLimits:{}, PAGE:20, dragId:null, dropCol:'',
      // glyph：列头信号灯；rail：列头之间画流水线（失败 / 取消是旁路，不连线）
      columns:[
        {key:'todo',label:'待办',s:['todo'],glyph:'todo',rail:true,empty:'暂无待办',emptyHint:'在下方写好任务，点「存为待办」'},
        {key:'running',label:'执行中',s:['queued','running'],glyph:'run',rail:true,empty:'没有正在运行的任务',emptyHint:'把待办拖到这里即开始执行'},
        {key:'review',label:'待验收',s:['review'],glyph:'review',rail:true,empty:'没有待验收的任务'},
        {key:'done',label:'已完成',s:['done'],glyph:'done',empty:'还没有完成的任务'},
        {key:'failed',label:'失败 / 取消',s:['failed','cancelled'],glyph:'fail',empty:'没有失败的任务'}],
  }; },
  computed:{
    // 正在拖动的卡片（用来提示哪些列可以放）
    dragTask(){ return this.dragId ? this.tasks.find(x=>x.id===this.dragId) || null : null; },
  },
  watch:{
    tq(){ this.colLimits={}; },
    view(v){ localStorage.setItem('wb.view', v); },
  },
  methods:{
    colTasks(c){ return this.filteredTasks.filter(t=>c.s.includes(t.status)); },
    // 每列默认只渲染前 PAGE 条，避免数据多时卡顿
    colLimit(c){ return this.colLimits[c.key] || this.PAGE; },
    colVisible(c){ return this.colTasks(c).slice(0, this.colLimit(c)); },
    showMore(c){ this.colLimits[c.key] = this.colLimit(c) + this.PAGE; },
    collapse(c){ delete this.colLimits[c.key]; },
    // ---- 看板拖拽：拖到某列即改为该列状态；拖到「执行中」= 开始执行
    dropTarget(t, c){
      if (!t || this.isActive(t) || c.s.includes(t.status)) return null;
      if (c.key==='running') return ['todo','failed','cancelled'].includes(t.status) ? 'run' : null;
      return {todo:'todo', review:'review', done:'done', failed:'cancelled'}[c.key] || null;
    },
    onDragStart(e, t){
      this.dragId=t.id; e.dataTransfer.effectAllowed='move';
      e.dataTransfer.setData('application/x-wb-task', String(t.id));
    },
    onDragOver(e, c){
      if (!this.dragId) return;
      if (this.dropTarget(this.tasks.find(x=>x.id===this.dragId), c)){ e.preventDefault(); e.dataTransfer.dropEffect='move'; this.dropCol=c.key; }
      else { e.dataTransfer.dropEffect='none'; if (this.dropCol===c.key) this.dropCol=''; }
    },
    onDragLeave(e, c){ if (this.dropCol===c.key && !e.currentTarget.contains(e.relatedTarget)) this.dropCol=''; },
    async onDrop(c){
      const t=this.tasks.find(x=>x.id===this.dragId), target=this.dropTarget(t, c);
      this.dragId=null; this.dropCol='';
      if (!target) return;
      try{
        if (target==='run'){ t.status='queued'; await this.quickRun(t); }
        else { t.status=target; await this.quickPatch(t,{status:target}); }
      }catch(_){ this.refreshTasks(); }
    },
  },
};
