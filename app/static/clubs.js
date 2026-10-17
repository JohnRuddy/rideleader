// Copy buttons on the club page: the invite code, or a sign-in link with the code filled in.
document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-copy]');
  if (!b) return;
  const text = b.dataset.copy, old = b.textContent;
  try { await navigator.clipboard.writeText(text); b.textContent = 'Copied'; }
  catch (err) { prompt('Copy this:', text); }
  setTimeout(() => { b.textContent = old; }, 1600);
});
