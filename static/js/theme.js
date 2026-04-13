(function () {
  var STORAGE_KEY = 'ojt-theme';

  function getMetaThemeColor() {
    return document.querySelector('meta[name="theme-color"]');
  }

  function applyTheme(mode) {
    var isLight = mode === 'light';
    if (isLight) {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    var meta = getMetaThemeColor();
    if (meta) {
      meta.setAttribute('content', isLight ? '#f0f4fa' : '#0c1017');
    }
    var btn = document.getElementById('theme-toggle');
    if (btn) {
      btn.textContent = isLight ? 'Dark mode' : 'Light mode';
      btn.setAttribute('aria-pressed', isLight ? 'true' : 'false');
      btn.setAttribute('aria-label', isLight ? 'Switch to dark mode' : 'Switch to light mode');
    }
  }

  function readStoredTheme() {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      return null;
    }
  }

  function writeStoredTheme(mode) {
    try {
      if (mode === 'dark') {
        localStorage.removeItem(STORAGE_KEY);
      } else {
        localStorage.setItem(STORAGE_KEY, mode);
      }
    } catch (e) {}
  }

  function init() {
    var stored = readStoredTheme();
    applyTheme(stored === 'light' ? 'light' : 'dark');
    var btn = document.getElementById('theme-toggle');
    if (btn) {
      btn.addEventListener('click', function () {
        var next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
        writeStoredTheme(next);
        applyTheme(next);
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
