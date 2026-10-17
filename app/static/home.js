// Home: drag a GPX file onto the drop zone and the upload starts.
(function () {
  const z = document.getElementById('dropzone');
  if (!z) return;
  const input = z.querySelector('input[type=file]'), name = document.getElementById('dz-name'), form = z.closest('form');
  const show = () => { if (input.files.length) name.textContent = input.files[0].name; };
  input.addEventListener('change', show);
  ['dragenter', 'dragover'].forEach(t => z.addEventListener(t, e => { e.preventDefault(); z.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(t => z.addEventListener(t, e => { e.preventDefault(); z.classList.remove('over'); }));
  z.addEventListener('drop', e => {
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (!f) return;
    if (!/\.gpx$/i.test(f.name)) { name.textContent = 'That is not a GPX file.'; return; }
    const dt = new DataTransfer(); dt.items.add(f); input.files = dt.files; show();
    form.requestSubmit();
  });
})();
