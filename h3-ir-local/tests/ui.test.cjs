const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'h3_ir_local', 'index.html'), 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
const context = {window:{Vue:{}}, Vue:{createApp:()=>({mount(){}})}};
vm.runInNewContext(script, context);
const {makePayload, blankForm, describeError} = context.window.H3Local;
const plain = value => JSON.parse(JSON.stringify(value));

function setupPage({confirm=()=>true, fetch=()=>{throw Error('unexpected network request');}, EventSource}={}) {
  let view;
  const vue={
    ref:value=>({value}), reactive:value=>value, computed:get=>({get value(){return get();}}),
    onMounted(){}, onUnmounted(){},
    createApp:options=>{view=options.setup();return {mount(){}};}
  };
  vm.runInNewContext(script,{
    window:{Vue:vue,confirm,EventSource}, Vue:vue, fetch, EventSource,
    crypto:require('node:crypto'),
    setTimeout:()=>0,clearTimeout(){}
  });
  return view;
}

function jsonResponse(body,ok=true) {
  return {ok,status:ok?200:409,headers:{get:()=> 'application/json'},json:async()=>body};
}

function historicalJob(id) {
  const request=plain(makePayload({...blankForm(),intent:'Original '+id},[
    {sha256:'a'.repeat(64),kind:'image',role:'frame_anchor_first',sizing:'match',note:'Keep the boat.'}
  ]));
  return {id,status:'completed',request:{...request,intent:'Effective '+id,seconds:8},
    attachments:[{index:0,sha256:'a'.repeat(64),kind:'image',name:id+'.png',bytes:1000,available:true,original_name:true}]};
}

test('opening history fills the one editor with effective inputs and archived files', async () => {
  const saved=historicalJob('saved'),requests=[];
  const view=setupPage({fetch:async(url)=>{requests.push(url);return jsonResponse(saved);}});
  Object.assign(view.settings,{base_url:'http://current.invalid/v1',model:'current'});
  view.keyInput.value='unsaved-key';
  await view.loadJob(saved.id);
  assert.equal(view.selected.value.id,saved.id);
  assert.equal(view.form.intent,'Effective saved');
  assert.equal(view.form.seconds,8);
  assert.equal(view.assets.value[0].name,'saved.png');
  assert.equal(view.assets.value[0].role,'frame_anchor_first');
  assert.equal(view.assets.value[0].note,'Keep the boat.');
  assert.equal(view.assets.value[0].file,null);
  assert.equal(view.assets.value[0].preview,'/api/jobs/saved/assets/0');
  assert.equal(view.assets.value[0].missing,false);
  assert.equal(view.settings.model,'current');
  assert.equal(view.keyInput.value,'unsaved-key');
  assert.deepEqual(requests,['/api/jobs/saved']);
});

test('switching history protects unsent edits and refinement instructions', async () => {
  const a=historicalJob('a'),b=historicalJob('b'),requests=[],prompts=[];
  let accept=false;
  const view=setupPage({confirm:message=>{prompts.push(message);return accept;},
    fetch:async(url)=>{requests.push(url);return jsonResponse(url.endsWith('/a')?a:b);}});
  await view.loadJob('a');
  assert.equal(prompts.length,0);
  view.assets.value[0].note='An unsent attachment edit.';
  view.change.value='An unsent refinement.';
  await view.loadJob('b');
  assert.deepEqual(requests,['/api/jobs/a']);
  assert.equal(view.selected.value.id,'a');
  assert.equal(view.assets.value[0].note,'An unsent attachment edit.');
  assert.equal(view.change.value,'An unsent refinement.');
  accept=true;
  await view.loadJob('b');
  assert.equal(view.form.intent,'Effective b');
  assert.equal(view.change.value,'');
  assert.match(prompts[0],/尚未提交/);
  await view.loadJob('a');
  assert.equal(prompts.length,2,'a freshly loaded history record must be clean');
  view.change.value='Refinement only.';
  accept=false;
  await view.loadJob('b');
  assert.equal(prompts.length,3);
  assert.equal(view.selected.value.id,'a');
  view.change.value='';
  view.form.intent='Unsent main parameters.';
  await view.loadJob('b');
  assert.equal(prompts.length,4);
  assert.equal(view.form.intent,'Unsent main parameters.');
});

test('history loading is serialized and a failed fetch keeps the current editor', async () => {
  const a=historicalJob('a'),requests=[];
  let resolve;
  const pending=new Promise(done=>{resolve=done;});
  const view=setupPage({fetch:async(url)=>{
    requests.push(url);
    return url.endsWith('/a')?jsonResponse(a):pending;
  }});
  await view.loadJob('a');
  const loading=view.loadJob('b');
  assert.equal(view.loadingJob.value,'b');
  const competing=view.loadJob('c');
  assert.deepEqual(requests,['/api/jobs/a','/api/jobs/b']);
  assert.equal(view.form.intent,'Effective a');
  resolve(jsonResponse({detail:{code:'job-missing',message:'历史记录不存在。'}},false));
  await Promise.all([loading,competing]);
  assert.equal(view.selected.value.id,'a');
  assert.equal(view.assets.value[0].name,'a.png');
  assert.equal(view.form.intent,'Effective a');
  assert.equal(view.loadingJob.value,'');
  assert.match(view.error.value,/job-missing/);
  view.uploading.value=true;
  await view.loadJob('c');
  assert.deepEqual(requests,['/api/jobs/a','/api/jobs/b']);
});

test('opening history flags missing files without changing active compilation state', async () => {
  const saved=historicalJob('saved');
  saved.attachments[0].available=false;
  saved.attachments[0].bytes=null;
  const view=setupPage({fetch:async()=>jsonResponse(saved)});
  view.busy.value=true;
  await view.loadJob(saved.id);
  assert.equal(view.busy.value,true);
  assert.equal(view.form.intent,'Effective saved');
  assert.equal(view.assets.value[0].missing,true);
  assert.equal(view.assets.value[0].preview,'');
  assert.equal(view.assets.value[0].size,null);
});

test('new and reset use the same unsent-edit protection as history selection', async () => {
  const saved=historicalJob('saved'),prompts=[];
  let accept=false;
  const view=setupPage({confirm:message=>{prompts.push(message);return accept;},
    fetch:async()=>jsonResponse(saved)});
  await view.loadJob(saved.id);
  view.form.intent='Unsent.';
  view.resetForm();
  assert.equal(view.form.intent,'Unsent.');
  view.reuseJob();
  assert.equal(prompts.length,2);
  assert.equal(view.form.intent,'Unsent.');
  assert.equal(view.selected.value.id,saved.id);
  accept=true;
  view.reuseJob();
  assert.equal(view.form.intent,'Effective saved');
  view.resetForm();
  assert.equal(prompts.length,3);
  assert.equal(view.form.intent,'');
  assert.equal(view.selected.value,null);
  assert.equal(view.assets.value.length,0);
  await view.loadJob(saved.id);
  assert.equal(prompts.length,3);
});

test('accepted rebuild is clean and its archived preview belongs to the new record', async () => {
  const saved=historicalJob('saved'),prompts=[];
  let rebuilt;
  class Events {addEventListener(){} close(){}}
  const view=setupPage({EventSource:Events,confirm:message=>{prompts.push(message);return true;},
    fetch:async(url,options)=>{
      if(options?.method==='POST') {
        const {source_job_id,...request}=JSON.parse(options.body);
        assert.equal(source_job_id,saved.id);
        rebuilt={...historicalJob('rebuilt'),status:'queued',request};
        return jsonResponse(rebuilt);
      }
      return jsonResponse(url==='/api/jobs'?[rebuilt]:saved);
    }});
  await view.loadJob(saved.id);
  view.form.intent='Rebuilt scene.';
  await view.submit();
  assert.equal(view.error.value,'');
  assert.equal(view.selected.value.id,'rebuilt');
  assert.equal(view.assets.value[0].preview,'/api/jobs/rebuilt/assets/0');
  assert.equal(rebuilt.request.intent,'Rebuilt scene.');
  await view.loadJob(saved.id);
  assert.equal(prompts.length,0,'accepted parameters must not be reported as unsent');
});

test('slot bindings read the manifest, not upload order or filenames', () => {
  const view=setupPage();
  const last={index:0,kind:'image',sha256:'last',name:'same.png'};
  const first={index:1,kind:'image',sha256:'first',name:'same.png'};
  const video={index:2,kind:'video',sha256:'clip',name:'clip.mp4'};
  const wrongKind={index:3,kind:'audio',sha256:'first',name:'voice.wav'};
  const entries=[
    {slot:0,label:'<Picture 1>',kind:'image',sha256:'first',role:'frame_anchor_first'},
    {slot:1,label:'<Picture 2>',kind:'image',sha256:'last',role:'frame_anchor_last'},
    {slot:2,label:'<Video 1>',kind:'video',sha256:'clip',role:'edit_source'}
  ];
  const job={attachments:[last,first,video,wrongKind],result:{ir:{manifest:entries}}};
  const before=JSON.stringify(job);
  assert.deepEqual(plain(view.assetSlots(job,last)),[entries[1]]);
  assert.deepEqual(plain(view.assetSlots(job,first)),[entries[0]]);
  assert.deepEqual(plain(view.manifestFiles(job,entries[0])),[first]);
  assert.deepEqual(plain(view.manifestFiles(job,entries[2])),[video]);
  assert.deepEqual(plain(view.assetSlots(job,wrongKind)),[]);
  assert.equal(JSON.stringify(job),before);
});

test('slot bindings expose repeated uses and never invent missing labels', () => {
  const view=setupPage();
  const asset={index:0,kind:'image',sha256:'shared',name:'reference.png'};
  const alias={...asset,index:1,name:'copy.png'};
  const first={slot:0,label:'<Picture 1>',kind:'image',sha256:'shared'};
  const last={slot:1,label:'<Picture 2>',kind:'image',sha256:'shared'};
  const job={attachments:[asset,alias],result:{ir:{manifest:[first,last]}}};
  assert.deepEqual(plain(view.assetSlots(job,asset)),[first,last]);
  assert.deepEqual(plain(view.manifestFiles(job,first)),[asset,alias]);
  assert.deepEqual(plain(view.assetSlots({attachments:[asset]},asset)),[]);
  assert.deepEqual(plain(view.manifestFiles({},first)),[]);
  assert.deepEqual(plain(view.manifestFiles(job,{...first,sha256:'missing'})),[]);
});

test('historical reuse restores effective parameters, original files and hidden API fields', () => {
  const image='i'.repeat(64),video='v'.repeat(64),audio='a'.repeat(64);
  const request={
    intent:'Original scene.',seconds:5,aspect:'9:16',creativity:'bold',effort:'max',
    shots:1,megapixels:1.2,seed:77,silent:false,constraints:['No music.'],
    director:'',director_profile:{name:'Quiet',notes:'Natural light.'},
    onscreen_text:['OPEN'],dialogue:[{text:'  原话，不改！  ',language:'Chinese',speaker:'pilot',voiceover:true}],
    loras:[{id:'original-style',strength:.8}],answer:{anchor_or_reference:'opening_frame'},
    transcripts:{[audio]:'Exact words.',['x'.repeat(64)]:'Additional transcript.'},
    assets:[
      {sha256:image,kind:'image',role:'replacement_subject',replaces:'the pilot',sizing:'max',
        note:'New person.',provenance:{camera:'original'}},
      {sha256:video,kind:'video',role:'edit_source',sizing:'match',note:'Source clip.',seconds:5,frames:124},
      {sha256:audio,kind:'audio',role:'voice_timbre',sizing:'match',note:'Voice.',paired_video_sha256:video}
    ]
  };
  const effective={...request,intent:'Show the mountains.',seconds:8,shots:2,
    constraints:['No music.','Slow camera movement.']};
  const job={id:'history',request:effective,parent_id:'earlier',attachments:[
    {index:0,sha256:image,name:'海边原图.png',bytes:100,available:true,original_name:true},
    {index:1,sha256:video,name:'clip.mp4',bytes:200,available:true,original_name:true},
    {index:2,sha256:audio,name:audio,bytes:300,available:true,original_name:false}
  ]};
  const before=JSON.stringify(job), view=setupPage();
  view.selected.value=job;
  view.reuseJob();
  assert.equal(view.form.intent,'Show the mountains.');
  assert.equal(view.form.seconds,8);
  assert.equal(view.form.shots,2);
  assert.equal(view.form.seed,77);
  assert.equal(view.form.constraints,'No music.\nSlow camera movement.');
  assert.equal(view.form.director,'custom');
  assert.equal(view.form.dialogue[0].text,'  原话，不改！  ');
  assert.equal(view.assets.value[0].name,'海边原图.png');
  assert.equal(view.assets.value[0].preview,'/api/jobs/history/assets/0');
  assert.equal(view.assets.value[0].file,null);
  assert.equal(view.assets.value[2].pairedId,view.assets.value[1].id);
  const payload=plain(makePayload(view.form,view.assets.value,view.baseRequest.value));
  assert.deepEqual(payload.assets,request.assets);
  assert.deepEqual(payload.loras,request.loras);
  assert.deepEqual(payload.answer,request.answer);
  assert.deepEqual(payload.transcripts,request.transcripts);
  assert.deepEqual(payload.asset_names,['海边原图.png','clip.mp4',null]);
  assert.deepEqual(payload.director_profile,request.director_profile);
  assert.equal(JSON.stringify(job),before);
  view.form.shots='';view.form.megapixels='';view.form.director='';
  view.assets.value[2].transcript='';
  const edited=plain(makePayload(view.form,view.assets.value,view.baseRequest.value));
  assert.equal('shots' in edited,false);
  assert.equal('megapixels' in edited,false);
  assert.equal(edited.director_profile,null);
  assert.equal(audio in edited.transcripts,false);
});

test('history deletion requires confirmation, preserves editing and other versions', async () => {
  const requests=[], prompts=[];
  const parent={id:'parent',status:'completed',intent:'A fox.'};
  const child={id:'child',status:'completed',intent:'Darker.',version:2};
  const view=setupPage({
    confirm:message=>{prompts.push(message);return true;},
    fetch:async(url,options)=>{
      requests.push({url,options});
      return jsonResponse(url==='/api/jobs'?[child]:{id:'parent',deleted:true});
    }
  });
  view.history.value=[parent,child];view.selected.value=parent;
  view.assets.value=[{file:null,preview:'/api/jobs/parent/assets/0',missing:false},
    {file:null,preview:'/api/jobs/child/assets/0',missing:false},
    {file:{},preview:'blob:unsent',missing:false}];
  view.form.intent='Unsaved edits';view.change.value='pending refinement';
  await view.deleteJob(parent);
  assert.match(prompts[0],/无法撤销/);
  assert.match(prompts[0],/独立附件，不影响其他版本/);
  assert.equal(requests[0].url,'/api/jobs/parent');
  assert.equal(requests[0].options.method,'DELETE');
  assert.equal(requests[0].options.headers['X-H3-Local'],'1');
  assert.equal(view.selected.value,null);
  assert.equal(view.change.value,'');
  assert.equal(view.form.intent,'Unsaved edits');
  assert.equal(view.assets.value[0].missing,true);
  assert.equal(view.assets.value[0].preview,'');
  assert.equal(view.assets.value[1].missing,false);
  assert.equal(view.assets.value[2].missing,false);
  assert.deepEqual(plain(view.history.value),[child]);
  assert.equal(view.deleting.value,'');
  view.selected.value=child;view.history.value=[parent,child];
  await view.deleteJob(parent);
  assert.equal(view.selected.value,child);
  assert.match(html,/class="history-delete"/);
  assert.match(html,/:disabled="!!deleting \|\| !!loadingJob \|\| \['queued','running'\]\.includes\(item.status\)"/);
});

test('history deletion cancellation and active status never send requests', async () => {
  let confirmations=0, requests=0;
  const view=setupPage({
    confirm:()=>{confirmations++;return false;},
    fetch:()=>{requests++;throw Error('must not delete');}
  });
  const saved={id:'one',status:'completed',intent:'fox'};
  view.history.value=[saved];view.selected.value=saved;
  await view.deleteJob(saved);
  assert.equal(confirmations,1);
  assert.equal(requests,0);
  assert.equal(view.selected.value,saved);
  for(const status of ['queued','running']) await view.deleteJob({...saved,status});
  assert.equal(confirmations,1);
  assert.equal(requests,0);
  assert.match(view.error.value,/任务不能删除/);
});

test('failed deletion keeps results and exposes the server error', async () => {
  const view=setupPage({fetch:async()=>jsonResponse({detail:{code:'job-active',message:'请等待任务结束。'}},false)});
  const saved={id:'one',status:'completed',intent:'fox',result:{ir:{prompt:'saved'}}};
  view.history.value=[saved];view.selected.value=saved;
  await view.deleteJob(saved);
  assert.equal(view.selected.value,saved);
  assert.deepEqual(plain(view.history.value),[saved]);
  assert.match(view.error.value,/job-active: 请等待任务结束/);
  assert.equal(view.deleting.value,'');
});

test('real page serializer preserves all material relationships and exact text', () => {
  const form = {...blankForm(), intent:'Change the pilot.', shots:'3', megapixels:'1.2',
    seed:'42', director:'custom', directorName:'Quiet', directorNotes:'Muted light.',
    dialogue:[{text:'  原话，不改！  ',language:'Chinese',speaker:'pilot',voiceover:true}],
    constraints:'no music\nno subtitles', onscreen_text:'OPEN\n营业中'};
  const assets = [
    {id:'image',sha256:'i'.repeat(64),kind:'image',sizing:'max',note:'pilot',role:'replacement_subject',replaces:'the pilot in blue'},
    {id:'video',sha256:'v'.repeat(64),kind:'video',sizing:'match',note:'source',role:'edit_source'},
    {id:'audio',sha256:'a'.repeat(64),kind:'audio',sizing:'match',note:'low voice',role:'voice_timbre',pairedId:'video',transcript:'Original words.'},
  ];
  const body = plain(makePayload(form, assets));
  assert.equal(body.assets[0].replaces, 'the pilot in blue');
  assert.equal(body.assets[2].paired_video_sha256, 'v'.repeat(64));
  assert.equal(body.transcripts['a'.repeat(64)], 'Original words.');
  assert.equal(body.dialogue[0].text, '  原话，不改！  ');
  assert.equal(body.dialogue[0].speaker, 'pilot');
  assert.equal(body.dialogue[0].voiceover, true);
  assert.equal(body.shots, 3);
  assert.equal(body.megapixels, 1.2);
  assert.deepEqual(body.director_profile, {name:'Quiet',notes:'Muted light.'});
  assert.deepEqual(body.onscreen_text, ['OPEN','营业中']);
  assert.deepEqual(body.constraints, ['no music','no subtitles']);
  assert.equal('file' in body.assets[0], false);
  assert.equal('path' in body.assets[0], false);
});

test('serializer refuses bad numbers, missing bytes and broken video pairing', () => {
  const form = {...blankForm(), intent:'fox'};
  assert.equal(makePayload(form, []).seed, 7);
  assert.equal(makePayload({...form,seed:''}, []).seed, null);
  assert.throws(()=>makePayload({...form,seconds:'oops'},[]), /时长/);
  assert.throws(()=>makePayload({...form,shots:'2.5'},[]), /镜头数/);
  assert.throws(()=>makePayload({...form,megapixels:'3'},[]), /像素/);
  assert.throws(()=>makePayload({...form,seed:'2.3'},[]), /种子/);
  assert.throws(()=>makePayload({...form,intent:'  '},[]), /创作需求/);
  assert.throws(()=>makePayload(form,[{name:'missing'}]), /尚未上传/);
  assert.throws(()=>makePayload(form,[{sha256:'a',kind:'audio',pairedId:'gone'}]), /配对视频/);
  const inferred = makePayload(form,[{sha256:'i',kind:'image',role:''}]);
  assert.equal('role' in inferred.assets[0], false);
});

test('errors retain useful upstream messages without HTML rendering', () => {
  assert.match(describeError({detail:{code:'asset-unreadable',message:'bad clip'}}), /asset-unreadable: bad clip/);
  assert.match(describeError({code:'invalid-ir',message:'bad',findings:[{rule:'T1'}]}), /T1/);
  assert.equal(html.includes('v-html'), false);
  assert.match(html, /Vue 加载失败/);
  assert.match(html, /确定性草稿/);
});

test('SSE subscriber updates stages, reconnects visibly and closes on completion', () => {
  const states=[], jobs=[], done=[];
  class Source {
    constructor(url) { this.url=url; this.listeners={}; this.closed=false; }
    addEventListener(name, callback) { this.listeners[name]=callback; }
    close() { this.closed=true; }
    emit(job) { this.listeners.job({data:JSON.stringify(job)}); }
  }
  const source=context.window.H3Local.subscribeJob('job-id', {
    onJob:job=>jobs.push(job),onState:message=>states.push(message),onDone:job=>done.push(job)
  }, Source);
  assert.equal(source.url,'/api/jobs/job-id/events');
  source.onopen();
  source.emit({status:'running',progress:[{stage:'analysis'}]});
  assert.equal(jobs[0].progress[0].stage,'analysis');
  assert.equal(source.closed,false);
  source.onerror();
  assert.match(states.at(-1),/重连/);
  assert.equal(done.length,0);
  source.emit({status:'completed',result:{source:'draft',fallback_reason:'test'}});
  assert.equal(source.closed,true);
  assert.equal(done[0].result.fallback_reason,'test');
  assert.equal(states.at(-1),'');
});

test('SSE subscriber closes on failure or interruption and reports malformed events', () => {
  for (const status of ['failed','interrupted']) {
    let ended;
    class Source {
      addEventListener(name,callback) {this.callback=callback;}
      close() {this.closed=true;}
    }
    const source=context.window.H3Local.subscribeJob('job',{
      onJob(){},onState(){},onDone(job){ended=job.status;}
    },Source);
    source.callback({data:JSON.stringify({status})});
    assert.equal(source.closed,true);
    assert.equal(ended,status);
  }
  let warning='';
  class Source {addEventListener(name,callback){this.callback=callback;}close(){this.closed=true;}}
  const source=context.window.H3Local.subscribeJob('job',{
    onJob(){},onState(message){warning=message;},onDone(){throw Error('must not succeed');}
  },Source);
  source.callback({data:'not json'});
  assert.equal(source.closed,true);
  assert.match(warning,/无法读取实时进度/);
  assert.equal(html.includes('setInterval(poll'),false);
});
