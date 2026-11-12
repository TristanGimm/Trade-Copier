(function () {
  // --- product navigation: opens on hover and on click ---------------------
  (function () {
    var box = document.getElementById('nav-switch');
    if (!box) return;
    var button = box.querySelector('button');
    var timer = null;
    // Hover opens the menu before the click lands, so a click right after a
    // hover must not close it again -- only a deliberate second click should.
    var openedByHover = false;
    function set(open) {
      box.classList.toggle('open', open);
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    button.addEventListener('click', function (event) {
      event.stopPropagation();
      if (openedByHover) { openedByHover = false; return; }
      set(!box.classList.contains('open'));
    });
    box.addEventListener('mouseenter', function () {
      clearTimeout(timer);
      if (!box.classList.contains('open')) { openedByHover = true; set(true); }
    });
    box.addEventListener('mouseleave', function () {
      clearTimeout(timer);
      openedByHover = false;
      timer = setTimeout(function () { set(false); }, 140);
    });
    document.addEventListener('click', function () { set(false); });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') set(false);
    });

    // Opened from this machine, the three products link to their local ports
    // so they can be tested against each other without touching production.
    var host = location.hostname;
    if (host !== 'localhost' && host !== '127.0.0.1' && !/.local$/.test(host)) return;
    var scheme = location.protocol === 'https:' ? 'https' : 'http';
    Array.prototype.forEach.call(box.querySelectorAll('a[data-port]'), function (link) {
      link.href = scheme + '://' + host + ':' + link.getAttribute('data-port') + (link.getAttribute('data-path') || '/');
    });
  })();

  // --- one clip per orientation; only the matching one is loaded -----------
  var wide = document.getElementById('wide');
  var tall = document.getElementById('tall');
  var portrait = window.matchMedia('(orientation: portrait)');
  function apply() {
    var tall_ = portrait.matches || window.innerWidth < window.innerHeight;
    wide.classList.toggle('on', !tall_);
    tall.classList.toggle('on', tall_);
    var active = tall_ ? tall : wide;
    var idle = tall_ ? wide : tall;
    idle.pause();
    if (!reduced.matches) active.play().catch(function () {});
  }
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)');

  // --- alternate between the two scenes every two minutes ------------------
  var scenes = [
    { wide: '/videos/globe-landscape.mp4', tall: '/videos/globe-portrait.mp4' },
    { wide: '/videos/horizon-landscape.mp4', tall: '/videos/horizon-portrait.mp4' }
  ];
  var index = 0;
  function swap() {
    index = (index + 1) % scenes.length;
    var target = (portrait.matches || window.innerWidth < window.innerHeight) ? 'tall' : 'wide';
    var el = target === 'tall' ? tall : wide;
    var other = target === 'tall' ? wide : tall;
    other.classList.remove('on');
    el.classList.remove('on');
    el.src = scenes[index][target];
    el.load();
    el.play().catch(function () {});
    el.classList.add('on');
  }

  apply();
  portrait.addEventListener('change', apply);
  window.addEventListener('resize', apply);
  if (!reduced.matches) setInterval(swap, 120000);

  // Pause when the tab is hidden: a background video should not burn battery.
  document.addEventListener('visibilitychange', function () {
    var active = wide.classList.contains('on') ? wide : tall;
    if (document.hidden) active.pause();
    else if (!reduced.matches) active.play().catch(function () {});
  });

})();
