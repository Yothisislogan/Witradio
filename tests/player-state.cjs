/* Dependency-free JavaScript state tests. These do not replace browser/audio QA. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const root = path.resolve(__dirname, '..');
const code = fs.readFileSync(path.join(root,'web/player.js'),'utf8');
const markup = fs.readFileSync(path.join(root,'web/wit-music-player.html'),'utf8');
const ids = [...markup.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
class Element {
  constructor(){this.textContent='';this.attrs={};this.events={};this.children=[];this.disabled=false;}
  addEventListener(name,callback){(this.events[name] ||= []).push(callback);}
  emit(name){for(const callback of this.events[name]||[]) callback({target:this});}
  setAttribute(name,value){this.attrs[name]=String(value);}
  getAttribute(name){return this.attrs[name]??null;}
  removeAttribute(name){delete this.attrs[name];}
  appendChild(child){this.children.push(child);}
  replaceChildren(...children){this.children=children;}
}
function harness({blockedStorage=false}={}) {
  let now=1000000000000, timerId=0, stale=false, voteFailure=true, playFailure=false;
  const timers=new Map(), intervals=[], storage=new Map(), elements=Object.fromEntries(ids.map(id=>[id,new Element()]));
  const audio=elements.audioPlayer;
  Object.defineProperty(audio,'src',{get(){return this.attrs.src;},set(value){this.attrs.src=value;}});
  audio.load=()=>{};
  audio.pause=()=>audio.emit('pause');
  audio.play=()=>{if(playFailure)return Promise.reject(new Error('Network error'));audio.emit('playing');return Promise.resolve();};
  const body={dataset:{}};
  const location=new URL('https://radio.example/wit-music-player.html');
  class Clock extends Date {static now(){return now;}}
  const listeners=[];
  const sandbox={
    document:{getElementById:id=>elements[id],body,createElement:()=>new Element()},
    window:{location,WIT_RADIO_CONFIG:{streamUrl:'/stream',apiBase:'/api'},addEventListener(){}},
    location,navigator:{},crypto:webcrypto,URL,AbortController,Date:Clock,console,
    localStorage:{getItem(key){if(blockedStorage)throw new Error('blocked');return storage.get(key)??null;},setItem(key,value){if(blockedStorage)throw new Error('blocked');storage.set(key,value);}},
    setInterval(callback){intervals.push(callback);},
    setTimeout(callback,delay){timers.set(++timerId,{callback,delay});return timerId;},clearTimeout(id){timers.delete(id);},
    async fetch(url,options){
      const endpoint=new URL(url).pathname.split('/').pop();
      let data;
      if(endpoint==='now-playing')data={track_id:'a'.repeat(64),track:'Verified Music',show:'The Day Shift',host:'Dash',type:'bumper',timestamp:new Date(now-(stale?90000:0)).toISOString()};
      if(endpoint==='schedule')data={upcoming:[]};
      if(endpoint==='history')data={recent:[]};
      if(endpoint==='feedback'){
        const payload=JSON.parse(options.body);listeners.push(payload);
        data=voteFailure?{error:'Unavailable'}:{status:'saved',track_id:payload.track_id,vote:payload.vote};
      }
      return {ok:endpoint!=='feedback'||!voteFailure,json:async()=>data};
    }
  };
  vm.runInNewContext(code,sandbox,{filename:'player.js'});
  const flush=()=>new Promise(resolve=>setImmediate(resolve));
  return {elements,body,audio,intervals,listeners,storage,timers,flush,
    setStale(value){stale=value;},allowVotes(){voteFailure=false;},failPlayback(){playFailure=true;},advance(ms){now+=ms;},
    fireTimer(delay){const entry=[...timers].find(([,t])=>t.delay===delay);assert.ok(entry,`Expected ${delay}ms timer`);timers.delete(entry[0]);entry[1].callback();}
  };
}
(async()=>{
  const h=harness(); await h.flush();
  assert.equal(h.audio.src,undefined,'No stream download before user playback');
  assert.equal(h.elements.likeButton.disabled,true);
  h.elements.playButton.emit('click'); await h.flush();
  assert.equal(h.body.dataset.playback,'playing');
  assert.equal(h.elements.likeButton.disabled,false);
  h.elements.likeButton.emit('click'); await h.flush();
  assert.match(h.elements.feedbackNote.textContent,/^Not saved/);
  assert.equal(h.elements.likeButton.getAttribute('aria-pressed'),'false');
  h.allowVotes();h.elements.likeButton.emit('click');await h.flush();
  assert.match(h.elements.feedbackNote.textContent,/^Feedback saved/);
  assert.equal(h.listeners[1].track_id,'a'.repeat(64));
  h.elements.favoriteButton.emit('click');
  assert.ok(h.storage.get('witRadioFavorites').includes('Verified Music'));
  const oldUrl=h.audio.src;
  h.elements.playButton.emit('click');await h.flush();
  assert.equal(h.audio.src,undefined);
  assert.equal(h.body.dataset.playback,'paused');
  h.advance(100);h.elements.playButton.emit('click');await h.flush();
  assert.notEqual(h.audio.src,oldUrl);
  h.setStale(true);h.intervals[0]();await h.flush();
  assert.equal(h.elements.songTitle.textContent,'Track information unavailable');
  assert.equal(h.elements.likeButton.disabled,true);
  assert.equal(h.elements.favoriteButton.disabled,true);
  assert.equal(h.elements.djName.textContent,'WIT Radio');
  assert.equal(h.body.dataset.playback,'playing');
  h.failPlayback();h.audio.emit('error');await h.flush();
  for(const delay of [1000,2000,4000,8000,15000]){h.fireTimer(delay);await h.flush();}
  assert.equal(h.body.dataset.playback,'unavailable');
  assert.equal(h.audio.src,undefined);
  assert.equal(h.timers.size,0,'Retry exhaustion leaves no pending reconnect');
  const blocked=harness({blockedStorage:true});await blocked.flush();
  blocked.elements.playButton.emit('click');await blocked.flush();
  assert.equal(blocked.body.dataset.playback,'playing');
  blocked.elements.favoriteButton.emit('click');
  assert.match(blocked.elements.feedbackNote.textContent,/could not save/);
  console.log('Player state checks passed: play, failed/saved feedback, favorites, stop/rejoin, stale metadata, bounded retries, blocked storage.');
})().catch(error=>{console.error(error);process.exitCode=1;});
