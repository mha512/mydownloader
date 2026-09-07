function applyTheme(theme) {
  const isDark = theme === 'dark';
  document.body.classList.toggle('dark-mode', isDark);
  const themeIcon = document.getElementById('theme-icon');
  if (themeIcon) {
    themeIcon.innerHTML = isDark
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.65 17.65l1.42 1.42M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.65 6.35l1.42-1.42"></path></svg>'
      : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 15.5A8.5 8.5 0 0 1 8.5 4 8.5 8.5 0 1 0 20 15.5Z"></path></svg>';
  }
  document.getElementById('theme-toggle')?.setAttribute(
    'aria-label', isDark ? 'Switch to light mode' : 'Switch to dark mode');
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', isDark ? '#071313' : '#008080');
}

function toggleTheme() {
  const next = document.body.classList.contains('dark-mode') ? 'light' : 'dark';
  localStorage.setItem('ToxicDownloader-theme', next);
  applyTheme(next);
}

function toggleMenu() {
  const menu = document.getElementById('mobile-menu');
  const button = document.getElementById('menu-button');
  if (!menu) return;
  const isOpen = !menu.hidden;
  menu.hidden = isOpen;
  menu.classList.toggle('open', !isOpen);
  button?.setAttribute('aria-expanded', String(!isOpen));
  button?.setAttribute('aria-label', isOpen ? 'Open navigation' : 'Close navigation');
}

function dismissPreloader() {
  const preloader = document.getElementById('site-preloader');
  if (!preloader) return;
  window.setTimeout(() => {
    preloader.classList.add('is-loaded');
    window.setTimeout(() => preloader.remove(), 500);
  }, 2000);
}

document.addEventListener('DOMContentLoaded', () => {
  const savedTheme = localStorage.getItem('ToxicDownloader-theme');
  const preferredTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  applyTheme(savedTheme || preferredTheme);
  document.getElementById('theme-toggle')?.addEventListener('click', toggleTheme);
  document.getElementById('menu-button')?.addEventListener('click', toggleMenu);
  dismissPreloader();
  document.querySelectorAll('.mobile-menu a').forEach((link) => {
    link.addEventListener('click', () => {
      const menu = document.getElementById('mobile-menu');
      const button = document.getElementById('menu-button');
      if (menu) {
        menu.classList.remove('open');
        menu.hidden = true;
      }
      button?.setAttribute('aria-expanded', 'false');
      button?.setAttribute('aria-label', 'Open navigation');
    });
  });
});
