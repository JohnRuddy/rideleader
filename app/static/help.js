// Help dialog: opens from any [data-open-help] button, from the #help address, and closes on Esc, the × or a click outside.
(function () {
  const dlg = document.getElementById('help');
  if (!dlg) return;
  const open = () => { if (!dlg.open) (dlg.showModal ? dlg.showModal() : dlg.setAttribute('open', '')); };
  const close = () => { if (dlg.open) (dlg.close ? dlg.close() : dlg.removeAttribute('open')); if (location.hash === '#help') history.replaceState(null, '', location.pathname + location.search); };
  document.addEventListener('click', e => {
    if (e.target.closest('[data-open-help]')) { e.preventDefault(); open(); }
    else if (e.target.closest('[data-close-help]')) close();
    else if (e.target.closest('a[href="#help"]')) { e.preventDefault(); open(); }
    else if (e.target === dlg) close();                       // a click on the backdrop
  });
  dlg.addEventListener('close', () => { if (location.hash === '#help') history.replaceState(null, '', location.pathname + location.search); });
  if (location.hash === '#help') open();
})();
