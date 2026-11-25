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
      const members=new Map();
      for (const t of this.tasks){
        if (!t.parallel_group_id) continue;
        if (!members.has(t.parallel_group_id)) members.set(t.parallel_group_id,[]);
        members.get(t.parallel_group_id).push(t);
      }
      const visible=new Set(this.filteredTasks.map(t=>t.id));
      for (const t of this.filteredTasks){
        if (this.parallelById.has(t.parallel_group_id)) continue;
        const key=this.isActive(t) ? 'active' : t.status==='todo' || t.status==='review' ? t.status : 'recent';
        categories[key].items.push(t);
      }
      const query=this.tq.trim().toLowerCase();
      for (const group of this.dispatchGroups){
        if (this.cur && group.project!==this.cur) continue;
        const all=members.get(group.id) || [];
        const matchesGroup=query && [group.title,group.prompt,String(group.id)].some(value=>value?.toLowerCase().includes(query));
        const children=all.filter(t=>matchesGroup || visible.has(t.id)).sort((a,b)=>a.parallel_position-b.parallel_position || a.id-b.id);
        if (!children.length && (group.state==='ready' || (query && !matchesGroup))) continue;
        const key=group.state!=='ready' || all.some(this.isActive) ? 'active'
          : all.some(t=>t.status==='todo') ? 'todo' : all.some(t=>t.status==='review') ? 'review' : 'recent';
        const completed=all.filter(t=>t.status==='review' || t.status==='done').length;
        const progress=all.length ? Math.round(100*completed/all.length) : 0;
        const updated_at=all.reduce((latest,t)=>Math.max(latest,t.updated_at||0),group.updated_at||0);
        categories[key].items.push({id:group.id,group,children,completed,total:all.length,progress,updated_at});
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
