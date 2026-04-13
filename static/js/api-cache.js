/**
 * SessionStorage-backed JSON response cache for GET fetches.
 * Reduces skeleton flashes: lists reuse cached data until TTL or invalidation.
 */
(function () {
  var PREFIX = 'ojt:j:v1:';

  function storageKey(url) {
    return PREFIX + url;
  }

  function getEntry(url) {
    try {
      var raw = sessionStorage.getItem(storageKey(url));
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  }

  function setEntry(url, data, ttlMs) {
    try {
      sessionStorage.setItem(
        storageKey(url),
        JSON.stringify({
          t: Date.now(),
          ttlMs: ttlMs,
          data: data,
        })
      );
    } catch (e) {
      /* quota */
    }
  }

  window.ojtCacheGet = function (url) {
    var e = getEntry(url);
    return e ? e.data : null;
  };

  window.ojtCacheIsFresh = function (url, ttlMsOverride) {
    var e = getEntry(url);
    if (!e || !e.t) return false;
    var ttl = ttlMsOverride != null ? ttlMsOverride : e.ttlMs || 45000;
    return Date.now() - e.t < ttl;
  };

  /**
   * @param {string} url - full URL with query string
   * @param {RequestInit} [init]
   * @param {{ ttlMs?: number, bypassCache?: boolean }} [opts]
   * @returns {Promise<{ ok: boolean, status: number, data: any, fromCache: boolean }>}
   */
  window.ojtFetchJson = async function (url, init, opts) {
    opts = opts || {};
    var ttl = opts.ttlMs != null ? opts.ttlMs : 45000;
    var bypass = opts.bypassCache === true;

    if (!bypass && window.ojtCacheIsFresh(url, ttl)) {
      var e = getEntry(url);
      if (e && e.data !== undefined) {
        return { ok: true, status: 200, data: e.data, fromCache: true };
      }
    }

    var res = await fetch(url, init || {});
    var text = await res.text();
    var data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (err) {
      return { ok: false, status: res.status, data: null, fromCache: false, parseError: true };
    }

    if (res.ok && data !== null) {
      setEntry(url, data, ttl);
    }

    return { ok: res.ok, status: res.status, data: data, fromCache: false };
  };

  /** Remove cache entries whose stored URL (after prefix) contains substring. */
  window.ojtCacheInvalidateContains = function (pathSubstring) {
    try {
      for (var i = sessionStorage.length - 1; i >= 0; i--) {
        var k = sessionStorage.key(i);
        if (!k || k.indexOf(PREFIX) !== 0) continue;
        var u = k.slice(PREFIX.length);
        if (u.indexOf(pathSubstring) !== -1) {
          sessionStorage.removeItem(k);
        }
      }
    } catch (e) {}
  };

  window.ojtCacheInvalidateUrl = function (url) {
    try {
      sessionStorage.removeItem(storageKey(url));
    } catch (e) {}
  };
})();
