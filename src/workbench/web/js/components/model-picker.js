// 模型选择器：搜索 + 引擎分栏 + 模型列表 + 思考强度滑轨。t 为 draft 或当前任务（直接改其 model/effort/engine）
const CLAUDE_MODEL_META = {opus:'最强推理', sonnet:'均衡', haiku:'极速低价'};
export default {
  props:{t:Object, canEngine:Boolean, disabled:Boolean},
  emits:['change'],
  data(){ return {open:false, q:'', tab:'', hi:-1, dragging:false, dragIdx:-1}; },
  computed:{
    r(){ return this.$root; },
    engines(){ const e=this.r.cfg.engines; return ['claude','codex'].filter(x=>!e || e[x]); },
    tabs(){
      const t=[{v:'all', l:'全部'}];
      for (const e of ['claude','codex']) t.push({v:e, l:this.r.engLabel(e), off:(this.r.cfg.engines && !this.r.cfg.engines[e]) || (!this.canEngine && e!==this.t.engine)});
      return this.canEngine ? t : t.filter(x=>x.v!=='all');
    },
    items(){
      const q=this.q.trim().toLowerCase(), out=[];
      const engs = this.tab==='all' ? this.engines : [this.tab];
      for (const e of engs){
        if (!this.canEngine && e!==this.t.engine) continue;
        const list=[{engine:e, model:'', label:'默认模型', meta:'跟随 CLI 配置'}];
        for (const m of this.r.modelsFor(e)) list.push({engine:e, model:m, label:m, meta:this.meta(e,m)});
        if (e===this.t.engine && this.t.model && !this.r.modelsFor(e).includes(this.t.model)) list.push({engine:e, model:this.t.model, label:this.t.model, meta:'自定义'});
        out.push(...list.filter(it=>!q || it.label.toLowerCase().includes(q) || this.r.engLabel(e).toLowerCase().includes(q)));
      }
      const raw=this.q.trim();
      if (raw && !out.some(it=>it.model===raw)){
        const e = this.tab!=='all' ? this.tab : this.t.engine;
        out.push({engine:e, model:raw, label:'使用「'+raw+'」', meta:'原样传给 '+(e==='codex'?'-m':'--model'), custom:true});
      }
      return out;
    },
    levels(){ return this.r.effortsFor(this.t.engine, this.t.model); },
    dflt(){ return this.t.engine==='codex' ? this.r.codexDefault(this.t.model) : ''; },
    idx(){ return this.levels.indexOf(this.t.effort); },
    shownIdx(){ return this.dragging && this.dragIdx>=0 ? this.dragIdx : this.idx>=0 ? this.idx : this.levels.indexOf(this.dflt); },
    pos(){ const n=this.levels.length-1, i=this.shownIdx; return n>0 && i>=0 ? i/n : 0; },
    lvText(){
      if (this.dragging && this.dragIdx>=0) return this.r.effortLabel(this.levels[this.dragIdx], this.t.engine);
      if (this.t.effort) return this.r.effortLabel(this.t.effort, this.t.engine);
      return this.dflt ? '默认 '+this.r.effortLabel(this.dflt, this.t.engine) : '默认';
    },
    btnTitle(){ return '模型与思考强度' + (this.canEngine ? '' : '：下一条消息起生效，会话上下文保留'); },
  },
  methods:{
    meta(e,m){
      if (e!=='codex') return (CLAUDE_MODEL_META[m]||'') + ' · ' + this.r.effortsFor(e,m).length + ' 档';
      const c=(this.r.cfg.codex_efforts||{})[m];
      return c ? c.levels.length+' 档 · 默认 '+this.r.effortLabel(c.default,'codex') : '';
    },
    isOn(it){ return !it.custom && it.engine===this.t.engine && it.model===(this.t.model||''); },
    toggle(){
      if (this.disabled) return;
      this.open=!this.open;
      if (this.open){ this.q=''; this.hi=-1; this.tab=this.canEngine ? this.t.engine : this.t.engine; this.$nextTick(()=>this.$refs.q && this.$refs.q.focus()); }
    },
    pick(it){
      const apply=()=>{
        this.t.model=it.model;
        if (this.t.effort && !this.r.effortsFor(this.t.engine, this.t.model).includes(this.t.effort)) this.t.effort='';
        this.$emit('change', {model:this.t.model, effort:this.t.effort});
      };
      this.q=''; this.hi=-1;
      if (it.engine!==this.t.engine){
        if (!this.canEngine) return;
        this.t.engine=it.engine; this.tab=it.engine;
        this.$nextTick(apply);   // draft.engine 的 watcher 会先清空 model，再写入
      } else apply();
    },
    setEff(e){
      if (this.t.effort===e) return;
      this.t.effort=e; this.$emit('change', {effort:e});
    },
    onKey(ev){
      const n=this.items.length; if (!n) return;
      if (ev.key==='ArrowDown'){ ev.preventDefault(); this.hi=(this.hi+1)%n; }
      else if (ev.key==='ArrowUp'){ ev.preventDefault(); this.hi=(this.hi-1+n)%n; }
      else if (ev.key==='Enter'){ ev.preventDefault(); this.pick(this.items[this.hi>=0?this.hi:0]); }
      else return;
      this.$nextTick(()=>{ const el=this.$el.querySelector('.mp-row.hi'); el && el.scrollIntoView({block:'nearest'}); });
    },
    // 拖动时滑块连续跟手：只在 rAF 里改 CSS 变量，不碰父级响应式数据；松手后吸附到最近档位并只提交一次
    pAt(x){
      const rc=this.rect||(this.rect=this.$refs.track.getBoundingClientRect());
      return Math.min(1, Math.max(0, (x-rc.left-21)/(rc.width-42)));
    },
    paint(){
      this.raf=0; const p=this.pAt(this.lastX), n=this.levels.length-1;
      this.$refs.track && this.$refs.track.style.setProperty('--p', p);
      const i=Math.round(p*n); if (i!==this.dragIdx) this.dragIdx=i;
    },
    down(ev){
      if (ev.button) return;
      this.rect=null; this.lastX=ev.clientX; this.moved=false;
      try{ this.$refs.track.setPointerCapture(ev.pointerId); }catch(_){}
      this.dragging=true; this.paint();
    },
    move(ev){
      if (!this.dragging) return;
      this.lastX=ev.clientX; this.moved=true;
      if (!this.raf) this.raf=requestAnimationFrame(this.paint);
    },
    up(){
      if (!this.dragging) return;
      if (this.raf){ cancelAnimationFrame(this.raf); this.paint(); }
      const i=this.dragIdx, n=this.levels.length-1, e=this.levels[i];
      this.dragging=false; this.dragIdx=-1;
      const el=this.$refs.track; el && i>=0 && el.style.setProperty('--p', n>0 ? i/n : 0);   // 带缓动吸附到最近档位
      if (e) this.setEff(e);
    },
    step(d){
      const n=this.levels.length, i=this.shownIdx<0 ? (d>0?-1:n) : this.shownIdx;
      const j=Math.min(n-1, Math.max(0, i+d)); this.setEff(this.levels[j]);
    },
    outside(e){ if (this.open && !this.$el.contains(e.target)) this.open=false; },
  },
  watch:{ disabled(v){ if (v) this.open=false; }, tab(){ this.hi=-1; }, q(){ this.hi=-1; } },
  mounted(){ document.addEventListener('mousedown', this.outside); },
  unmounted(){ document.removeEventListener('mousedown', this.outside); },
  template:`<div class="mp" :class="{open}" @keydown.esc.stop="open=false">
  <button type="button" class="ctl mp-btn" @click="toggle" :disabled="disabled" :title="btnTitle" aria-haspopup="dialog" :aria-expanded="open">
    <span class="mp-gl" :class="t.engine"><ic :n="t.engine==='codex'?'terminal':'spark'" :s="11" :w="2.4"></ic></span>
    <span class="mp-name">{{ t.model || '默认模型' }}</span><span class="mp-lv">{{ lvText }}</span>
    <ic class="chev" n="chevd" :s="11"></ic></button>
  <transition name="mp">
  <div class="mp-pop" v-if="open" role="dialog" aria-label="选择模型与思考强度">
    <label class="mp-search"><ic n="search" :s="16"></ic>
      <input ref="q" v-model="q" placeholder="搜索模型，或输入任意模型名" aria-label="搜索模型" @keydown="onKey" autocomplete="off" spellcheck="false"><kbd>esc</kbd></label>
    <div class="mp-tabs" role="tablist">
      <button v-for="tb in tabs" :key="tb.v" type="button" role="tab" class="mp-tab" :class="{on:tab===tb.v}" :aria-selected="tab===tb.v" :disabled="tb.off"
        :title="tb.off && !canEngine ? '会话进行中不能切换引擎' : null" @click="tab=tb.v">
        <ic v-if="tb.v!=='all'" :class="tb.v" :n="tb.v==='codex'?'terminal':'spark'" :s="13" :w="2.2"></ic>{{ tb.l }}</button>
    </div>
    <div class="mp-list" role="listbox" aria-label="模型">
      <template v-for="(it,i) in items" :key="it.engine+'/'+it.model+(it.custom?'*':'')">
        <div class="mp-sec" v-if="tab==='all' && !q && (i===0 || items[i-1].engine!==it.engine)">{{ r.engLabel(it.engine) }}</div>
        <button type="button" role="option" class="mp-row" :class="{on:isOn(it), hi:i===hi}" :aria-selected="isOn(it)" @click="pick(it)" @mousemove="hi=i">
          <span class="mp-gl" :class="it.engine"><ic :n="it.custom?'edit':it.engine==='codex'?'terminal':'spark'" :s="14" :w="2.2"></ic></span>
          <span class="mp-rn">{{ it.label }}</span><span class="mp-rm">{{ it.meta }}</span>
          <ic v-if="isOn(it)" class="mp-ck" n="check" :s="15" :w="2.4"></ic></button>
      </template>
      <div class="mp-none" v-if="!items.length">没有匹配的模型</div>
    </div>
    <div class="mp-card">
      <div class="mp-ch"><span class="mp-dot" :class="{def:!t.effort}"></span><b>思考强度 · {{ lvText }}</b>
        <button type="button" class="mp-def" :class="{on:!t.effort}" @click="setEff('')" :title="r.effortTitle(t.engine, t.model)">默认</button></div>
      <div class="mp-sub"><span>{{ r.engLabel(t.engine) }} · {{ t.model || '默认模型' }}</span><span>{{ levels.length }} 档{{ t.engine==='codex' ? ' · -c model_reasoning_effort' : ' · --effort' }}</span></div>
      <div class="mp-track" ref="track" :class="{dragging, def:!t.effort && !dragging}" :style="{'--p':pos}" role="slider" tabindex="0" aria-label="思考强度"
        :aria-valuemin="0" :aria-valuemax="levels.length-1" :aria-valuenow="shownIdx" :aria-valuetext="lvText"
        @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="up" @lostpointercapture="up"
        @keydown.left.prevent="step(-1)" @keydown.down.prevent="step(-1)" @keydown.right.prevent="step(1)" @keydown.up.prevent="step(1)">
        <div class="mp-fill"></div><div class="mp-knob"></div></div>
      <div class="mp-ticks">
        <button v-for="(e,i) in levels" :key="e" type="button" :class="{on:i===(dragging&&dragIdx>=0?dragIdx:idx), dflt:e===dflt}" @click="setEff(e)">{{ r.effortLabel(e, t.engine) }}</button></div>
    </div>
  </div>
  </transition>
</div>`,
}
