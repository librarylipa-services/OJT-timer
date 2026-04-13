(function () {
  function removePageSkeleton() {
    var el = document.getElementById('page-skeleton-overlay');
    if (!el) return;
    el.classList.add('hidden');
    el.setAttribute('aria-hidden', 'true');
    el.setAttribute('aria-busy', 'false');
  }

  window.removePageSkeleton = removePageSkeleton;

  document.addEventListener('DOMContentLoaded', function () {
    if (document.body.getAttribute('data-page-skeleton') === 'defer') {
      /* Fast path: cached /api/batches (account/register) — hide overlay without waiting for network */
      try {
        if (
          typeof window.ojtCacheIsFresh === 'function' &&
          window.ojtCacheIsFresh('/api/batches', 60000)
        ) {
          removePageSkeleton();
        }
      } catch (e) {}
      setTimeout(removePageSkeleton, 12000);
      return;
    }
    requestAnimationFrame(removePageSkeleton);
  });
})();
