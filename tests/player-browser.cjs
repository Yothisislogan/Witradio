/* Local browser integration checks. Requires Playwright; no live station is used. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const trackId = 'a'.repeat(64);
function toneWav(seconds = 120) {
  const rate = 8000, samples = rate * seconds;
  const buffer = Buffer.alloc(44 + samples * 2);
  buffer.write('RIFF'); buffer.writeUInt32LE(buffer.length - 8, 4); buffer.write('WAVEfmt ', 8);
  buffer.writeUInt32LE(16, 16); buffer.writeUInt16LE(1, 20); buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(rate, 24); buffer.writeUInt32LE(rate * 2, 28);
  buffer.writeUInt16LE(2, 32); buffer.writeUInt16LE(16, 34); buffer.write('data', 36);
  buffer.writeUInt32LE(samples * 2, 40);
  for (let i=0;i<samples;i++) buffer.writeInt16LE(Math.round(1200*Math.sin(2*Math.PI*440*i/rate)), 44+i*2);
  return buffer;
}
(async () => {
  let metadataStale = false, failVotes = true, votePayload, streamRequests = 0;
  const wave = toneWav();
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (process.env.WIT_FIXTURE_ONLY && fs.existsSync('/tmp/wit-fixture-state.json')) {const state=JSON.parse(fs.readFileSync('/tmp/wit-fixture-state.json'));metadataStale=!!state.metadataStale;failVotes=state.failVotes!==false;}
    const send = data => {res.setHeader('Content-Type','application/json'); res.end(JSON.stringify(data));};
    if (url.pathname === '/api/now-playing') return send({track_id:trackId, track:'Harbor After Hours', type:'bumper', show:'The Night Shift', host:'Nyx', dj_role:'A little company after dark.', timestamp:new Date(Date.now()-(metadataStale?90000:0)).toISOString()});
    if (url.pathname === '/api/schedule') return send({current:{description:'Late-night lo-fi, synthwave, and relaxed instrumental music.'}, upcoming:[{name:'Morning Acoustic',starts_around:'06:00'},{name:'The Day Shift',starts_around:'09:00'},{name:'Dance Party',starts_around:'20:00'}]});
    if (url.pathname === '/api/history') return send({recent:[{track_name:'The Quiet Water',vibe:'bumper'},{track_name:'A Little Further',vibe:'bumper'},{track_name:'Last Light',vibe:'bumper'}]});
    if (url.pathname === '/api/feedback') { let body=''; req.on('data',c=>body+=c); req.on('end',()=>{votePayload=JSON.parse(body);res.statusCode=failVotes?503:200;send(failVotes?{error:'Feedback temporarily unavailable.'}:{status:'saved',track_id:votePayload.track_id,vote:votePayload.vote});}); return; }
    if (url.pathname === '/stream') { streamRequests++; res.setHeader('Content-Type','audio/wav'); res.setHeader('Cache-Control','no-store'); res.end(wave); return; }
    const file = path.resolve(root, 'web', '.'+url.pathname);
    if (!file.startsWith(path.join(root,'web')+path.sep) || !fs.existsSync(file)) {res.statusCode=404;res.end();return;}
    res.setHeader('Content-Type', file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html');
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve=>server.listen(process.env.WIT_FIXTURE_ONLY ? 8765 : 0,'0.0.0.0',resolve));
  if (process.env.WIT_FIXTURE_ONLY) { console.log('WIT fixture server: http://127.0.0.1:'+server.address().port); return; }
  let browser;
  try {
    browser = await chromium.launch({headless:true, args:['--no-sandbox']});
    const page = await browser.newPage({viewport:{width:1440,height:1200}});
    const errors=[]; page.on('pageerror',e=>errors.push(e.message));
    await page.route('https://fonts.googleapis.com/**', route=>route.abort());
    await page.goto(`http://127.0.0.1:${server.address().port}/wit-music-player.html`);
    await page.waitForFunction(()=>document.getElementById('songTitle').textContent==='Harbor After Hours');
    assert.equal(await page.locator('#likeButton').isDisabled(),true);
    assert.equal(streamRequests,0,'No automatic audio download before Play Live');
    await page.locator('#playButton').click();
    await page.waitForFunction(()=>document.body.dataset.playback==='playing');
    assert.equal(await page.locator('#likeButton').isEnabled(),true);
    await page.locator('#likeButton').click();
    await page.waitForFunction(()=>document.getElementById('feedbackNote').textContent.startsWith('Not saved.'));
    assert.equal(await page.locator('#likeButton').getAttribute('aria-pressed'),'false');
    failVotes=false;
    await page.locator('#likeButton').click();
    await page.waitForFunction(()=>document.getElementById('feedbackNote').textContent.startsWith('Feedback saved.'));
    assert.equal(votePayload.track_id,trackId);
    assert.match(votePayload.listener_id,/^[a-f0-9-]{36}$/);
    await page.locator('#favoriteButton').click();
    assert.match(await page.locator('#favoritesList').textContent(),/Harbor After Hours/);
    fs.mkdirSync(path.join(root,'artifacts'),{recursive:true});
    await page.screenshot({path:path.join(root,'artifacts/player-desktop.png'),fullPage:true});
    const firstUrl=await page.locator('#audioPlayer').getAttribute('src');
    await page.locator('#playButton').click();
    assert.equal(await page.locator('#audioPlayer').getAttribute('src'),null);
    await page.locator('#playButton').click();
    await page.waitForFunction(()=>document.body.dataset.playback==='playing');
    assert.notEqual(await page.locator('#audioPlayer').getAttribute('src'),firstUrl);
    await page.evaluate(()=>document.getElementById('audioPlayer').dispatchEvent(new Event('error')));
    await page.waitForFunction(()=>document.body.dataset.playback==='reconnecting');
    await page.waitForFunction(()=>document.body.dataset.playback==='playing');
    metadataStale=true;
    await page.waitForFunction(()=>document.getElementById('songTitle').textContent==='Track information unavailable',{timeout:15000});
    assert.equal(await page.locator('#likeButton').isDisabled(),true);
    assert.equal(await page.locator('#favoriteButton').isDisabled(),true);
    assert.equal(await page.evaluate(()=>document.body.dataset.playback),'playing','Metadata outage must not stop audio');
    metadataStale=false;
    await page.setViewportSize({width:390,height:844});
    await page.reload();
    await page.waitForFunction(()=>document.getElementById('songTitle').textContent==='Harbor After Hours');
    assert.match(await page.locator('#favoritesList').textContent(),/Harbor After Hours/);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'No horizontal mobile overflow');
    const button=await page.locator('#playButton').boundingBox();
    assert.ok(button.y+button.height<844,'Play Live is above the fold on mobile');
    await page.screenshot({path:path.join(root,'artifacts/player-mobile.png'),fullPage:true});
    // Storage can be disabled or malformed; neither may break listening.
    await page.addInitScript(()=>{Object.defineProperty(Storage.prototype,'getItem',{value(){throw new Error('blocked');}});Object.defineProperty(Storage.prototype,'setItem',{value(){throw new Error('blocked');}});});
    await page.reload();
    await page.locator('#playButton').click();
    await page.waitForFunction(()=>document.body.dataset.playback==='playing');
    assert.deepEqual(errors,[]);
    console.log('Browser checks passed: playback, stop/rejoin, reconnect, failed/saved votes, stale metadata, favorites, mobile layout, blocked storage.');
  } finally {
    if(browser) await browser.close();
    await new Promise(resolve=>server.close(resolve));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
