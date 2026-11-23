// 项目切换、源文件夹关联与目录浏览
import {apiq} from '../core/api.js';

const parentFolder = path => path.slice(0, path.lastIndexOf('/')) || '/';
const folderName = path => path.slice(path.lastIndexOf('/') + 1) || '/';
const ancestors = path => path === '/' ? ['/'] : [...ancestors(parentFolder(path)), path];

export default {
  data(){ return {
    cfg:{}, projects:[], cur:localStorage.getItem('wb.cur')||'', pq:'',
    sourcesOpen:false, sourceSelection:{}, sourceEditor:null, folderPicker:null,
  }; },
  computed:{
    visibleProjects(){ return this.projects.filter(p=>p.name.toLowerCase().includes(this.pq.toLowerCase())); },
    curProject(){ return this.projects.find(p=>p.name===this.cur); },
    projectSources(){ return this.curProject?.sources || []; },
    activeSource(){
      const saved = this.sourceSelection[this.cur] || localStorage.getItem('wb.source.' + this.cur);
      return this.projectSources.find(s=>this.sourceKey(s)===saved) || this.projectSources[0];
    },
    activeSourceKey(){ return this.cur + ':' + (this.activeSource ? this.sourceKey(this.activeSource) : ''); },
    executionSource(){
      const id = this.draft.ssh_id;
      if (this.activeSource?.ssh_id===id && this.activeSource.available) return this.activeSource;
      return this.projectSources.find(s=>s.ssh_id===id && s.available);
    },
    canChooseFolder(){
      const p=this.folderPicker;
      return !!(p && !p.jumping && !p.error && p.nodes[p.selected]?.path);
    },
    folderRows(){
      const p = this.folderPicker;
      if (!p) return [];
      const filter = p.query.trim().toLowerCase();
      const rows = [];
      function visit(path, depth, waitingForPreview=false){
        const data = p.nodes[path];
        const open = p.expanded[path];
        const next=p.trail[depth]===path && p.trail[depth+1];
        const start = rows.length;
        const expandable=!!next || (data?.folders ? data.folders.length>0 : !waitingForPreview);
        rows.push({path, depth, open, data, name:folderName(path), expandable});
        if (open){
          if (next) visit(next,depth+1);
          for (const name of data?.folders || []){
            const child=(path==='/' ? '' : path)+'/'+name;
            if (child!==next) visit(child,depth+1,!!data.prefetched && !!data.loading);
          }
        }
        if (filter && !path.toLowerCase().includes(filter) && rows.length===start+1) rows.pop();
      }
      if (p.nodes['/']) visit('/', 0);
      return rows;
    },
  },
  watch:{
    cur(v){
      localStorage.setItem('wb.cur', v);
      this.colLimits={};
      this.sourcesOpen=false;
      this.projSsh=[];
      this.draft.use_worktree=false;
      this.draft.ssh_id=this.activeSource?.ssh_id || 0;
      this.loadProjSsh();
    },
    activeSourceKey(){
      this.draft.use_worktree=false;
      this.draft.ssh_id=this.activeSource?.ssh_id || 0;
    },
  },
  methods:{
    sourceKey(s){ return s.ssh_id + ':' + s.path; },
    sourceLabel(s){ return s.ssh_id ? s.connection : '本机'; },
    sourceDisplay(s){ return s.ssh_id ? s.path : this.shortPath(s.path); },
    switchProject(){ if (this.sourceEditor) return; if (!this.pswOpen) this.togglePsw(); this.sideOpen=true; },
    pick(name){ this.cur=name; this.pswOpen=false; this.sideOpen=false; this.closeTask(); },
    pickFirstProject(){ const p=this.visibleProjects[0]; if (p) this.pick(p.name); },
    togglePsw(){ this.sourcesOpen=false; this.pswOpen=!this.pswOpen; if (this.pswOpen){ this.pq=''; this.$nextTick(()=>this.$refs.projSearch?.focus()); } },
    selectProjectSource(s){
      const key=this.sourceKey(s);
      this.sourceSelection[this.cur]=key;
      localStorage.setItem('wb.source.'+this.cur, key);
      this.draft.ssh_id=s.ssh_id;
      this.sourcesOpen=false;
    },
    async openProjectEditor(manage=false, trigger){
      if (this.sourceEditor || (manage && !this.curProject)) return;
      this.pswOpen=false;
      this.sourcesOpen=false;
      this.sourceEditor={
        manage, trigger, name:manage ? this.cur : '', error:'', saving:false,
        sources:manage ? this.projectSources.map(s=>({...s})) : [],
      };
      await this.$nextTick();
      this.$refs.sourceDialog?.showModal();
      this.focusProjectEditor();
    },
    focusProjectEditor(){
      this.$nextTick(()=>{
        const dialog=this.$refs.sourceDialog;
        (this.$refs.projectName || dialog?.querySelector('.source-add, .source-empty, .source-close'))?.focus();
      });
    },
    closeProjectEditor(){
      if (this.sourceEditor?.saving) return;
      if (this.ssh.fromFolder){ this.closeSsh(); return; }
      if (this.folderPicker){
        this.folderPicker=null;
        this.focusProjectEditor();
        return;
      }
      const trigger=this.sourceEditor?.trigger;
      this.$refs.sourceDialog?.close();
      this.sourceEditor=null;
      this.$nextTick(()=>{ if (trigger?.isConnected) trigger.focus(); });
    },
    async saveProjectEditor(){
      const form=this.sourceEditor;
      if (form.saving) return;
      form.error='';
      if (!form.manage && !form.name.trim()){
        form.error='请输入项目名称';
        this.$refs.projectName?.focus();
        return;
      }
      if (!form.manage && !form.sources.length){ form.error='请选择源文件夹'; return; }
      const url=form.manage ? '/api/projects/'+encodeURIComponent(form.name)+'/sources' : '/api/projects';
      const sources=form.sources.map(({path, ssh_id})=>({path, ssh_id}));
      form.saving=true;
      try{
        this.projects=await apiq(url,{method:form.manage?'PUT':'POST',body:{name:form.name.trim(),sources}});
        if (!form.manage) this.pick(form.name.trim());
        form.saving=false;
        this.closeProjectEditor();
      }catch(e){ form.error=e.message; }
      finally{ form.saving=false; }
    },
    async openFolderPicker(index=-1){
      const source=this.sourceEditor.sources[index];
      this.folderPicker={index, mode:source?.ssh_id?'ssh':'local', connections:[], query:'',
        nodes:{}, expanded:{}, trail:[], sshId:source?.ssh_id||0, selected:'', position:'',
        pathInput:'', pathError:'', jumping:false, error:'', loading:false, request:0};
      this.$nextTick(()=>this.$refs.folderTitle?.focus());
      await this.initFolderPicker(source?.path);
    },
    async initFolderPicker(start=''){
      const p=this.folderPicker;
      const request=++p.request;
      Object.assign(p,{error:'',duplicate:'',nodes:{},expanded:{},trail:[],selected:'',position:'',
        query:'',pathInput:'',pathError:'',jumping:false,loading:true});
      try{
        let directory;
        if (p.mode==='ssh'){
          const connections=await apiq('/api/project-folders/ssh');
          if (p.request!==request) return;
          p.connections=connections;
          const c=connections.find(c=>c.id===p.sshId) || connections[0];
          if (!c) return;
          if (p.sshId && p.sshId!==c.id) start='';
          p.sshId=c.id;
          if (!start) start=c.recent_path || c.remote_dir || '/';
        }else{
          try{
            directory=await this.readFolder(p,start);
            start=directory.path;
          }catch(e){
            if (!start) throw e;
          }
        }
        if (!start.startsWith('/')){
          directory=await this.readFolder(p,start.startsWith('~') ? start : '~');
          start=directory.path;
        }
        if (p.request!==request) return;
        if (directory) this.cacheFolderNode(p,start,directory);
        if (!p.pathInput) p.pathInput=start;
        await this.openFolderTrail(p,start,request);
      }catch(e){ if (p.request===request) p.error=e.message; }
      finally{ if (p.request===request) p.loading=false; }
    },
    readFolder(p,path){
      const endpoint=p.mode==='ssh' ? '/api/project-folders/ssh/'+p.sshId : '/api/project-folders/local';
      return apiq(endpoint+'?path='+encodeURIComponent(path));
    },
    cacheFolderNode(p,path,directory){
      const {previews, ...node}=directory;
      p.nodes[path]=node;
      for (const [name, folders] of Object.entries(previews || {})){
        const child=(path==='/' ? '' : path)+'/'+name;
        if (!p.nodes[child]) p.nodes[child]={path:child,folders,prefetched:true};
      }
    },
    openFolderTrail(p,path,request,reportError=true){
      p.position=path.replace(/\/+$/,'') || '/';
      p.trail=ancestors(p.position);
      return Promise.all(p.trail.map(folder=>{
        p.expanded[folder]=true;
        const node=p.nodes[folder];
        if (!node?.folders || node.prefetched) return this.loadFolderNode(p,folder,request,reportError);
      }));
    },
    async loadFolderNode(p, path, request=p.request, reportError=true){
      p.expanded[path]=true;
      const cached=p.nodes[path];
      if (cached?.loading===request) return;
      p.nodes[path]={...cached,loading:request};
      try{
        const data=await this.readFolder(p,path);
        if (p.request===request) this.cacheFolderNode(p,path,data);
      }catch(e){
        if (p.request!==request) return;
        p.nodes[path]={error:e.message};
        if (reportError && (path==='/' || e.message.startsWith('无法连接到'))) p.error=e.message;
      }
    },
    setFolderMode(mode){
      const p=this.folderPicker;
      if (p.mode===mode) return;
      p.mode=mode;
      this.initFolderPicker();
    },
    selectFolderConnection(id){
      const p=this.folderPicker;
      if (p.sshId===id) return;
      p.sshId=id;
      this.initFolderPicker();
    },
    async selectFolder(path){
      const p=this.folderPicker;
      this.editFolderPath(p.nodes[path]?.path || path);
      p.selected=path;
      if (!p.nodes[path] || p.nodes[path].error) await this.loadFolderNode(p,path);
    },
    toggleFolder(path){
      const p=this.folderPicker;
      const node=p.nodes[path];
      if (p.expanded[path]) p.expanded[path]=false;
      else if (node?.folders?.length!==0){
        p.expanded[path]=true;
        if (!node?.folders || node.prefetched) this.loadFolderNode(p,path);
      }
    },
    editFolderPath(value){
      const p=this.folderPicker;
      if (p.jumping){ ++p.request; p.jumping=false; }
      Object.assign(p,{pathInput:value,pathError:'',duplicate:'',selected:''});
    },
    async goToFolderPath(){
      const p=this.folderPicker;
      if (p.jumping || (p.mode==='ssh' && !p.sshId)) return;
      const path=p.pathInput;
      if (!(path.startsWith('/') || path==='~' || path.startsWith('~/'))){
        p.pathError='请输入以「/」或「~/」开头的目录路径';
        return;
      }
      const request=++p.request;
      Object.assign(p,{selected:'',pathError:'',duplicate:'',jumping:true,loading:false});
      try{
        const directory=await this.readFolder(p,path);
        if (p.request!==request) return;
        this.cacheFolderNode(p,directory.path,directory);
        p.selected=directory.path;
        p.pathInput=directory.path;
        p.query='';
        p.error='';
        this.openFolderTrail(p,directory.path,request,false);
      }catch(e){
        if (p.request===request) p.pathError=e.message;
      }finally{
        if (p.request===request) p.jumping=false;
      }
    },
    chooseFolder(){
      const p=this.folderPicker;
      if (!this.canChooseFolder) return;
      const path=p.nodes[p.selected].path;
      const sshId=p.mode==='ssh' ? p.sshId : 0;
      const source={path,ssh_id:sshId,name:folderName(path),connection:p.connections.find(c=>c.id===sshId)?.name};
      const form=this.sourceEditor;
      if (form.sources.some((s,i)=>i!==p.index && this.sourceKey(s)===this.sourceKey(source))){
        p.duplicate='已关联此源文件夹';
        return;
      }
      if (p.index<0) form.sources.push(source);
      else form.sources.splice(p.index,1,source);
      if (!form.manage && !form.name.trim() && source.name!=='/') form.name=source.name;
      apiq('/api/project-folders/recent',{method:'PUT',body:{path,ssh_id:sshId}}).catch(()=>{});
      form.error='';
      this.closeProjectEditor();
    },
    folderParent(){
      const p=this.folderPicker;
      this.selectFolder(parentFolder(p.selected));
    },
  },
};
