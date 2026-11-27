// 侧边栏：按状态分组的任务列表

export default {
  data(){ return {
      sideOpen:false, pswOpen:false, sideLimit:{},
      sideClosed:(()=>{ try{ return JSON.parse(localStorage.getItem('wb.sideClosed')||'{}'); }catch(e){ return {}; } })(),
  }; },
  computed:{
    // 整组只出现一次，搜索仅缩小展示的会话范围。
    sideGroups(){
      const categories={
        active:{label:'进行中',items:[]},
        review:{label:'待验收',items:[]},
        todo:{label:'待办',items:[]},
        recent:{label:'最近',items:[]},
      };
      const visible=new Set(this.filteredTasks.map(t=>t.id));
      for (const t of this.tasks){
        if (this.cur && t.project!==this.cur) continue;
        const children=this.agentChildren.get(t.id) || [];
        if (t.parent_task_id && this.tasksById.has(t.parent_task_id)) continue;
        const matching=visible.has(t.id) ? children : children.filter(child=>visible.has(child.id));
        if (!visible.has(t.id) && !matching.length) continue;
        const key=this.isActive(t) || (t.status!=='done' && children.some(this.isActive)) ? 'active'
          : t.status==='todo' || t.status==='review' ? t.status : 'recent';
        const total=children.length;
        const completed=children.filter(child=>['review','done'].includes(this.displayTaskStatus(child))).length;
        const overall=total ? {completed, total, percent:Math.round(100*completed/total)} : null;
        categories[key].items.push({...t, children:matching, overall});
      }
      return Object.entries(categories).filter(([,category])=>category.items.length).map(([key,category])=>({
        key,...category,
        items:category.items.sort((a,b)=>b.updated_at-a.updated_at),
        limit:this.sideLimit[key]||(key==='recent'?20:50),
      }));
    },
  },
  methods:{
    toggleGroup(k, closed=!this.sideClosed[k]){
      if (closed) this.sideClosed[k]=true;
      else delete this.sideClosed[k];
      try{ localStorage.setItem('wb.sideClosed', JSON.stringify(this.sideClosed)); }catch(e){}
    },
  },
};
