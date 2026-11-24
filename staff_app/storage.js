/* Blue Nile staff app storage: shared between every phone through the shop server.
   Same calls the app always used (collection().onSnapshot, doc().set/delete/onSnapshot,
   collection().doc().id), but data lives in the workshop database (bluenile/staff.py).
   Changes from other phones arrive within a few seconds. A copy is kept on this phone
   so the app opens instantly and can still be viewed with no signal. */
(function () {
  var API = 'api/', KEY = 'bluenile-sync-v1', subs = {}, docSubs = {}, n = 0;
  var store = { rev: 0, data: {} }, loaded = false, timer = null, polling = false;
  try { var saved = JSON.parse(localStorage.getItem(KEY)); if (saved && saved.data) { store = saved; loaded = true; } } catch (e) {}

  function persist() { try { localStorage.setItem(KEY, JSON.stringify(store)); } catch (e) {} }
  function coll(c) { return store.data[c] || (store.data[c] = {}); }
  function copy(v) { return v == null ? v : JSON.parse(JSON.stringify(v)); }
  function snap(c) {
    var d = coll(c);
    return { docs: Object.keys(d).map(function (id) { return { id: id, data: function () { return copy(d[id]); } }; }) };
  }
  function notify(c) {
    (subs[c] || []).forEach(function (s) { s.ok(snap(c)); });
    Object.keys(docSubs).forEach(function (p) {
      if (p.split('/')[0] === c) docSubs[p].forEach(function (s) { s.run(); });
    });
  }
  function signInAgain() { location.href = '/staff/login'; }

  async function poll() {
    if (polling) return; polling = true; clearTimeout(timer);
    try {
      for (var guard = 0; guard < 20; guard++) {
        var r = await fetch(API + 'changes?since=' + store.rev, { credentials: 'same-origin', cache: 'no-store' });
        if (r.status === 401) return signInAgain();
        if (!r.ok) throw { code: 'server-' + r.status };
        var j = await r.json(), touched = {};
        j.docs.forEach(function (d) {
          if (d.data === null) delete coll(d.c)[d.id]; else coll(d.c)[d.id] = d.data;
          touched[d.c] = 1;
        });
        store.rev = j.rev;
        var first = !loaded; loaded = true;
        if (j.docs.length || first) persist();
        Object.keys(first ? subs : touched).forEach(notify);
        if (first) Object.keys(docSubs).forEach(function (p) { docSubs[p].forEach(function (s) { s.run(); }); });
        if (j.rev >= j.latest) break;
      }
    } catch (e) {
      if (!loaded) Object.keys(subs).forEach(function (c) { (subs[c] || []).forEach(function (s) { s.err && s.err({ code: e.code || 'offline' }); }); });
    } finally {
      polling = false;
      timer = setTimeout(poll, document.hidden ? 30000 : 4000);
    }
  }

  async function write(c, id, v) {
    if (v === null) delete coll(c)[id]; else coll(c)[id] = copy(v);
    persist(); notify(c);
    var r = await fetch(API + 'doc/' + encodeURIComponent(c) + '/' + encodeURIComponent(id), {
      method: v === null ? 'DELETE' : 'PUT', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Staff': '1' },
      body: v === null ? undefined : JSON.stringify(v)
    });
    if (r.status === 401) { signInAgain(); throw { code: 'signed-out' }; }
    if (!r.ok) throw { code: 'server-' + r.status };
  }

  function docRef(path) {
    var a = path.split('/'), c = a[0], id = a[1];
    return {
      id: id,
      set: function (v) { return write(c, id, v); },
      delete: function () { return write(c, id, null); },
      onSnapshot: function (ok, err) {
        var s = { run: function () { var x = coll(c)[id]; ok({ exists: x != null, data: function () { return copy(x); } }); }, err: err };
        (docSubs[path] = docSubs[path] || []).push(s);
        if (loaded) setTimeout(s.run, 0);
      }
    };
  }
  var db = {
    collection: function (c) {
      return {
        doc: function () { return { id: 'd' + Date.now().toString(36) + (++n) + Math.random().toString(36).slice(2, 6) }; },
        onSnapshot: function (ok, err) {
          (subs[c] = subs[c] || []).push({ ok: ok, err: err });
          if (loaded) setTimeout(function () { ok(snap(c)); }, 0);
        }
      };
    },
    doc: docRef
  };

  window.claude = { use: async function () { setTimeout(poll, 0); return db; } };
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
  window.addEventListener('online', poll);
  if (navigator.storage && navigator.storage.persist) navigator.storage.persist();
  if ('serviceWorker' in navigator && location.protocol !== 'file:') {
    window.addEventListener('load', function () { navigator.serviceWorker.register('sw.js').catch(function () {}); });
  }
})();
