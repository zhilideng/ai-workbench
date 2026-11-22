// <ic n="名称">：线性图标
import {ICONS} from '../core/icons.js';

export default {
  props:{n:String, s:{type:Number,default:16}, w:{type:Number,default:2}},
  computed:{ html(){ return ICONS[this.n]||''; } },
  template:`<svg aria-hidden="true" :width="s" :height="s" viewBox="0 0 24 24" fill="none" stroke="currentColor" :stroke-width="w" stroke-linecap="round" stroke-linejoin="round" style="flex:none" v-html="html"></svg>`,
}
