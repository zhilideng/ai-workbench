// 应用入口：把各功能模块（features/*，Vue mixin）组合成一个应用，注册组件，启动轮询。
// 新增功能：在 features/ 下加一个 mixin 并放进 FEATURES；新增组件：在 components/ 下加一个并放进 COMPONENTS。
import {api, apiq} from './core/api.js';
import {call, isNative, nativeReady} from './core/native.js';
import {setProjectsRoot} from './core/paths.js';

import attachments from './features/attachments.js';
import board from './features/board.js';
import composer from './features/composer.js';
import conversation from './features/conversation.js';
import format from './features/format.js';
import logs from './features/logs.js';
import models from './features/models.js';
import preview from './features/preview.js';
import projects from './features/projects.js';
import quota from './features/quota.js';
import sidebar from './features/sidebar.js';
import ssh from './features/ssh.js';
import tasks from './features/tasks.js';

import icon from './components/icon.js';
import modelPicker from './components/model-picker.js';
import statusGlyph from './components/status-glyph.js';

const FEATURES = [format, models, projects, tasks, sidebar, board, composer, conversation, logs, attachments, preview, ssh, quota];
const COMPONENTS = {ic: icon, sg: statusGlyph, 'model-picker': modelPicker};

const app = Vue.createApp({
  mixins: FEATURES,
  data(){ return {timer:null, native:false}; },
  watch:{
    'cfg.projects_root'(v){ setProjectsRoot(v); },
  },
  methods:{
    async loadAll(){
      [this.cfg, this.projects, this.tasks] = await Promise.all([api('/api/config'), api('/api/projects'), api('/api/tasks')]);
    },
    // 选择项目根目录（其下每个子目录是一个项目）：原生文件夹对话框；浏览器调试时退化为输入框
    async chooseProjectsRoot(){
      if (this.sourceEditor) return;
      if (this.cfg.projects_root_locked) return alert('项目目录由环境变量 WB_PROJECTS_ROOT 指定，不能在应用内修改。');
      const path = isNative() ? await call('choose_directory', '选择项目目录')
                              : prompt('项目目录（其下每个子目录是一个项目）', this.cfg.projects_root || '');
      if (!path) return;
      this.cfg = await api('/api/settings', {method:'PUT', body:{projects_root:path}});
      this.cur = '';
      this.projects = await api('/api/projects');
    },
    // AI 回复里引用的本地文件：在桌面应用里用系统默认应用打开（如「预览」），浏览器里照常新开页面
    async openLocalLink(e){
      const a = e.target.closest && e.target.closest('a[data-wb-file]');
      if (!a || !this.sel || !isNative()) return;
      e.preventDefault();
      const r = await call('open_task_file', this.sel.id, a.getAttribute('data-wb-file'));
      if (r && !r.ok) alert(r.error);
    },
    async tick(){
      try{
        await this.refreshTasks();
        // Codex 任务跑完后刷新一次额度
        const nowActive=new Set(this.tasks.filter(t=>t.engine==='codex'&&this.isActive(t)).map(t=>t.id));
        if ([...this.activeCodex].some(id=>!nowActive.has(id))) this.loadQuota(true);
        this.activeCodex=nowActive;
        // 远程 Claude 任务跑完后刷新服务器额度
        const nowRemote=new Set(this.tasks.filter(t=>t.ssh_id && t.engine!=='codex' && this.isActive(t)).map(t=>t.id));
        if ([...this.activeRemoteClaude].some(id=>!nowRemote.has(id))) this.fetchClaudeRemote(true);
        this.activeRemoteClaude=nowRemote;
        if (this.sel){
          const t=this.tasks.find(x=>x.id===this.sel.id);
          if (!t){ this.closeTask(); return; }
          const changed = t.status!==this.sel.status || t.session_id!==this.sel.session_id;
          if (changed || this.isActive(t)){
            const fresh = await apiq('/api/tasks/'+t.id);
            if (this.sel && this.sel.id===t.id){ this.sel=fresh; await this.pullLogs(false); }
          }
        }
      }catch(e){}
    },
  },
  async mounted(){
    await this.loadAll();
    if (this.cur && !this.curProject) this.cur='';
    this.loadSshConnections();
    this.$refs.promptInput && this.$refs.promptInput.focus();
    this.timer = setInterval(this.tick, 1500);
    this.loadQuota(false);
    setInterval(()=>this.loadQuota(false), 5*60*1000);
    setInterval(async()=>{ try{ this.projects = await apiq('/api/projects'); }catch(e){} }, 30000);
    setInterval(()=>{ if (!document.hidden) this.loadSshConnections(); }, 15000);
    nativeReady().then(v=>{ this.native=v; });
    document.addEventListener('click', this.openLocalLink);
    // 文件拖到输入框以外的地方时，不要让窗口直接打开它
    ['dragover','drop'].forEach(t=>window.addEventListener(t, e=>{
      if (!e.defaultPrevented && [...(e.dataTransfer?.types||[])].includes('Files')){ e.preventDefault(); if (t==='drop') this.fileOver=''; }
    }));
    document.addEventListener('mousedown', e=>{
      if (this.pswOpen && this.$refs.psw && !this.$refs.psw.contains(e.target)) this.pswOpen=false;
      if (this.sourcesOpen && !this.$refs.sourceMenu?.contains(e.target)) this.sourcesOpen=false;
    });
    window.addEventListener('popstate', ()=>{ if (this.sel){ this.sel=null; this.showInfo=false; this.pv=null; this.editing=null; } });
    document.addEventListener('keydown', e=>{
      const mod = e.metaKey || e.ctrlKey;
      if (this.sourceEditor) return;
      if (mod && e.key.toLowerCase()==='n'){ e.preventDefault(); this.newTask(); }
      else if (mod && e.key.toLowerCase()==='k'){ e.preventDefault(); this.switchProject(); }
      else if (e.key==='Escape' && this.pswOpen) this.pswOpen=false;
      else if (e.key==='Escape' && this.sourcesOpen) this.sourcesOpen=false;
      else if (e.key==='Escape' && this.sideOpen) this.sideOpen=false;
      else if (e.key==='Escape' && this.pv) this.pv=null;
      else if (e.key==='Escape' && this.ssh.open){ if (this.ssh.confirmDel||this.ssh.confirmDisc){ this.ssh.confirmDel=this.ssh.confirmDisc=null; } else if (this.ssh.form && this.ssh.list.length) this.cancelSshForm(); else this.closeSsh(); }
      else if (this.pv && (e.key==='+'||e.key==='=')) this.pvZoom(1.25);
      else if (this.pv && e.key==='-') this.pvZoom(1/1.25);
      else if (this.pv && e.key==='0') this.pvReset();
      else if (this.pv && e.key==='1') this.pvActual();
      else if (e.key==='Escape' && this.editing) this.editing=null;
      else if (e.key==='Escape' && this.sel) this.closeTask();
    });
  },
});
for (const [name, c] of Object.entries(COMPONENTS)) app.component(name, c);
const vm = app.mount('#app');

// 给原生菜单调用（desktop/main.py）
window.wb = {
  newTask: () => vm.newTask(),
  switchProject: () => vm.switchProject(),
  chooseProjectsRoot: () => vm.chooseProjectsRoot(),
};
