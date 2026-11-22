// 图片附件：选择、粘贴、拖入、上传
import {upload} from '../core/api.js';

export default {
  data(){ return {
      fileOver:'',
  }; },
  methods:{
    // ---- 图片附件
    async addFiles(list, files){
      for (const f of [...(files||[])]){
        if (!/^image\/(png|jpeg|gif|webp)$/.test(f.type)){ alert(`不支持的文件：${f.name}（只支持 png / jpg / gif / webp）`); continue; }
        if (f.size > 20*1024*1024){ alert(`${f.name} 超过 20MB`); continue; }
        const a={key:Math.random().toString(36).slice(2), name:f.name||'粘贴的图片', preview:URL.createObjectURL(f), path:''};
        list.push(a);
        try{
          const j=await upload(f);
          const it=list.find(x=>x.key===a.key); if (it) it.path=j.path;
        }catch(err){ const i=list.findIndex(x=>x.key===a.key); if (i>=0) this.removeAtt(list,i); alert('图片上传失败：'+err.message); }
      }
    },
    removeAtt(list, i){ URL.revokeObjectURL(list[i].preview); list.splice(i,1); },
    clearAtts(list){ list.forEach(a=>URL.revokeObjectURL(a.preview)); list.splice(0); },
    onPaste(e, list){
      const files=[...(e.clipboardData?.items||[])].filter(i=>i.kind==='file' && i.type.startsWith('image/')).map(i=>i.getAsFile()).filter(Boolean);
      if (files.length){ e.preventDefault(); this.addFiles(list, files); }
    },
    onFileOver(e, which){
      if (![...(e.dataTransfer?.types||[])].includes('Files')) return;
      e.preventDefault(); e.dataTransfer.dropEffect='copy'; this.fileOver=which;
    },
    onFileDrop(e, list){
      this.fileOver='';
      if (!e.dataTransfer?.files?.length) return;
      e.preventDefault();
      if (list===this.replyImgs && this.isActive(this.sel)) return;
      this.addFiles(list, e.dataTransfer.files);
    },
  },
};
