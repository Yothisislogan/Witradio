(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const audio = $('audioPlayer');
  if (!audio) return;
  const configured = window.WIT_RADIO_CONFIG || {};
  const streamUrl = new URL(configured.streamUrl || '/stream', window.location.href);
  const apiBase = new URL((configured.apiBase || '/api').replace(/\/$/, '') + '/', window.location.href);
  const mixedContent = location.protocol === 'https:' && (streamUrl.protocol !== 'https:' || apiBase.protocol !== 'https:');
  let wantsPlayback = false;
  let playbackState = 'ready';
  let retryCount = 0;
  let retryTimer;
  let stallTimer;
  let lastMetadataAt = 0;
  let currentTrack = null;
  let savingVote = false;
  let favorites = [];
  let listenerId;
  const votes = new Map();
  const retries = [1000, 2000, 4000, 8000, 15000];

  function readSaved(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch (_) { return fallback; }
  }
  function persist(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch (_) { return false; }
  }
  listenerId = readSaved('witRadioListenerId', null);
  if (typeof listenerId !== 'string' || !/^[0-9a-f-]{36}$/i.test(listenerId)) {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, n => n.toString(16).padStart(2, '0')).join('');
    listenerId = `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
    persist('witRadioListenerId', listenerId);
  }
  const saved = readSaved('witRadioFavorites', []);
  if (Array.isArray(saved)) favorites = saved.filter(t => t && typeof t.id === 'string' && typeof t.title === 'string').slice(0, 50);
  const volume = Number(readSaved('witRadioVolume', 0.8));
  audio.volume = Number.isFinite(volume) ? Math.min(1, Math.max(0, volume)) : 0.8;
  $('volume').value = String(audio.volume);

  function refreshButtons() {
    const validTrack = currentTrack && Date.now() - lastMetadataAt < 30000;
    const canVote = validTrack && playbackState === 'playing' && !savingVote;
    $('likeButton').disabled = !canVote;
    $('dislikeButton').disabled = !canVote;
    $('favoriteButton').disabled = !validTrack;
    const choice = currentTrack && votes.get(currentTrack.id);
    $('likeButton').setAttribute('aria-pressed', String(choice === 'like'));
    $('dislikeButton').setAttribute('aria-pressed', String(choice === 'dislike'));
    $('favoriteButton').textContent = currentTrack && favorites.some(t => t.id === currentTrack.id) ? '♥ Saved' : '♡ Save track';
  }
  function setState(state, help) {
    playbackState = state;
    document.body.dataset.playback = state;
    const labels = {ready: 'Ready to listen', connecting: 'Connecting', playing: 'Listening live', buffering: 'Buffering', reconnecting: 'Reconnecting', paused: 'Stopped', unavailable: 'Stream unavailable'};
    $('playbackStatus').textContent = labels[state];
    $('playbackHelp').textContent = help || ({ready: 'Join the live broadcast.', playing: 'You’re listening to the live stream.', paused: 'Press Play Live to rejoin the broadcast.'}[state] || '');
    $('playText').textContent = wantsPlayback ? 'Stop listening' : state === 'unavailable' ? 'Try again' : 'Play Live';
    $('playIcon').textContent = wantsPlayback ? '■' : '▶';
    $('playButton').setAttribute('aria-label', wantsPlayback ? 'Stop listening' : 'Play live radio');
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = state === 'playing' ? 'playing' : 'paused';
    refreshButtons();
  }
  function clearTimers() { clearTimeout(retryTimer); clearTimeout(stallTimer); retryTimer = null; stallTimer = null; }
  function stop() {
    wantsPlayback = false;
    clearTimers();
    audio.pause();
    audio.removeAttribute('src');
    audio.load(); // Stop downloading; the next click always joins the live edge.
    setState('paused');
  }
  function failed() {
    if (!wantsPlayback || retryTimer) return;
    clearTimeout(stallTimer);
    if (retryCount >= retries.length) {
      wantsPlayback = false;
      clearTimers();
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
      setState('unavailable', 'The stream could not reconnect. Try again when you’re ready.');
      return;
    }
    const delay = retries[retryCount++];
    setState('reconnecting', `Reconnecting to the live broadcast… attempt ${retryCount} of ${retries.length}.`);
    retryTimer = setTimeout(() => { retryTimer = null; connect(); }, delay);
  }
  function waitForAudio() {
    if (!wantsPlayback || retryTimer) return;
    clearTimeout(stallTimer);
    setState('buffering', 'Waiting for audio. We’ll reconnect if the signal doesn’t return.');
    stallTimer = setTimeout(failed, 12000);
  }
  async function connect() {
    if (!wantsPlayback) return;
    if (mixedContent) {
      wantsPlayback = false;
      setState('unavailable', 'The station’s secure audio connection is not configured yet.');
      return;
    }
    const url = new URL(streamUrl);
    url.searchParams.set('_live', String(Date.now()));
    audio.src = url.href;
    audio.load();
    clearTimeout(stallTimer);
    stallTimer = setTimeout(failed, 12000);
    try {
      await audio.play();
      if (!wantsPlayback) audio.pause();
    } catch (error) {
      if (!wantsPlayback || error.name === 'AbortError') return;
      if (error.name === 'NotAllowedError') {
        wantsPlayback = false;
        clearTimers();
        audio.pause();
        setState('ready', 'Press Play Live to allow audio in your browser.');
      } else failed();
    }
  }
  function start() {
    if (wantsPlayback) return;
    wantsPlayback = true;
    retryCount = 0;
    clearTimers();
    setState('connecting', 'Joining the live broadcast…');
    connect();
  }
  $('playButton').addEventListener('click', () => wantsPlayback ? stop() : start());
  $('volume').addEventListener('input', event => {
    audio.volume = Number(event.target.value);
    persist('witRadioVolume', audio.volume);
  });
  audio.addEventListener('playing', () => {
    if (!wantsPlayback) { audio.pause(); return; }
    retryCount = 0;
    clearTimers();
    setState('playing');
  });
  audio.addEventListener('waiting', waitForAudio);
  audio.addEventListener('stalled', waitForAudio);
  audio.addEventListener('error', failed);
  audio.addEventListener('ended', failed);
  audio.addEventListener('pause', () => { if (!wantsPlayback && playbackState !== 'unavailable') setState('paused'); });
  window.addEventListener('offline', () => { if (wantsPlayback) failed(); });
  window.addEventListener('online', () => { if (wantsPlayback && playbackState !== 'playing') { clearTimers(); connect(); } });
  if ('mediaSession' in navigator) {
    navigator.mediaSession.setActionHandler('play', start);
    navigator.mediaSession.setActionHandler('pause', stop);
    navigator.mediaSession.setActionHandler('stop', stop);
  }

  async function request(endpoint, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(new URL(endpoint, apiBase), {...options, cache: 'no-store', signal: controller.signal});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'The station could not complete the request.');
      return data;
    } finally { clearTimeout(timer); }
  }
  function clearTrack() {
    currentTrack = null;
    lastMetadataAt = 0;
    $('songTitle').textContent = 'Track information unavailable';
    $('showName').textContent = 'WIT Radio';
    $('showLabel').textContent = 'MUSIC FOR THE MOMENT';
    $('djName').textContent = 'WIT Radio';
    $('djInitials').textContent = 'W';
    $('djRole').textContent = 'Host details will return when the station reconnects.';
    $('artistName').textContent = 'The station’s track details are temporarily unavailable.';
    $('feedbackNote').textContent = 'Track feedback will return when current track details are available.';
    if ('mediaSession' in navigator) navigator.mediaSession.metadata = null;
    refreshButtons();
  }
  async function loadNowPlaying() {
    try {
      const data = await request('now-playing');
      const age = Date.now() - Date.parse(data.timestamp);
      if (!Number.isFinite(age) || age < -10000 || age > 30000 || !data.track || data.type === 'silence' || data.type === 'unknown') throw new Error('Stale metadata');
      const previousId = currentTrack?.id;
      currentTrack = typeof data.track_id === 'string' && /^[a-f0-9]{64}$/.test(data.track_id) ? {id: data.track_id, title: String(data.track), artist: data.artist || 'WIT AI Music Lab', show: data.show || 'WIT Radio'} : null;
      lastMetadataAt = Date.now();
      $('songTitle').textContent = data.track;
      $('artistName').textContent = data.type === 'talk' ? 'A short break with your host' : (data.type === 'backup' ? 'WIT Radio • Backup music' : data.artist || 'WIT AI Music Lab');
      if (data.show) { $('showName').textContent = data.show; $('showLabel').textContent = 'CURRENT SHOW'; }
      if (data.host) {
        $('djName').textContent = data.host;
        $('djInitials').textContent = String(data.host).slice(0, 1).toUpperCase();
        $('djRole').textContent = data.dj_role || 'Keeping you company on WIT Radio.';
      }
      if (previousId !== currentTrack?.id) $('feedbackNote').textContent = currentTrack ? 'Your vote helps shape future music rotation.' : 'Track feedback is available during music.';
      if ('mediaSession' in navigator && 'MediaMetadata' in window) navigator.mediaSession.metadata = new MediaMetadata({title: data.track, artist: data.host || 'WIT Radio', album: data.show || 'WIT Radio'});
      refreshButtons();
    } catch (_) { clearTrack(); }
  }
  async function saveVote(vote) {
    if (!currentTrack || playbackState !== 'playing' || savingVote || Date.now() - lastMetadataAt >= 30000) return;
    const track = {...currentTrack};
    savingVote = true;
    refreshButtons();
    $('feedbackNote').textContent = 'Saving your feedback…';
    try {
      const result = await request('feedback', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({track_id: track.id, listener_id: listenerId, vote})});
      if (result.status !== 'saved' || result.track_id !== track.id || result.vote !== vote) throw new Error('Feedback could not be confirmed. Please try again.');
      votes.set(track.id, vote);
      if (currentTrack?.id === track.id) $('feedbackNote').textContent = 'Feedback saved. Thank you for helping shape WIT Radio.';
    } catch (error) {
      if (currentTrack?.id === track.id) $('feedbackNote').textContent = `Not saved. ${error.name === 'AbortError' ? 'The request timed out. Please try again.' : error.message}`;
    } finally { savingVote = false; refreshButtons(); }
  }
  $('likeButton').addEventListener('click', () => saveVote('like'));
  $('dislikeButton').addEventListener('click', () => saveVote('dislike'));
  function row(title, secondary) {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.textContent = title;
    li.appendChild(name);
    if (secondary) { const note = document.createElement('small'); note.textContent = secondary; li.appendChild(note); }
    return li;
  }
  function list(id, rows, empty) {
    const target = $(id);
    target.replaceChildren(...rows);
    if (!rows.length) { const item = row(empty); item.className = 'empty'; target.appendChild(item); }
  }
  function renderFavorites() {
    const rows = favorites.map(track => {
      const item = row(track.title);
      const button = document.createElement('button');
      button.className = 'remove'; button.type = 'button'; button.textContent = '×';
      button.setAttribute('aria-label', `Remove ${track.title} from saved tracks`);
      button.addEventListener('click', () => {
        const next = favorites.filter(t => t.id !== track.id);
        if (!persist('witRadioFavorites', next)) { $('feedbackNote').textContent = 'Your browser could not update saved tracks.'; return; }
        favorites = next; renderFavorites(); refreshButtons();
      });
      item.appendChild(button); return item;
    });
    list('favoritesList', rows, 'Hear something you love? Save it here.');
  }
  $('favoriteButton').addEventListener('click', () => {
    if (!currentTrack || favorites.some(t => t.id === currentTrack.id)) return;
    const next = [{...currentTrack}, ...favorites].slice(0, 50);
    if (!persist('witRadioFavorites', next)) { $('feedbackNote').textContent = 'Your browser could not save this track.'; return; }
    favorites = next; renderFavorites(); refreshButtons();
    $('feedbackNote').textContent = 'Track saved on this device.';
  });
  async function loadSchedule() {
    try {
      const data = await request('schedule');
      if (!Array.isArray(data.upcoming)) throw new Error('No schedule');
      list('scheduleList', data.upcoming.slice(0, 3).map(show => row(show.name, show.starts_around)), 'No upcoming shows listed.');
      if (data.current?.description) $('showDescription').textContent = data.current.description;
    } catch (_) { list('scheduleList', [], 'The schedule is temporarily unavailable.'); }
  }
  async function loadHistory() {
    try {
      const data = await request('history');
      if (!Array.isArray(data.recent)) throw new Error('No history');
      const tracks = data.recent.filter(t => ['bumper', 'backup'].includes(t.vibe)).slice(0, 5);
      list('historyList', tracks.map(track => row(track.track_name || 'Instrumental track')), 'No music history yet.');
    } catch (_) { list('historyList', [], 'Play history is temporarily unavailable.'); }
  }
  renderFavorites();
  setState('ready');
  loadNowPlaying(); loadSchedule(); loadHistory();
  setInterval(loadNowPlaying, 10000);
  setInterval(() => { loadSchedule(); loadHistory(); }, 60000);
  setInterval(() => { if (currentTrack && Date.now() - lastMetadataAt >= 30000) clearTrack(); }, 5000);
})();
