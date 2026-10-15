(function () {
  var root = document.documentElement;
  var query = matchMedia('(prefers-color-scheme: dark)');

  function saved() {
    try { return localStorage.getItem('theme'); } catch (e) { return null; }
  }
  function current() {
    return saved() || (query.matches ? 'dark' : 'light');
  }
  function apply(theme) {
    root.style.colorScheme = theme;
    var button = document.getElementById('theme-toggle');
    if (button) button.textContent = theme === 'dark' ? 'Light mode' : 'Dark mode';
  }

  // Runs before first paint (script is not deferred) to avoid a flash.
  apply(current());

  document.addEventListener('DOMContentLoaded', function () {
    apply(current());
    document.getElementById('theme-toggle').addEventListener('click', function () {
      var next = current() === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem('theme', next); } catch (e) {}
      apply(next);
    });
  });
})();
