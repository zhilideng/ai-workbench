// 看板执行过程：运行时增量读取，结束后首次悬停再读取。
import {apiq} from '../core/api.js';

const stateLabels = {done:'已完成', running:'进行中', pending:'待执行', failed:'出错', warning:'警告', interrupted:'未完成'};
const clock = new Intl.DateTimeFormat('zh-CN', {hourCycle:'h23', hour:'2-digit', minute:'2-digit', second:'2-digit'});

const ActivityRows = {
  props:{rows:Array, timed:Boolean, empty:{type:String,default:'等待执行日志…'}},
  methods:{
    time(ts){ return Number.isFinite(ts) ? clock.format(ts * 1000) : '—'; },
    stateLabel(state){ return stateLabels[state]; },
  },
  template:`<ol class="activity-rows" :class="{timed}">
    <li v-for="(row, index) in rows" :key="row.index ?? index" class="activity-row" :class="{current:row.state==='running'}">
      <span v-if="timed" class="activity-time">{{ time(row.time) }}</span>
      <span class="activity-mark" :class="row.state">
        <span class="sr">{{ stateLabel(row.state) }}</span>
        <ic v-if="row.state==='done'" n="check" :s="13" :w="2.6"></ic>
        <ic v-else-if="row.state==='failed'" n="x" :s="12" :w="2.6"></ic>
        <span v-else-if="row.state==='running'" class="activity-spinner"></span>
        <span v-else-if="row.state==='pending' || row.state==='interrupted'" class="activity-pending"></span>
        <span v-else aria-hidden="true">!</span>
      </span>
      <span class="activity-label">{{ row.label }}</span>
    </li>
    <li v-if="!rows.length" class="activity-empty">{{ empty }}</li>
  </ol>`,
};

export default {
  components:{ActivityRows},
  props:{taskId:Number, runId:Number, running:Boolean, progress:Number, progressLabel:String, startedAt:Number},
  data(){ return {rows:[], plan:[], offset:0, now:Date.now(), logStart:null, showing:false, loaded:false, position:{}, side:''}; },
  computed:{
    elapsed(){
      const seconds = Math.max(0, Math.floor(this.now / 1000 - (this.logStart ?? this.startedAt)));
      return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
        .map(n=>String(n).padStart(2, '0')).join(':');
    },
    recentRows(){ return this.rows.slice(-3); },
    pendingApprovals(){ return this.running ? this.rows.filter(row=>row.approval) : []; },
    progressText(){
      if (this.progress != null) return this.progress+'%';
      const completed = this.rows.filter(row=>row.operation && row.state==='done').length;
      return completed ? `完成 ${completed} 项` : '进度待定';
    },
  },
  mounted(){
    this._rowIndexes = new Map();
    if (this.running){
      this.pullActivity();
      this._activityTimer = setInterval(this.pullActivity, 1000);
    }
  },
  beforeUnmount(){
    clearInterval(this._activityTimer);
    this.hideDetails();
  },
  methods:{
    async pullActivity(){
      this.now = Date.now();
      if (!this.runId || this._pullingActivity || document.hidden) return;
      this._pullingActivity = true;
      try{
        const query = new URLSearchParams({activity:'true', offset:String(this.offset)});
        const res = await apiq(`/api/runs/${this.runId}/events?${query}`);
        const scroll = this.$refs.detailsScroll;
        const followLatest = scroll && scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 12;
        for (const {plan, ...row} of res.events){
          if (plan) this.plan = plan;
          const index = this._rowIndexes.get(row.key);
          if (index !== undefined){
            const current = this.rows[index];
            current.label = row.label || current.label;
            current.state = row.state;
            if (row.operation) current.operation = true;
            if (current.approval || row.approval) current.approval = row.approval;
          }else if (row.label){
            if (row.key) this._rowIndexes.set(row.key, this.rows.length);
            this.rows.push({...row, index:this.rows.length});
          }
          if (row.key === 'start' && Number.isFinite(row.time)) this.logStart = row.time;
        }
        this.offset = res.offset;
        if (res.status !== 'running'){
          for (const row of this.rows){
            if (row.state === 'running') row.state = 'interrupted';
            if (row.approval){
              row.label = '审批已失效';
              row.approval = null;
            }
          }
          clearInterval(this._activityTimer);
          this.loaded = true;
        }
        if (this.showing) this.$nextTick(()=>this.placeDetails(followLatest));
      }catch(_){ /* 保留上次结果，下次轮询重试。 */ }
      finally{ this._pullingActivity = false; }
    },
    async decide(approval, decision){
      if (approval.busy) return;
      approval.busy = true;
      approval.error = '';
      try{
        await apiq(`/api/tasks/${this.taskId}/approval`, {
          method:'POST', body:{id:approval.id, run_id:this.runId, decision},
        });
      }catch(error){
        approval.error = error.message;
        approval.busy = false;
      }
      this.pullActivity();
    },
    showDetails(e){
      if (e.pointerType !== 'mouse') return;
      this.cancelDetailsClose();
      if (this.showing) return;
      this.showing = true;
      if (!this.running && !this.loaded) this.pullActivity();
      this.$nextTick(()=>this.placeDetails(true));
      window.addEventListener('scroll', this.trackDetails, true);
      window.addEventListener('resize', this.trackDetails);
      window.addEventListener('blur', this.hideDetails);
    },
    cancelDetailsClose(){
      clearTimeout(this._detailsCloseTimer);
    },
    queueDetailsClose(e){
      if (e.pointerType !== 'mouse') return;
      this.cancelDetailsClose();
      this._detailsCloseTimer = setTimeout(this.hideDetails, 180);
    },
    hideDetails(){
      this.cancelDetailsClose();
      this.showing = false;
      window.removeEventListener('scroll', this.trackDetails, true);
      window.removeEventListener('resize', this.trackDetails);
      window.removeEventListener('blur', this.hideDetails);
    },
    trackDetails(e){
      if (e.target === this.$refs.detailsScroll) return;
      if (this.$refs.preview?.matches(':hover') || this.$refs.details?.matches(':hover')) this.placeDetails();
      else this.hideDetails();
    },
    placeDetails(followLatest = false){
      const popup = this.$refs.details;
      if (!this.showing || !popup) return;
      const box = this.$refs.preview.getBoundingClientRect();
      const {width, height} = popup.getBoundingClientRect();
      const gap = 12;
      const right = box.right + gap + width <= innerWidth - gap;
      const left = box.left - gap - width >= gap;
      const minTop = gap;
      const maxTop = Math.max(gap, innerHeight - height - gap);
      const above = box.top > innerHeight - box.bottom;
      const preferredTop = right || left ? box.top : above ? box.top - height - gap : box.bottom + gap;
      const top = Math.min(maxTop, Math.max(minTop, preferredTop));
      const x = right ? box.right + gap : left ? box.left - gap - width
        : Math.min(innerWidth - width - gap, Math.max(gap, box.left));
      this.side = right ? 'right' : left ? 'left' : above ? 'above' : 'below';
      this.position = {left:x+'px', top:top+'px', '--activity-gap':gap+'px',
                       '--activity-tip':Math.min(height - 12, Math.max(12, box.top + box.height / 2 - top))+'px'};
      if (followLatest){
        const scroll = this.$refs.detailsScroll;
        scroll.scrollTop = scroll.scrollHeight;
      }
    },
    scrollDetails(e){
      const popup = this.$refs.detailsScroll;
      if (!popup || !e.deltaY || popup.scrollHeight <= popup.clientHeight) return;
      e.preventDefault();
      popup.scrollTop += e.deltaY * ([1, 18, popup.clientHeight][e.deltaMode] || 1);
    },
  },
  template:`<div class="run-activity">
    <div v-if="running" class="activity-progress">
      <span class="cstate run"><span class="dot"></span>执行中</span>
      <div class="activity-track" role="progressbar" aria-label="执行进度"
           :aria-valuenow="progress ?? undefined" :aria-valuetext="progress == null ? progressText : progressLabel || undefined"
           :class="{unknown:progress == null}">
        <span :style="progress == null ? null : {width:progress+'%'}"></span>
      </div>
      <span class="activity-percent" :class="{unknown:progress == null}">{{ progressText }}</span>
    </div>
    <section v-for="row in pendingApprovals" :key="row.key" class="activity-approval"
             :aria-label="row.label" @click.stop @keydown.enter.stop>
      <div class="activity-approval-title"><ic :n="row.approval.kind==='file' ? 'edit' : row.approval.kind==='tool' ? 'tool' : 'terminal'" :s="13"></ic>{{ row.label }}</div>
      <details v-if="row.approval.command || row.approval.reason || row.approval.cwd || row.approval.grant_root">
        <summary>查看审批详情</summary>
        <pre v-if="row.approval.command" class="activity-approval-command">{{ row.approval.command }}</pre>
        <div v-if="row.approval.reason">{{ row.approval.reason }}</div>
        <div v-if="row.approval.cwd">目录：{{ row.approval.cwd }}</div>
        <div v-if="row.approval.grant_root">申请可写：{{ row.approval.grant_root }}</div>
      </details>
      <div v-if="row.approval.error" class="activity-approval-error" role="alert">{{ row.approval.error }}</div>
      <div class="activity-approval-actions">
        <button type="button" class="btn sm" :disabled="row.approval.busy" @click="decide(row.approval,'decline')">拒绝</button>
        <button type="button" class="btn sm primary" :disabled="row.approval.busy" @click="decide(row.approval,'accept')">批准</button>
      </div>
    </section>
    <div ref="preview" class="activity-preview" :class="{showing, compact:!running}"
         :aria-label="running ? '最近三条执行日志' : '查看本轮执行过程'"
         @pointerenter="showDetails" @pointerleave="queueDetailsClose" @wheel="scrollDetails">
      <activity-rows v-if="running" :rows="recentRows" timed></activity-rows>
      <template v-else><ic n="list" :s="13"></ic>执行过程</template>
    </div>
    <div v-if="running" class="card-foot activity-foot">
      <span class="activity-elapsed"><ic n="clock" :s="14"></ic><span class="elapsed-label">已耗时</span> {{ elapsed }}</span>
      <slot></slot>
    </div>
    <teleport to="body">
      <div v-if="showing" ref="details" class="activity-pop" :class="side" :style="position" role="tooltip" aria-label="执行过程"
           @pointerenter="cancelDetailsClose" @pointerleave="queueDetailsClose">
        <div ref="detailsScroll" class="activity-details">
          <template v-if="plan.length">
            <div class="activity-heading">执行计划</div>
            <activity-rows :rows="plan"></activity-rows>
            <div class="activity-heading">执行过程</div>
          </template>
          <activity-rows :rows="rows" :empty="loaded ? '暂无执行记录' : undefined" timed></activity-rows>
        </div>
      </div>
    </teleport>
  </div>`,
};
